from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from radar.classify import ANTHROPIC_VERSION, API_URL
from radar.config import Settings
from radar.http_utils import RateLimitedClient

logger = logging.getLogger("radar.brief")

MAX_BRIEF_TOKENS = 200
MAX_REPORT_NARRATIVE_TOKENS = 400
MAX_COA_TOKENS = 150
MAX_EXPLANATION_TOKENS = 350
# Same bound classify.py uses when building its own classification prompt --
# keeps this prompt's size (and cost) bounded the same way.
EXPLANATION_MAX_TEXT_CHARS = 4000


class BriefGenerationError(RuntimeError):
    """Raised internally when a Claude call for a brief/report narrative fails
    or returns something unusable. Caught inside generate_exec_brief()/
    generate_incident_report() themselves -- callers never see this, they
    always get back a usable string (Claude-written, or a template fallback).
    """


@dataclass
class BriefFacts:
    """The facts fed into both the exec-brief prompt and its template
    fallback -- one alert (member_count=1) or one root-cause cluster
    (member_count>1, episode_count set from ClusterSummary).
    """

    subject: str
    category: str
    severity: str
    issue_summary: str
    platforms: list[str]
    member_count: int = 1
    velocity: float | None = None
    matched_term: str | None = None
    episode_count: int | None = None
    protection_tier: str | None = None
    client: str | None = None


