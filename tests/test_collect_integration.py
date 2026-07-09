from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx

import radar.collect as collect_module
from radar.db import get_connection, init_db
from radar.sources.github import SEARCH_URL as GITHUB_SEARCH_URL
from radar.sources.hackernews import SEARCH_URL as HN_SEARCH_URL
from radar.sources.hackernews import SEARCH_BY_DATE_URL as HN_SEARCH_BY_DATE_URL
from radar.sources.reddit import API_BASE, TOKEN_URL, RedditSource
from radar.sources.stackoverflow import SEARCH_URL as SO_SEARCH_URL
from radar.sources.youtube import SEARCH_URL as YOUTUBE_SEARCH_URL
from radar.sources.youtube import VIDEOS_URL as YOUTUBE_VIDEOS_URL

# Fixture posts are dated 2023 (search_top_page1.json). A large poll interval pushes
# `since` far enough into the past that the "recent" pass doesn't filter them out --
# keeps these tests decoupled from real wall-clock time. Kept well under the ~2000-year
# range that would overflow datetime's year-1 floor.
_HUGE_POLL_INTERVAL = 2_000_000_000


def _mock_reddit(load_reddit_fixture, search_fixture="search_top_page1.json"):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("oauth_token.json"))
    )
    return respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture(search_fixture))
    )


def _single_term_config():
    return {"subreddits": ["ClaudeAI"], "terms": ["claude down"]}


@respx.mock
def test_run_collection_queries_client_risk_pattern_combinations(
    settings_factory, load_reddit_fixture, monkeypatch
):
    # Proves the client-scope feature is a real, live search -- not just
    # computed and discarded -- by asserting the combined string actually
    # reaches the HTTP request as the `q` param. Client-scoped terms search
    # site-wide (not the configured subreddits) -- see the site-wide-vs-
    # subreddit-scoped test below for that distinction specifically.
    monkeypatch.setattr(
        collect_module,
        "load_search_terms",
        lambda: {
            "subreddits": ["ClaudeAI"],
            "terms": [],
            "clients": ["McDonald's"],
            "risk_patterns": ["jailbreak"],
        },
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("oauth_token.json"))
    )
    route = respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_top_page1.json"))
    )

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert result.snapshots_written == 4  # 2 posts x (top + recent), one effective term
    queried_terms = {call.request.url.params["q"] for call in route.calls}
    assert queried_terms == {"McDonald's jailbreak"}


@respx.mock
def test_run_collection_reddit_generic_term_stays_subreddit_scoped_client_term_goes_site_wide(
    settings_factory, load_reddit_fixture, monkeypatch
):
    monkeypatch.setattr(
        collect_module,
        "load_search_terms",
        lambda: {
            "subreddits": ["ClaudeAI"],
            "terms": ["claude down"],
            "clients": ["McDonald's"],
            "risk_patterns": ["jailbreak"],
        },
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("oauth_token.json"))
    )
    subreddit_route = respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_top_page1.json"))
    )
    site_wide_route = respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_top_page1.json"))
    )

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert {call.request.url.params["q"] for call in subreddit_route.calls} == {"claude down"}
    assert {call.request.url.params["q"] for call in site_wide_route.calls} == {"McDonald's jailbreak"}


