from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.mastodon import (
    CredentialsMissingError,
    MastodonAPIError,
    MastodonSource,
)

SEARCH_URL = "https://mastodon.social/api/v2/search"


def make_source(settings_factory, sleep_fn=None, **overrides):
    overrides.setdefault("mastodon_instance_url", "https://mastodon.social")
    overrides.setdefault("mastodon_access_token", "test-mastodon-token")
    settings = settings_factory(**overrides)
    return MastodonSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_sorts_by_engagement_and_maps_fixture(settings_factory, load_mastodon_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_mastodon_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    # No native engagement sort exists on the Mastodon search endpoint -- the
    # source must sort client-side by (favourites + reblogs) descending.
    assert [p.id for p in posts] == ["mastodon_1002", "mastodon_1001", "mastodon_1003"]

    top = posts[0]
    assert top.platform.value == "mastodon"
    assert top.author == "top_engagement@mastodon.social"
    assert top.matched_term == "claude api"
    assert top.metrics.likes == 20
    assert top.metrics.shares == 10
    assert top.url == "https://mastodon.social/@top_engagement/1002"
    assert top.created_at.tzinfo is not None
    # HTML content is stripped, not stored raw.
    assert "<strong>" not in posts[1].text
    assert "great" in posts[1].text


@respx.mock
def test_no_results_returns_empty_list(settings_factory, load_mastodon_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_mastodon_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []


@respx.mock
def test_search_recent_aggregates_full_page_then_stops_on_since(
    settings_factory, load_mastodon_fixture
):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200, json=load_mastodon_fixture("search_recent_page1_with_more.json")
            ),
            httpx.Response(200, json=load_mastodon_fixture("search_recent_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime(2023, 11, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude api", since=since, limit=45)

    # Page 1 (40 posts, all after `since`) looks "full" so pagination continues;
    # page 2's posts all predate `since`, so they're fetched (to confirm the
    # request happened) but filtered out of the final result.
    assert route.call_count == 2
    assert len(posts) == 40
    assert all(p.created_at >= since for p in posts)

    first_request_params = route.calls[0].request.url.params
    second_request_params = route.calls[1].request.url.params
    assert first_request_params["offset"] == "0"
    assert second_request_params["offset"] == "40"


@respx.mock
def test_search_recent_stops_without_second_page_when_page_is_short(
    settings_factory, load_mastodon_fixture
):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_mastodon_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    source.search_recent("claude api", since=since, limit=50)

    # Only 3 statuses returned against a page request for up to 40 -- a short
    # page is itself the "no more results" signal (Mastodon's search endpoint
    # has no has_more/total field to check instead).
    assert route.call_count == 1


@respx.mock
def test_missing_credentials_raises_before_any_http_call(settings_factory):
    source = make_source(settings_factory, mastodon_instance_url="", mastodon_access_token="")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_sends_bearer_token_to_configured_instance(settings_factory, load_mastodon_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_mastodon_fixture("empty_results.json"))
    )

    source = make_source(
        settings_factory,
        mastodon_instance_url="https://mastodon.social",
        mastodon_access_token="my-token",
    )
    source.search_top("claude api", window="week", limit=50)

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer my-token"


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_mastodon_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_mastodon_fixture("search_top_page1.json")),
        ]
    )

    sleeps: list[float] = []
    source = make_source(settings_factory, sleep_fn=sleeps.append)
    posts = source.search_top("claude api", window="week", limit=50)

    assert route.call_count == 2
    assert len(posts) == 3
    assert any(delay > 0 for delay in sleeps)


@respx.mock
def test_retries_exhausted_raises(settings_factory):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    source = make_source(settings_factory)

    with pytest.raises(MastodonAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1