def _call_claude(
    settings: Settings, prompt: str, max_tokens: int, sleep_fn: Callable[[float], None]
) -> str:
    if not settings.anthropic_api_key:
        raise BriefGenerationError("ANTHROPIC_API_KEY is not configured")

    payload = {
        "model": settings.classifier_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    # A fresh client per call -- unlike ClaudeClassifier (a batch loop reusing
    # one client across many posts), brief/report generation is a one-off
    # dashboard-triggered request.
    with httpx.Client(timeout=60.0) as client:
        rate_limited = RateLimitedClient(client, sleep_fn=sleep_fn)
        try:
            response = rate_limited.request(
                "POST",
                API_URL,
                json=payload,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise BriefGenerationError(f"Claude brief/report request failed: {exc}") from exc
        except ValueError as exc:
            raise BriefGenerationError(f"Claude brief/report response was not valid JSON: {exc}") from exc

    content = data.get("content", []) if isinstance(data, dict) else []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
            return block["text"].strip()
    raise BriefGenerationError("Claude response had no usable text content block")


def _build_brief_prompt(facts: BriefFacts) -> str:
    lines = [
        "You are drafting a 2-3 sentence executive brief for an internal escalations "
        "channel, summarizing a live public-signal alert about Claude (Anthropic's AI) "
        "or the Claude API. Be crisp and factual: state what's happening, how "
        "significant it is, and one recommended next step. No preamble, no bullet "
        "points, no headings -- just the brief itself.",
        "",
        f"Subject: {facts.subject}",
        f"Category: {facts.category}",
        f"Severity: {facts.severity}",
    ]
    if facts.velocity is not None:
        lines.append(f"Velocity: {facts.velocity:.1f} engagement points/hour")
    lines.append(f"Platform(s) involved: {', '.join(facts.platforms) or 'unknown'}")
    if facts.member_count > 1:
        lines.append(f"Number of posts in this cluster: {facts.member_count}")
    if facts.matched_term:
        lines.append(f"Matched search term: {facts.matched_term}")
    if facts.episode_count and facts.episode_count > 1:
        lines.append(f"This root cause has recurred {facts.episode_count} separate times.")
    if facts.protection_tier == "flagship":
        lines.append(
            "This involves a FLAGSHIP-tier model -- treat as higher business/reputational "
            "impact than the same issue on a smaller model would carry."
        )
    lines.append(f"Representative summary: {facts.issue_summary}")
    return "\n".join(lines)


def _template_brief(facts: BriefFacts) -> str:
    parts = [f"{facts.subject}: {facts.category}, severity={facts.severity}"]
    if facts.velocity is not None:
        parts.append(f"velocity={facts.velocity:.1f}")
    if facts.member_count > 1:
        parts.append(f"{facts.member_count} posts across {', '.join(facts.platforms) or 'unknown platforms'}")
    elif facts.platforms:
        parts.append(f"platform={facts.platforms[0]}")
    if facts.episode_count and facts.episode_count > 1:
        parts.append(f"recurred {facts.episode_count} times")
    if facts.protection_tier == "flagship":
        parts.append("FLAGSHIP-tier model")
    return ". ".join(parts) + f". {facts.issue_summary}"


def generate_exec_brief(
    settings: Settings, facts: BriefFacts, sleep_fn: Callable[[float], None] = time.sleep
) -> str:
    """Always returns a usable brief -- falls back to a deterministic template
    if ANTHROPIC_API_KEY is missing or the Claude call fails for any reason,
    so a dashboard action always shows something rather than an error.
    """
    try:
        return _call_claude(settings, _build_brief_prompt(facts), MAX_BRIEF_TOKENS, sleep_fn)
    except BriefGenerationError:
        logger.warning("Falling back to templated exec brief for %r", facts.subject)
        return _template_brief(facts)


def _build_coa_prompt(facts: BriefFacts, playbook: str, target_status: str) -> str:
    lines = [
        "You are recommending a single, concrete Course of Action for an internal "
        "escalations team working a public-signal alert about Claude (Anthropic's AI) "
        "or the Claude API. Give ONE specific, actionable recommendation (who should do "
        "what) in 1-2 sentences -- no preamble, no list of options, just the "
        "recommendation itself.",
        "",
        f"This alert is now being moved to: {target_status}",
        f"Category: {facts.category}",
        f"Severity: {facts.severity}",
        f"Platform(s): {', '.join(facts.platforms) or 'unknown'}",
    ]
    if facts.client:
        lines.append(
            f"This involves a specific enterprise client: {facts.client} -- if that "
            "changes the right action (e.g. looping in their account team instead of a "
            "generic public response), say so explicitly."
        )
    if facts.protection_tier == "flagship":
        lines.append("This involves a FLAGSHIP-tier model -- treat with higher urgency.")
    lines.append(f"The team's standing playbook for this category: {playbook}")
    lines.append(f"Representative summary: {facts.issue_summary}")
    return "\n".join(lines)


def _template_coa(facts: BriefFacts, playbook: str) -> str:
    base = playbook or f"Triage this {facts.category} alert manually."
    if facts.client:
        return f"{base} Regarding {facts.client}: loop in their account team given the client-scoped match."
    return base


def generate_coa(
    settings: Settings,
    facts: BriefFacts,
    playbook: str,
    target_status: str,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Always returns a usable Course of Action -- falls back to the
    category's standing playbook (client-annotated when one is present) if
    Claude is unavailable or the call fails, so a Kanban card never lands
    with nothing to show.
    """
    try:
        return _call_claude(
            settings, _build_coa_prompt(facts, playbook, target_status), MAX_COA_TOKENS, sleep_fn
        )
    except BriefGenerationError:
        logger.warning("Falling back to templated COA for %r", facts.subject)
        return _template_coa(facts, playbook)


def _build_explanation_prompt(facts: BriefFacts, raw_text: str) -> str:
    return (
        "You are explaining a technical issue report to a Program Manager who needs to "
        "actually understand what's going wrong, not skim a one-line summary -- but who "
        "isn't necessarily a Claude API engineer either. Explain in plain but precise "
        "language: what's actually happening, the likely mechanism or root cause if it "
        "can be reasonably inferred from the text below, and why it matters. Ground every "
        "claim only in the text provided -- don't invent details that aren't there, and "
        "say so if the text doesn't give enough to infer a cause. 3-5 sentences, no "
        "headings, no preamble, don't just restate the category or severity.\n\n"
        f"Category: {facts.category}\n"
        f"Severity: {facts.severity}\n"
        f"Platform(s): {', '.join(facts.platforms) or 'unknown'}\n\n"
        f"Original post text:\n{raw_text}"
    )


def generate_technical_explanation(
    settings: Settings,
    facts: BriefFacts,
    raw_text: str | None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """A longer, technical explanation of the underlying issue grounded in
    the post's full original text -- distinct from the exec brief (short,
    audience is leadership, no technical depth) and the 120-char
    issue_summary (a table-row label, not meant to explain anything).

    Falls back to the issue_summary itself, with a note, if raw_text was
    never captured or has since aged out (see radar/db.py's
    purge_old_raw_text() / Settings.raw_text_retention_days) -- there's
    nothing left to ground a real explanation in at that point. Also falls
    back (without the note) if Claude is unavailable or the call fails.
    """
    if not raw_text:
        return (
            facts.issue_summary
            + " (The original post text is no longer retained, so this is just the "
            "classifier's short summary, not a full explanation.)"
        )
    try:
        return _call_claude(
            settings,
            _build_explanation_prompt(facts, raw_text[:EXPLANATION_MAX_TEXT_CHARS]),
            MAX_EXPLANATION_TOKENS,
            sleep_fn,
        )
    except BriefGenerationError:
        logger.warning("Falling back to the issue summary for %r's technical explanation", facts.subject)
        return facts.issue_summary


# (from_status, to_status, note, coa, created_at)
IncidentEvent = tuple[str, str, "str | None", "str | None", str]


def _format_timeline_markdown(timeline: list[IncidentEvent]) -> str:
    if not timeline:
        return "_No status transitions recorded._"
    lines = []
    for from_status, to_status, note, coa, created_at in timeline:
        line = f"- **{created_at}** — {from_status} → {to_status}"
        if note:
            line += f": {note}"
        lines.append(line)
        if coa:
            lines.append(f"  - Recommended: {coa}")
    return "\n".join(lines)


def _build_report_narrative_prompt(
    facts: BriefFacts, timeline: list[IncidentEvent], closing_note: str
) -> str:
    tier_line = (
        "Model protection tier: flagship -- note explicitly that this carries more "
        "business/reputational weight than the same issue on a smaller model.\n"
        if facts.protection_tier == "flagship"
        else ""
    )
    return (
        "You are writing the 'What happened' section of an internal post-incident "
        "report about a public-signal alert for Claude (Anthropic's AI) or the "
        "Claude API. Write 2-4 factual sentences a new responder could read cold and "
        "understand the situation -- no preamble, no headings, just the narrative "
        "paragraph itself.\n\n"
        f"Category: {facts.category}\n"
        f"Severity: {facts.severity}\n"
        f"Platform(s): {', '.join(facts.platforms) or 'unknown'}\n"
        f"{tier_line}"
        f"Representative summary: {facts.issue_summary}\n"
        f"Status transitions: {_format_timeline_markdown(timeline)}\n"
        f"Closing note from the responder: {closing_note}"
    )


def generate_incident_report(
    settings: Settings,
    facts: BriefFacts,
    timeline: list[IncidentEvent],
    closing_note: str,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Returns a complete Markdown report every time. Only the 'What happened'
    narrative depends on Claude (falls back to the raw issue_summary on
    failure) -- the facts/timeline/closing-note sections are always rendered
    deterministically, so exact figures and timestamps can never be
    hallucinated.
    """
    try:
        what_happened = _call_claude(
            settings,
            _build_report_narrative_prompt(facts, timeline, closing_note),
            MAX_REPORT_NARRATIVE_TOKENS,
            sleep_fn,
        )
    except BriefGenerationError:
        logger.warning("Falling back to the raw issue summary for %r's report narrative", facts.subject)
        what_happened = facts.issue_summary

    fact_lines = [
        f"- Category: {facts.category}",
        f"- Severity: {facts.severity}",
        f"- Platforms: {', '.join(facts.platforms) or 'unknown'}",
    ]
    if facts.velocity is not None:
        fact_lines.append(f"- Velocity: {facts.velocity:.1f} engagement points/hour")
    if facts.episode_count and facts.episode_count > 1:
        fact_lines.append(f"- Recurred {facts.episode_count} separate times")
    if facts.protection_tier == "flagship":
        fact_lines.append("- Model protection tier: **flagship**")

    return (
        f"# Post-incident report: {facts.subject}\n\n"
        f"## What happened\n\n{what_happened}\n\n"
        f"## Facts\n\n" + "\n".join(fact_lines) + "\n\n"
        f"## Timeline\n\n{_format_timeline_markdown(timeline)}\n\n"
        f"## What should change\n\n{closing_note}\n"
    )
