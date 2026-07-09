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
        except httpx.HTTPError as exc:
            raise BriefGenerationError(f"Claude brief/report request failed: {exc}") from exc

    data = response.json()
    for block in data.get("content", []):
        if block.get("type") == "text" and block.get("text", "").strip():
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


IncidentEvent = tuple[str, str, "str | None", str]  # (from_status, to_status, note, created_at)


def _format_timeline_markdown(timeline: list[IncidentEvent]) -> str:
    if not timeline:
        return "_No status transitions recorded._"
    lines = []
    for from_status, to_status, note, created_at in timeline:
        line = f"- **{created_at}** — {from_status} → {to_status}"
        if note:
            line += f": {note}"
        lines.append(line)
    return "\n".join(lines)


def _build_report_narrative_prompt(
    facts: BriefFacts, timeline: list[IncidentEvent], closing_note: str
) -> str:
    return (
        "You are writing the 'What happened' section of an internal post-incident "
        "report about a public-signal alert for Claude (Anthropic's AI) or the "
        "Claude API. Write 2-4 factual sentences a new responder could read cold and "
        "understand the situation -- no preamble, no headings, just the narrative "
        "paragraph itself.\n\n"
        f"Category: {facts.category}\n"
        f"Severity: {facts.severity}\n"
        f"Platform(s): {', '.join(facts.platforms) or 'unknown'}\n"
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

    return (
        f"# Post-incident report: {facts.subject}\n\n"
        f"## What happened\n\n{what_happened}\n\n"
        f"## Facts\n\n" + "\n".join(fact_lines) + "\n\n"
        f"## Timeline\n\n{_format_timeline_markdown(timeline)}\n\n"
        f"## What should change\n\n{closing_note}\n"
    )
