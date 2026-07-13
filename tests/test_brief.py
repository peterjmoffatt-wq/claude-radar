from __future__ import annotations

import httpx
import pytest
import respx

from radar.brief import (
    BriefFacts,
    generate_coa,
    generate_exec_brief,
    generate_incident_report,
    generate_technical_explanation,
)
from radar.classify import API_URL


def _facts(**overrides) -> BriefFacts:
    defaults = dict(
        subject="YouTube alert",
        category="product_bug",
        severity="high",
        issue_summary="Claude Code deletes the wrong plugin during cleanup.",
        platforms=["youtube"],
    )
    defaults.update(overrides)
    return BriefFacts(**defaults)


@respx.mock
def test_generate_exec_brief_returns_claude_text(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    brief = generate_exec_brief(settings, _facts(), sleep_fn=lambda s: None)

    assert brief == (
        "YouTube alert: users report Claude Code deleting the wrong plugin during "
        "cleanup. Velocity is moderate and confined to one platform so far. Recommend "
        "eng triage before the next release."
    )


@respx.mock
def test_generate_exec_brief_sends_no_tool_use(settings_factory, load_anthropic_fixture):
    # Briefs are free-text -- must NOT force tool-use the way classify.py does.
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    generate_exec_brief(settings, _facts(), sleep_fn=lambda s: None)

    import json

    body = json.loads(route.calls.last.request.content)
    assert "tools" not in body
    assert "tool_choice" not in body


def test_generate_exec_brief_falls_back_to_template_when_no_api_key(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    brief = generate_exec_brief(settings, _facts(velocity=270.4), sleep_fn=lambda s: None)

    assert "product_bug" in brief
    assert "severity=high" in brief
    assert "270.4" in brief
    assert "Claude Code deletes the wrong plugin" in brief


@respx.mock
def test_generate_exec_brief_falls_back_to_template_on_api_error(settings_factory):
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    settings = settings_factory()

    brief = generate_exec_brief(settings, _facts(), sleep_fn=lambda s: None)

    assert "product_bug" in brief
    assert "YouTube alert" in brief


@respx.mock
def test_generate_exec_brief_falls_back_when_no_text_block(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_no_text_block.json"))
    )
    settings = settings_factory()

    brief = generate_exec_brief(settings, _facts(), sleep_fn=lambda s: None)

    assert "product_bug" in brief


@respx.mock
def test_generate_exec_brief_cluster_mentions_member_count_and_recurrence(
    settings_factory, load_anthropic_fixture
):
    respx.post(API_URL).mock(return_value=httpx.Response(503))  # force template path
    settings = settings_factory()

    brief = generate_exec_brief(
        settings,
        _facts(
            subject="Product bug — claude code",
            platforms=["reddit", "youtube"],
            member_count=3,
            episode_count=2,
        ),
        sleep_fn=lambda s: None,
    )

    assert "3 posts across reddit, youtube" in brief
    assert "recurred 2 times" in brief


@respx.mock
def test_generate_incident_report_includes_claude_narrative_and_facts(
    settings_factory, load_anthropic_fixture
):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()
    timeline = [
        ("open", "acknowledged", None, None, "2024-01-01T00:00:00+00:00"),
        ("acknowledged", "resolved", "Fixed upstream", None, "2024-01-01T02:00:00+00:00"),
    ]

    report = generate_incident_report(
        settings, _facts(velocity=270.4), timeline, "Add a regression test.", sleep_fn=lambda s: None
    )

    assert "# Post-incident report: YouTube alert" in report
    assert "Recommend eng triage before the next release" in report  # from the mocked narrative
    assert "- Category: product_bug" in report
    assert "- Velocity: 270.4" in report
    assert "open → acknowledged" in report
    assert "Fixed upstream" in report
    assert "## What should change\n\nAdd a regression test." in report


def test_template_brief_mentions_flagship_tier(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    brief = generate_exec_brief(settings, _facts(protection_tier="flagship"), sleep_fn=lambda s: None)

    assert "FLAGSHIP-tier model" in brief


def test_template_brief_omits_tier_mention_when_standard(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    brief = generate_exec_brief(settings, _facts(protection_tier="standard"), sleep_fn=lambda s: None)

    assert "FLAGSHIP" not in brief


def test_incident_report_template_facts_mention_flagship_tier(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    report = generate_incident_report(
        settings, _facts(protection_tier="flagship"), [], "note", sleep_fn=lambda s: None
    )

    assert "Model protection tier: **flagship**" in report


@respx.mock
def test_generate_incident_report_falls_back_to_issue_summary_on_api_error(settings_factory):
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    settings = settings_factory()

    report = generate_incident_report(
        settings, _facts(), [], "Nothing to change.", sleep_fn=lambda s: None
    )

    assert "Claude Code deletes the wrong plugin during cleanup." in report
    assert "_No status transitions recorded._" in report


@pytest.mark.parametrize("timeline", [[]])
def test_format_timeline_markdown_empty_case(timeline, settings_factory):
    settings = settings_factory(anthropic_api_key="")
    report = generate_incident_report(settings, _facts(), timeline, "note", sleep_fn=lambda s: None)
    assert "_No status transitions recorded._" in report


def test_format_timeline_markdown_shows_coa_when_present():
    from radar.brief import _format_timeline_markdown

    timeline = [("open", "acknowledged", None, "Escalate to security.", "2024-01-01T00:00:00+00:00")]
    markdown = _format_timeline_markdown(timeline)

    assert "Recommended: Escalate to security." in markdown


@respx.mock
def test_generate_coa_returns_claude_text(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    coa = generate_coa(
        settings, _facts(), "Escalate to security immediately.", "acknowledged", sleep_fn=lambda s: None
    )

    assert "Recommend eng triage" in coa


def test_generate_coa_falls_back_to_playbook_template_on_api_error(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    coa = generate_coa(
        settings, _facts(), "Escalate to security immediately.", "acknowledged", sleep_fn=lambda s: None
    )

    assert coa == "Escalate to security immediately."


def test_generate_coa_template_mentions_client_when_present(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    coa = generate_coa(
        settings,
        _facts(client="McDonald's"),
        "Escalate to security immediately.",
        "acknowledged",
        sleep_fn=lambda s: None,
    )

    assert "Escalate to security immediately." in coa
    assert "McDonald's" in coa
    assert "account team" in coa


def test_generate_coa_template_falls_back_to_generic_when_no_playbook(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    coa = generate_coa(settings, _facts(category="other"), "", "acknowledged", sleep_fn=lambda s: None)

    assert "Triage this other alert manually." in coa


@respx.mock
def test_generate_coa_sends_no_tool_use(settings_factory, load_anthropic_fixture):
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    generate_coa(settings, _facts(), "playbook text", "acknowledged", sleep_fn=lambda s: None)

    import json

    body = json.loads(route.calls.last.request.content)
    assert "tools" not in body
    assert "tool_choice" not in body


@respx.mock
def test_generate_technical_explanation_returns_claude_text(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    explanation = generate_technical_explanation(
        settings, _facts(), "Full raw post text describing the bug in detail.", sleep_fn=lambda s: None
    )

    assert "Recommend eng triage" in explanation


def test_generate_technical_explanation_falls_back_when_raw_text_missing(settings_factory):
    settings = settings_factory()

    explanation = generate_technical_explanation(settings, _facts(), None, sleep_fn=lambda s: None)

    assert explanation.startswith("Claude Code deletes the wrong plugin during cleanup.")
    assert "no longer retained" in explanation


@respx.mock
def test_generate_technical_explanation_falls_back_to_issue_summary_on_api_error(settings_factory):
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    settings = settings_factory()

    explanation = generate_technical_explanation(
        settings, _facts(), "Full raw post text.", sleep_fn=lambda s: None
    )

    assert explanation == "Claude Code deletes the wrong plugin during cleanup."


@respx.mock
def test_generate_technical_explanation_sends_no_tool_use(settings_factory, load_anthropic_fixture):
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    settings = settings_factory()

    generate_technical_explanation(settings, _facts(), "Full raw post text.", sleep_fn=lambda s: None)

    import json

    body = json.loads(route.calls.last.request.content)
    assert "tools" not in body
    assert "tool_choice" not in body