@respx.mock
def test_run_collection_writes_expected_rows(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert result.skipped is False
    assert result.snapshots_written == 4  # 2 posts x (top + recent) passes

    conn = get_connection(settings.database_path)
    rows = conn.execute("SELECT search_pass FROM snapshots").fetchall()
    conn.close()
    assert len(rows) == 4
    assert {row[0] for row in rows} == {"top", "recent"}


@respx.mock
def test_author_hashing_enabled_by_default(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    conn = get_connection(settings.database_path)
    hashed_value = conn.execute(
        "SELECT hashed_author FROM snapshots WHERE post_id='t3_top1' AND search_pass='top'"
    ).fetchone()[0]
    conn.close()

    assert hashed_value != "alice123"
    assert len(hashed_value) == 64


@respx.mock
def test_author_hashing_disabled_stores_raw_handle(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL, hash_authors=False)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    conn = get_connection(settings.database_path)
    raw_value = conn.execute(
        "SELECT hashed_author FROM snapshots WHERE post_id='t3_top1' AND search_pass='top'"
    ).fetchone()[0]
    conn.close()

    assert raw_value == "alice123"


@respx.mock
def test_virality_score_matches_formula(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    conn = get_connection(settings.database_path)
    row = conn.execute(
        "SELECT score, comments, likes, shares, virality_score FROM snapshots "
        "WHERE post_id='t3_top1' AND search_pass='top'"
    ).fetchone()
    conn.close()

    score, comments, likes, shares, virality = row
    assert virality == score + comments * 2 + likes + shares


@respx.mock
def test_within_run_dedupe_across_terms(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(
        collect_module,
        "load_search_terms",
        lambda: {"subreddits": ["ClaudeAI"], "terms": ["claude down", "claude api"]},
    )
    _mock_reddit(load_reddit_fixture)  # same 2 posts regardless of which term queries them

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    # Still 2 posts x 2 passes -- the second term re-surfacing the same posts
    # must not double-count them within a single run.
    assert result.snapshots_written == 4


@respx.mock
def test_two_runs_produce_time_series_rows(settings_factory, load_reddit_fixture, monkeypatch):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=_HUGE_POLL_INTERVAL)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)
    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    conn = get_connection(settings.database_path)
    rows = conn.execute(
        "SELECT poll_run_id FROM snapshots WHERE post_id='t3_top1' AND search_pass='top'"
    ).fetchall()
    conn.close()

    run_ids = {row[0] for row in rows}
    assert len(rows) == 2
    assert len(run_ids) == 2


@respx.mock
def test_since_uses_last_real_collection_not_configured_interval(settings_factory, load_reddit_fixture, monkeypatch):
    # A small interval so "now - interval" would land recently -- but seed a
    # snapshot from well before that, simulating a missed/late collection run
    # (laptop asleep, cron skipped a tick). `since` must reflect the real
    # last collection, not silently narrow the gap to just the interval.
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)

    settings = settings_factory(poll_interval_seconds=3600)  # 1 hour
    old_collected_at = datetime.now(timezone.utc) - timedelta(days=3)
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass) "
        "VALUES ('seed', 'reddit', 'seed-run', ?, ?, 'https://x/seed', 'top')",
        (old_collected_at.isoformat(), old_collected_at.isoformat()),
    )
    conn.commit()
    conn.close()

    captured_since = []
    real_search_recent = RedditSource.search_recent

    def spy_search_recent(self, query, since, limit=50, **kwargs):
        captured_since.append(since)
        return real_search_recent(self, query, since, limit, **kwargs)

    monkeypatch.setattr(RedditSource, "search_recent", spy_search_recent)

    collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert len(captured_since) == 1
    assert abs((captured_since[0] - old_collected_at).total_seconds()) < 2


@respx.mock
def test_run_collection_noop_when_credentials_missing(settings_factory):
    settings = settings_factory(reddit_client_id="", reddit_client_secret="")

    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert result.skipped is True
    assert result.snapshots_written == 0

    conn = get_connection(settings.database_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert tables == []


@respx.mock
def test_run_collection_combines_multiple_configured_sources(
    settings_factory, load_reddit_fixture, load_youtube_fixture, monkeypatch
):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("search_top_page1.json"))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )

    settings = settings_factory(
        poll_interval_seconds=_HUGE_POLL_INTERVAL, youtube_api_key="test-youtube-key"
    )
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    # 2 Reddit posts + 2 YouTube videos, each collected in both the top and recent pass.
    assert result.snapshots_written == 8

    conn = get_connection(settings.database_path)
    platforms = {
        row[0] for row in conn.execute("SELECT DISTINCT platform FROM snapshots").fetchall()
    }
    conn.close()
    assert platforms == {"reddit", "youtube"}


