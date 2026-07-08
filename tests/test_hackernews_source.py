from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.hackernews import (
    SEARCH_BY_DATE_URL,
    SEARCH_URL,
    HackerNewsAPIError,
    HackerNewsSource,
)


def make_source(settings_factory, sleep_fn=None, **overrides):
    settings = settings_factory(**overrides)
    return HackerNewsSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_hackernews_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_hackernews_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "hn_1001"
    assert first.platform.value == "hackernews"
    assert first.author == "alice_hn"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 250
    assert first.metrics.comments == 80
    assert first.url == "https://example.com/claude-rate-limits"
    assert first.created_at.tzinfo is not None

    second = posts[1]
    assert second.url == "https://news.ycombinator.com/item?id=1002"  # no url in fixture -> HN link
    assert "529s" in second.text


@respx.mock
def test_search_recent_filters_by_since(settings_factory, load_hackernews_fixture):
    respx.get(SEARCH_BY_DATE_URL).mock(
        return_value=httpx.Response(200, json=load_hackernews_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2023, 11, 2, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert len(posts) == 1
    assert posts[0].id == "hn_1002"


@respx.mock
def test_search_recent_aggregates_multiple_pages(settings_factory, load_hackernews_fixture):
    route = respx.get(SEARCH_BY_DATE_URL).mock(
        side_effect=[
            httpx.Response(
                200, json=load_hackernews_fixture("search_recent_page1_with_more.json")
            ),
            httpx.Response(200, json=load_hackernews_fixture("search_recent_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["hn_2001", "hn_2002"]


@respx.mock
def test_no_results_returns_empty_list(settings_factory, load_hackernews_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_hackernews_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_hackernews_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_hackernews_fixture("search_top_page1.json")),
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

    with pytest.raises(HackerNewsAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1
