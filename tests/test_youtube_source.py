from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.youtube import (
    SEARCH_URL,
    VIDEOS_URL,
    CredentialsMissingError,
    YouTubeAPIError,
    YouTubeSource,
)


def make_source(settings_factory, sleep_fn=None, **overrides):
    overrides.setdefault("youtube_api_key", "test-youtube-key")
    settings = settings_factory(**overrides)
    return YouTubeSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_youtube_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("search_top_page1.json"))
    )
    respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "yt_vid1"
    assert first.platform.value == "youtube"
    assert first.author == "TechChannel"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 5000
    assert first.metrics.likes == 300
    assert first.metrics.comments == 120
    assert first.url == "https://www.youtube.com/watch?v=vid1"
    assert first.created_at.tzinfo is not None
    assert "529 errors" in first.text


@respx.mock
def test_search_recent_filters_by_since(settings_factory, load_youtube_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("search_top_page1.json"))
    )
    respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )

    source = make_source(settings_factory)
    # vid1 published 2023-11-01T12:00, vid2 2023-11-02T08:30 -- since excludes vid1.
    since = datetime(2023, 11, 2, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert len(posts) == 1
    assert posts[0].id == "yt_vid2"


@respx.mock
def test_search_recent_aggregates_multiple_pages(settings_factory, load_youtube_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200, json=load_youtube_fixture("search_recent_page1_with_token.json")
            ),
            httpx.Response(200, json=load_youtube_fixture("search_recent_page2.json")),
        ]
    )
    respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["yt_vid1", "yt_vid2"]


@respx.mock
def test_missing_credentials_raises_before_any_http_call(settings_factory):
    source = make_source(settings_factory, youtube_api_key="")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_no_results_returns_empty_list_without_calling_videos_endpoint(
    settings_factory, load_youtube_fixture
):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("empty_results.json"))
    )
    videos_route = respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []
    assert videos_route.call_count == 0


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_youtube_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_youtube_fixture("search_top_page1.json")),
        ]
    )
    respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
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

    with pytest.raises(YouTubeAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1


@respx.mock
def test_connection_timeout_raises_youtube_api_error_not_raw_httpx_error(settings_factory):
    # A transport-level failure must convert to YouTubeAPIError the same way
    # an HTTP error status does -- previously this only caught
    # httpx.HTTPStatusError, so a timeout would escape as a raw httpx exception.
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    source = make_source(settings_factory)

    with pytest.raises(YouTubeAPIError):
        source.search_top("claude api", window="week", limit=50)
