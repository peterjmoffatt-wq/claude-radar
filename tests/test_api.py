from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import radar.api as api_module
import radar.collect as collect_module
import radar.config as config_module
from radar.db import get_connection, init_db
from radar.sources.reddit import API_BASE, TOKEN_URL


@pytest.fixture
def client(settings_factory, monkeypatch):
    settings = settings_factory()
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    return TestClient(api_module.app), settings


def _seed_alert(
    conn,
    post_id: str,
    category: str = "product_bug",
    severity: str = "high",
    qa_status: str = "not_required",
    matched_term: str = "Claude API",
    author: str = "real_handle_42",
    likes: int = 17,
    comments: int = 4,
    score: int = 88,
    shares: int = 2,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, 1, ?, 'claude_api_general', ?, 'a summary', 'test-model', ?)
        """,
        (post_id, category, severity, now),
    )
    conn.execute(
        """
        INSERT INTO snapshots (
            post_id, platform, poll_run_id, collected_at, created_at, url, search_pass,
            matched_term, hashed_author, likes, comments, score, shares
        )
        VALUES (?, 'reddit', 'run-1', ?, ?, ?, 'top', ?, ?, ?, ?, ?, ?)
        """,
        (post_id, now, now, f"https://x/{post_id}", matched_term, author, likes, comments, score, shares),
    )
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES (?, ?, 100.0, 40.0, ?, ?, ?)
        """,
        (post_id, now, category, severity, qa_status),
    )
    conn.commit()


def _seed_classification_only(
    conn,
    post_id: str,
    is_pain_point: bool = True,
    category: str = "product_bug",
    platform: str = "reddit",
    matched_term: str = "Claude API",
    author: str = "real_handle_42",
    likes: int = 17,
    comments: int = 4,
    score: int = 88,
    shares: int = 2,
) -> None:
    """A classified post with no alert row -- e.g. not enough snapshot history
    yet to compute velocity, or velocity never crossed VELOCITY_THRESHOLD.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, ?, ?, 'claude_api_general', 'med', 'a summary', 'test-model', ?)
        """,
        (post_id, int(is_pain_point), category, now),
    )
    conn.execute(
        """
        INSERT INTO snapshots (
            post_id, platform, poll_run_id, collected_at, created_at, url, search_pass,
            matched_term, hashed_author, likes, comments, score, shares
        )
        VALUES (?, ?, 'run-1', ?, ?, ?, 'top', ?, ?, ?, ?, ?, ?)
        """,
        (post_id, platform, now, now, f"https://x/{post_id}", matched_term, author, likes, comments, score, shares),
    )
    conn.commit()