@respx.mock
def test_run_collection_isolates_a_failing_source_from_the_rest(
    settings_factory, load_reddit_fixture, monkeypatch
):
    # Reddit succeeds; YouTube exhausts its retries and errors out. Found via
    # live testing: a single flaky upstream (e.g. a real HN timeout) must not
    # take down every other configured source's results in the same run,
    # especially now that this is reachable from a synchronous, user-facing
    # button click (POST /api/collect).
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)
    respx.get(YOUTUBE_SEARCH_URL).mock(return_value=httpx.Response(503))

    settings = settings_factory(
        poll_interval_seconds=_HUGE_POLL_INTERVAL, youtube_api_key="test-youtube-key"
    )
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert result.skipped is False
    assert set(result.sources_run) == {"reddit", "youtube"}
    assert result.sources_failed == ["youtube"]
    assert result.snapshots_written == 4  # Reddit's 2 posts x (top + recent) only

    conn = get_connection(settings.database_path)
    platforms = {
        row[0] for row in conn.execute("SELECT DISTINCT platform FROM snapshots").fetchall()
    }
    conn.close()
    assert platforms == {"reddit"}


@respx.mock
def test_run_collection_sources_filter_narrows_which_configured_sources_run(
    settings_factory, load_reddit_fixture, load_youtube_fixture, monkeypatch
):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("search_top_page1.json"))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )

    settings = settings_factory(
        poll_interval_seconds=_HUGE_POLL_INTERVAL, youtube_api_key="test-youtube-key"
    )
    # Both Reddit and YouTube are configured, but only Reddit is requested.
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None, sources={"reddit"})

    assert result.sources_run == ["reddit"]
    assert result.snapshots_written == 4  # 2 Reddit posts x (top + recent), no YouTube

    conn = get_connection(settings.database_path)
    platforms = {
        row[0] for row in conn.execute("SELECT DISTINCT platform FROM snapshots").fetchall()
    }
    conn.close()
    assert platforms == {"reddit"}


@respx.mock
def test_run_collection_combines_all_five_sources(
    settings_factory,
    load_reddit_fixture,
    load_youtube_fixture,
    load_hackernews_fixture,
    load_stackoverflow_fixture,
    load_github_fixture,
    monkeypatch,
):
    monkeypatch.setattr(collect_module, "load_search_terms", _single_term_config)
    _mock_reddit(load_reddit_fixture)
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("search_top_page1.json"))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_youtube_fixture("videos_statistics.json"))
    )
    respx.get(HN_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_hackernews_fixture("search_top_page1.json"))
    )
    respx.get(HN_SEARCH_BY_DATE_URL).mock(
        return_value=httpx.Response(200, json=load_hackernews_fixture("search_top_page1.json"))
    )
    respx.get(SO_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_stackoverflow_fixture("search_top_page1.json"))
    )
    respx.get(GITHUB_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_github_fixture("search_top_page1.json"))
    )

    settings = settings_factory(
        poll_interval_seconds=_HUGE_POLL_INTERVAL,
        youtube_api_key="test-youtube-key",
        enable_hackernews_source=True,
        enable_stackoverflow_source=True,
        github_token="test-github-token",
    )
    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    # 2 posts each from reddit/youtube/hackernews/stackoverflow, 2 from github --
    # every source's fixture has 2 items, x (top + recent) passes x 5 sources.
    assert result.snapshots_written == 20

    conn = get_connection(settings.database_path)
    platforms = {
        row[0] for row in conn.execute("SELECT DISTINCT platform FROM snapshots").fetchall()
    }
    conn.close()
    assert platforms == {"reddit", "youtube", "hackernews", "stackoverflow", "github"}
