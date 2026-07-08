from __future__ import annotations

import httpx
import respx

import radar.collect as collect_module
from radar.db import get_connection
from radar.sources.reddit import API_BASE, TOKEN_URL

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
def test_run_collection_noop_when_credentials_missing(settings_factory):
    settings = settings_factory(reddit_client_id="", reddit_client_secret="")

    result = collect_module.run_collection(settings, sleep_fn=lambda s: None)

    assert result.skipped is True
    assert result.snapshots_written == 0

    conn = get_connection(settings.database_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert tables == []
