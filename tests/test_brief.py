from __future__ import annotations

import httpx
import pytest
import respx

from radar.brief import BriefFacts, generate_exec_brief, generate_incident_report
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
        ("open", "acknowledged", None, "2024-01-01T00:00:00+00:00"),
        ("acknowledged", "resolved", "Fixed upstream", "2024-01-01T02:00:00+00:00"),
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
