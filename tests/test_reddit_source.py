from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.reddit import (
    API_BASE,
    TOKEN_URL,
    CredentialsMissingError,
    RedditAPIError,
    RedditSource,
)


def make_source(settings_factory, subreddits=("ClaudeAI",), sleep_fn=None, **overrides):
    settings = settings_factory(**overrides)
    return RedditSource(
        settings,
        subreddits=list(subreddits),
        sleep_fn=sleep_fn or (lambda s: None),
    )


def mock_token_route(load_reddit_fixture):
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("oauth_token.json"))
    )


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "t3_top1"
    assert first.platform.value == "reddit"
    assert first.author == "alice123"
    assert first.subreddit == "ClaudeAI"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 150
    assert first.metrics.comments == 42
    assert first.url == "https://www.reddit.com/r/ClaudeAI/comments/top1/claude_api_rate_limits/"
    assert first.created_at.tzinfo is not None

    second = posts[1]
    assert second.text.startswith("Anyone else hitting Claude API errors?")
    assert "529s" in second.text


@respx.mock
def test_search_recent_stops_paginating_when_page_crosses_since(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    route = respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_new_page1.json"))
    )

    source = make_source(settings_factory)
    # Between the two posts in page 1 -- the page's oldest post predates `since`,
    # so pagination should stop without ever requesting page 2.
    since = datetime.fromtimestamp(1_999_500_000, tz=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 1
    assert len(posts) == 1
    assert posts[0].id == "t3_new1"


@respx.mock
def test_search_recent_aggregates_multiple_pages(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    route = respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        side_effect=[
            httpx.Response(200, json=load_reddit_fixture("search_new_page1.json")),
            httpx.Response(200, json=load_reddit_fixture("search_new_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime.fromtimestamp(500_000_000, tz=timezone.utc)  # older than every fixture post
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["t3_new1", "t3_new2", "t3_new3", "t3_new4"]


@respx.mock
def test_access_token_cached_across_calls(settings_factory, load_reddit_fixture):
    token_route = mock_token_route(load_reddit_fixture)
    respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    source.search_top("claude api", window="week", limit=50)
    source.search_top("claude code", window="week", limit=50)

    assert token_route.call_count == 1


@respx.mock
def test_access_token_refreshed_after_expiry(settings_factory, load_reddit_fixture):
    token_route = mock_token_route(load_reddit_fixture)

    source = make_source(settings_factory)
    source._get_access_token()
    assert token_route.call_count == 1

    source._token_expires_at = time.monotonic() - 1  # force expiry
    source._get_access_token()
    assert token_route.call_count == 2


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    route = respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_reddit_fixture("search_top_page1.json")),
        ]
    )

    sleeps: list[float] = []
    source = make_source(settings_factory, sleep_fn=sleeps.append)
    posts = source.search_top("claude api", window="week", limit=50)

    assert route.call_count == 2
    assert len(posts) == 2
    assert any(delay > 0 for delay in sleeps)


@respx.mock
def test_retries_exhausted_raises(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    route = respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(return_value=httpx.Response(503))

    source = make_source(settings_factory)

    with pytest.raises(RedditAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1


@respx.mock
def test_missing_credentials_raises_before_any_http_call(settings_factory):
    source = make_source(settings_factory, reddit_client_id="", reddit_client_secret="")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_combined_subreddit_path_and_user_agent(settings_factory, load_reddit_fixture):
    mock_token_route(load_reddit_fixture)
    route = respx.get(f"{API_BASE}/r/ClaudeAI+Anthropic/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("empty_results.json"))
    )

    source = make_source(
        settings_factory,
        subreddits=("ClaudeAI", "Anthropic"),
        reddit_user_agent="my-agent/9.9",
    )
    source.search_top("claude api", window="week", limit=50)

    assert route.call_count == 1
    sent_request = route.calls.last.request
    assert sent_request.headers["User-Agent"] == "my-agent/9.9"