def test_api_alerts_returns_seeded_alert(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.get("/api/alerts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["post_id"] == "t3_a"
    assert body[0]["url"] == "https://x/t3_a"
    assert body[0]["platform"] == "reddit"
    assert body[0]["matched_term"] == "Claude API"
    assert body[0]["author"] == "real_handle_42"
    assert body[0]["likes"] == 17
    assert body[0]["comments"] == 4
    assert body[0]["score"] == 88
    assert body[0]["shares"] == 2
    assert body[0]["created_at"]


def test_api_alerts_filters_by_status(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_pending", qa_status="pending", category="abuse")
    _seed_alert(conn, "t3_ok", qa_status="not_required")
    conn.close()

    response = test_client.get("/api/alerts", params={"status": "pending"})

    body = response.json()
    assert len(body) == 1
    assert body[0]["post_id"] == "t3_pending"


def test_api_alerts_filters_by_category_and_severity(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_bug", category="product_bug", severity="high")
    _seed_alert(conn, "t3_ux", category="ux_confusion", severity="low")
    conn.close()

    response = test_client.get("/api/alerts", params={"category": "ux_confusion"})
    assert [a["post_id"] for a in response.json()] == ["t3_ux"]

    response = test_client.get("/api/alerts", params={"severity": "high"})
    assert [a["post_id"] for a in response.json()] == ["t3_bug"]


def test_api_clusters_groups_alerts(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", category="product_bug")
    _seed_alert(conn, "t3_b", category="product_bug")
    conn.close()

    response = test_client.get("/api/clusters")

    body = response.json()
    assert len(body) == 1
    assert body[0]["alert_count"] == 2


def test_api_watching_returns_pain_points_with_no_alert(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_classification_only(
        conn,
        "t3_unscored",
        is_pain_point=True,
        platform="youtube",
        matched_term="McDonald's jailbreak",
    )
    _seed_classification_only(conn, "t3_not_pain_point", is_pain_point=False)
    _seed_alert(conn, "t3_already_alerted", category="product_bug")
    conn.close()

    response = test_client.get("/api/watching")

    assert response.status_code == 200
    body = response.json()
    assert [row["post_id"] for row in body] == ["t3_unscored"]
    assert body[0]["platform"] == "youtube"
    assert body[0]["url"] == "https://x/t3_unscored"
    assert body[0]["matched_term"] == "McDonald's jailbreak"
    assert body[0]["author"] == "real_handle_42"
    assert body[0]["likes"] == 17
    assert body[0]["comments"] == 4
    assert body[0]["score"] == 88
    assert body[0]["shares"] == 2
    assert body[0]["created_at"]


def test_api_watching_empty_when_none_pending(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.get("/api/watching")

    assert response.json() == []


def test_api_stats_counts_advertisements(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, is_advertisement, category, model_implicated,
            severity, issue_summary, classifier_model, classified_at
        ) VALUES (?, 0, 1, 'other', 'claude_api_general', 'low', 'ad post', 'test-model', ?)
        """,
        ("t3_ad", now),
    )
    _seed_classification_only(conn, "t3_real", is_pain_point=True)
    conn.commit()
    conn.close()

    response = test_client.get("/api/stats")

    assert response.status_code == 200
    assert response.json() == {"ads_filtered": 1}


def test_api_lead_time_summary(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.close()

    response = test_client.get("/api/lead-time")

    assert response.status_code == 200
    body = response.json()
    assert body["posts_caught_early"] == 0
    assert body["lead_times_seconds"] == []


def test_api_lead_time_includes_sorted_positive_lead_times(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for post_id, recent_offset, top_offset in [("t3_a", 0, 30), ("t3_b", 0, 10)]:
        conn.execute(
            "INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass) "
            "VALUES (?, 'reddit', 'run-1', ?, ?, ?, 'recent')",
            (post_id, (base + timedelta(minutes=recent_offset)).isoformat(), base.isoformat(), f"https://x/{post_id}"),
        )
        conn.execute(
            "INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass) "
            "VALUES (?, 'reddit', 'run-1', ?, ?, ?, 'top')",
            (post_id, (base + timedelta(minutes=top_offset)).isoformat(), base.isoformat(), f"https://x/{post_id}"),
        )
    conn.commit()
    conn.close()

    response = test_client.get("/api/lead-time")

    body = response.json()
    # t3_b: 10 min lead time, t3_a: 30 min lead time -- sorted ascending.
    assert body["lead_times_seconds"] == [600.0, 1800.0]


def test_api_review_approve_updates_status(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", qa_status="pending", category="abuse")
    conn.close()

    response = test_client.post("/api/alerts/t3_a/review", json={"decision": "approved"})

    assert response.status_code == 200
    assert response.json() == {"post_id": "t3_a", "qa_status": "approved"}

    conn = get_connection(settings.database_path)
    status = conn.execute("SELECT qa_status FROM alerts WHERE post_id='t3_a'").fetchone()[0]
    conn.close()
    assert status == "approved"


def test_api_review_404_when_no_pending_alert(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.close()

    response = test_client.post("/api/alerts/nonexistent/review", json={"decision": "approved"})

    assert response.status_code == 404


def test_api_review_rejects_invalid_decision(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.close()

    response = test_client.post("/api/alerts/t3_a/review", json={"decision": "maybe"})

    assert response.status_code == 422


def test_connect_only_initializes_db_once_per_path(client, monkeypatch):
    test_client, _settings = client
    test_client.get("/api/watching")  # first request -- initializes

    calls = []
    monkeypatch.setattr(api_module, "init_db", lambda conn: calls.append(conn))
    test_client.get("/api/watching")  # second request, same path -- must skip

    assert calls == []


def test_static_index_served_at_root(client):
    test_client, _settings = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_api_sources_reflects_configured_state(settings_factory, monkeypatch):
    # settings_factory's defaults configure Reddit; explicitly enable Hacker News
    # too, and leave YouTube/Mastodon/etc. unconfigured.
    settings = settings_factory(enable_hackernews_source=True)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    test_client = TestClient(api_module.app)

    response = test_client.get("/api/sources")

    assert response.status_code == 200
    body = response.json()
    assert body["reddit"] is True
    assert body["hackernews"] is True
    assert body["youtube"] is False
    assert body["mastodon"] is False
    assert body["x"] is False


@respx.mock
def test_api_collect_runs_requested_source_and_reports_unconfigured_skip(
    settings_factory, load_reddit_fixture, monkeypatch
):
    monkeypatch.setattr(
        collect_module,
        "load_search_terms",
        lambda: {"subreddits": ["ClaudeAI"], "terms": ["claude down"]},
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("oauth_token.json"))
    )
    respx.get(f"{API_BASE}/r/ClaudeAI/search").mock(
        return_value=httpx.Response(200, json=load_reddit_fixture("search_top_page1.json"))
    )

    # Large poll interval so the "recent" pass doesn't filter out the 2023-dated
    # fixture posts -- same trick test_collect_integration.py uses.
    settings = settings_factory(poll_interval_seconds=2_000_000_000)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    test_client = TestClient(api_module.app)

    response = test_client.post("/api/collect", json={"sources": ["reddit", "mastodon"]})

    assert response.status_code == 200
    body = response.json()
    assert body["sources_run"] == ["reddit"]
    assert body["sources_skipped_unconfigured"] == ["mastodon"]
    assert body["snapshots_written"] == 4  # 2 Reddit posts x (top + recent)


def test_api_collect_defaults_to_every_configured_source(settings_factory, monkeypatch):
    settings = settings_factory(reddit_client_id="", reddit_client_secret="")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    test_client = TestClient(api_module.app)

    response = test_client.post("/api/collect", json={})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "snapshots_written": 0,
        "sources_run": [],
        "sources_skipped_unconfigured": [],
        "sources_failed": [],
    }


def test_api_get_search_terms_returns_current_config(tmp_path, monkeypatch):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text(
        "subreddits: []\nterms:\n  - Claude API\nclients:\n  - McDonald's\n"
        "risk_patterns:\n  - jailbreak\n"
    )
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )

    response = TestClient(api_module.app).get("/api/search-terms")

    assert response.status_code == 200
    body = response.json()
    assert body["terms"] == ["Claude API"]
    assert body["clients"] == ["McDonald's"]
    assert body["risk_patterns"] == ["jailbreak"]
    assert body["effective_terms"] == ["Claude API", "McDonald's jailbreak"]
    assert body["max_items"] == 10


def test_api_put_search_terms_persists_to_disk(tmp_path, monkeypatch):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text("subreddits:\n  - ClaudeAI\nterms: []\nclients: []\nrisk_patterns: []\n")
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )
    monkeypatch.setattr(
        api_module,
        "save_search_terms",
        lambda updates: config_module.save_search_terms(updates, path=yaml_path),
    )
    test_client = TestClient(api_module.app)

    response = test_client.put(
        "/api/search-terms",
        json={"terms": ["new term"], "clients": ["McDonald's"], "risk_patterns": ["jailbreak"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["terms"] == ["new term"]
    assert body["clients"] == ["McDonald's"]
    assert body["effective_terms"] == ["new term", "McDonald's jailbreak"]

    # Persisted to disk, not just held in memory for the response -- confirm
    # via an independent read, and that `subreddits` (not part of the PUT
    # body) survived untouched.
    reloaded = config_module.load_search_terms(yaml_path)
    assert reloaded["subreddits"] == ["ClaudeAI"]
    assert reloaded["terms"] == ["new term"]


def test_api_put_search_terms_rejects_over_the_cap(tmp_path, monkeypatch):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text("subreddits: []\nterms: []\nclients: []\nrisk_patterns: []\n")
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )
    monkeypatch.setattr(
        api_module,
        "save_search_terms",
        lambda updates: config_module.save_search_terms(updates, path=yaml_path),
    )
    test_client = TestClient(api_module.app)

    response = test_client.put(
        "/api/search-terms",
        json={"terms": [f"term{i}" for i in range(11)], "clients": [], "risk_patterns": []},
    )

    assert response.status_code == 400
    # Rejected before writing -- the file on disk is unaffected.
    assert config_module.load_search_terms(yaml_path)["terms"] == []
