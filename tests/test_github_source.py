from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.sources.github import (
    SEARCH_URL,
    CredentialsMissingError,
    GitHubAPIError,
    GitHubSource,
)


def make_source(settings_factory, sleep_fn=None, **overrides):
    overrides.setdefault("github_token", "test-github-token")
    settings = settings_factory(**overrides)
    return GitHubSource(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_search_top_maps_fixture_to_rawpost(settings_factory, load_github_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert len(posts) == 2
    first = posts[0]
    assert first.id == "gh_111"
    assert first.platform.value == "github"
    assert first.author == "gh_user_1"
    assert first.matched_term == "claude api"
    assert first.metrics.score == 10
    assert first.metrics.comments == 5
    assert first.url == "https://github.com/someorg/somerepo/issues/42"
    assert first.created_at.tzinfo is not None
    assert "429s" in first.text


@respx.mock
def test_search_recent_filters_by_since(settings_factory, load_github_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("search_top_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2023, 11, 2, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert len(posts) == 1
    assert posts[0].id == "gh_112"


@respx.mock
def test_search_recent_aggregates_multiple_pages_via_link_header(
    settings_factory, load_github_fixture
):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=load_github_fixture("search_recent_page1.json"),
                headers={
                    "link": '<https://api.github.com/search/issues?page=2>; rel="next"'
                },
            ),
            httpx.Response(200, json=load_github_fixture("search_recent_page2.json")),
        ]
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    posts = source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 2
    assert [p.id for p in posts] == ["gh_211", "gh_212"]


@respx.mock
def test_no_next_link_header_stops_pagination(settings_factory, load_github_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("search_recent_page1.json"))
    )

    source = make_source(settings_factory)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    source.search_recent("claude down", since=since, limit=50)

    assert route.call_count == 1


@respx.mock
def test_no_results_returns_empty_list(settings_factory, load_github_fixture):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("empty_results.json"))
    )

    source = make_source(settings_factory)
    posts = source.search_top("claude api", window="week", limit=50)

    assert posts == []


@respx.mock
def test_missing_credentials_raises_before_any_http_call(settings_factory):
    source = make_source(settings_factory, github_token="")

    with pytest.raises(CredentialsMissingError):
        source.search_top("claude api", window="week", limit=50)


@respx.mock
def test_sends_bearer_token_and_api_version_headers(settings_factory, load_github_fixture):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("empty_results.json"))
    )

    source = make_source(settings_factory, github_token="my-token")
    source.search_top("claude api", window="week", limit=50)

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer my-token"
    assert sent.headers["X-GitHub-Api-Version"] == "2022-11-28"


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_github_fixture):
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_github_fixture("search_top_page1.json")),
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

    with pytest.raises(GitHubAPIError):
        source.search_top("claude api", window="week", limit=50)

    assert route.call_count > 1
