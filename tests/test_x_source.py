from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.x import SEARCH_URL, CredentialsMissingError, XAPIError, XSource


def make_source(settings_factory, sleep_fn=None, **overrides):
    overrides.setdefault("enable_x_source", True)
    overrides.setdefault("x_bearer_token", "test-bearer-token")
    settings = settings_factory(**overrides)
    return XSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_x_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_x_fixture("search_recent_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "x_1001"
    assert first.platform.value == "x"
    assert first.author == "555"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 10  # retweet_count
    assert first.metrics.likes == 40
    assert first.metrics.comments == 5
    assert first.metrics.shares == 2
    assert first.url == "https://x.com/i/web/status/1001"
    assert first.created_at.tzinfo is not None


@respx.mock
def test_uses_real_username_when_includes_users_present(settings_factory, load_x_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_x_fixture("search_with_username.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts[0].author == "claude_watcher"
    sent_params = dict(httpx.QueryParams(route.calls.last.request.url.query))
    assert sent_params["expansions"] == "author_id"
    assert sent_params["user.fields"] == "username"


@respx.mock
def test_falls_back_to_author_id_when_no_matching_user_included(settings_factory, load_x_fixture):
    # search_recent_page1.json has no `includes.users` at all -- must not
    # crash, and must fall back to the raw author_id like before this fix.
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_x_fixture("search_recent_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts[0].author == "555"


@respx.mock
def test_search_recent_filters_by_since(settings_factory, load_x_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_x_fixture("search_recent_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2023, 11, 2, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert len(posts) == 1
    assert posts[0].id == "x_1002"


@respx.mock
def test_search_recent_aggregates_multiple_pages(settings_factory, load_x_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=load_x_fixture("search_page1_with_token.json")),
            httpx.Response(200, json=load_x_fixture("search_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["x_2001", "x_2002"]


@respx.mock
def test_no_results_returns_empty_list(settings_factory, load_x_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_x_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []


@respx.mock
def test_missing_bearer_token_raises_before_any_http_call(settings_factory):
    source = make_source(settings_factory, x_bearer_token="")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_feature_flag_off_raises_even_with_a_token_configured(settings_factory):
    # True feature-flag behavior: a token alone isn't enough, ENABLE_X_SOURCE must be on too.
    source = make_source(settings_factory, enable_x_source=False, x_bearer_token="some-token")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_x_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_x_fixture("search_recent_page1.json")),
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

    with pytest.raises(XAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1


@respx.mock
def test_connection_timeout_raises_x_api_error_not_raw_httpx_error(settings_factory):
    # A transport-level failure must convert to XAPIError the same way an
    # HTTP error status does -- previously this only caught
    # httpx.HTTPStatusError, so a timeout would escape as a raw httpx exception.
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    source = make_source(settings_factory)

    with pytest.raises(XAPIError):
        source.search_top("claude api", window="week", limit=50)
