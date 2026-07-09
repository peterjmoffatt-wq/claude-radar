from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.stackoverflow import SEARCH_URL, StackOverflowAPIError, StackOverflowSource


def make_source(settings_factory, sleep_fn=None, **overrides):
    settings = settings_factory(**overrides)
    return StackOverflowSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_stackoverflow_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "so_5001"
    assert first.platform.value == "stackoverflow"
    assert first.author == "dev_so_1"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 12
    assert first.metrics.comments == 3
    assert first.url == "https://stackoverflow.com/questions/5001/claude-api-429"
    assert first.created_at.tzinfo is not None
    assert first.created_at == datetime.fromtimestamp(1698840000, tz=timezone.utc)


@respx.mock
def test_search_requests_withbody_filter_and_strips_html_into_text(
    settings_factory, load_stackoverflow_fixture
):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("search_top_with_body.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    sent_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert sent_params["filter"] == "withbody"

    first = posts[0]
    assert "Claude API returning 429 too many requests" in first.text
    assert "<p>" not in first.text
    assert "keeps erroring" in first.text  # body content survived HTML-stripping


@respx.mock
def test_search_recent_filters_by_since(settings_factory, load_stackoverflow_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime.fromtimestamp(1698900000, tz=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert len(posts) == 1
    assert posts[0].id == "so_5002"


@respx.mock
def test_search_recent_aggregates_multiple_pages(settings_factory, load_stackoverflow_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200, json=load_stackoverflow_fixture("search_recent_page1_with_more.json")
            ),
            httpx.Response(200, json=load_stackoverflow_fixture("search_recent_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["so_6001", "so_6002"]


@respx.mock
def test_no_results_returns_empty_list(settings_factory, load_stackoverflow_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []


@respx.mock
def test_optional_api_key_is_sent_when_configured(settings_factory, load_stackoverflow_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("empty_results.json"))
    )

    source = make_source(settings_factory, stackoverflow_api_key="my-so-key")
    source.search_top("claude api", window="week", limit=50)

    sent_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert sent_params["key"] == "my-so-key"


@respx.mock
def test_no_api_key_param_when_not_configured(settings_factory, load_stackoverflow_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("empty_results.json"))
    )

    source = make_source(settings_factory, stackoverflow_api_key="")
    source.search_top("claude api", window="week", limit=50)

    sent_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert "key" not in sent_params


@respx.mock
def test_stack_exchange_backoff_field_triggers_extra_sleep(settings_factory, load_stackoverflow_fixture):
    # Stack Exchange's documented throttling contract: a `backoff` field in a
    # successful (200) JSON body means "wait this many seconds" -- distinct
    # from RateLimitedClient's header-only 429/5xx handling, which never sees
    # this API's response body at all.
    fixture = load_stackoverflow_fixture("search_top_page1.json")
    fixture["backoff"] = 12
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=fixture))

    sleeps: list[float] = []
    source = make_source(settings_factory, sleep_fn=sleeps.append)
    source.search_top("claude api", window="week", limit=50)

    assert 12.0 in sleeps


@respx.mock
def test_no_backoff_field_does_not_sleep(settings_factory, load_stackoverflow_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("search_top_page1.json"))
    )

    sleeps: list[float] = []
    source = make_source(settings_factory, sleep_fn=sleeps.append)
    source.search_top("claude api", window="week", limit=50)

    assert sleeps == []


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_stackoverflow_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_stackoverflow_fixture("search_top_page1.json")),
        ]
    )

    sleeps: list[float] = []
    source = make_source(settings_factory, sleep_fn=sleeps.append)
    posts = source.search_top("claude api", window="week", limit=50)

    assert route.call_count == 2
    assert len(posts) == 2
    assert any(delay > 0 for delay in sleeps)


@respx.mock
def test_retries_exhausted_raises(settings_factory):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    source = make_source(settings_factory)

    with pytest.raises(StackOverflowAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1
