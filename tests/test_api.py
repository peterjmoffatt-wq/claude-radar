from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import radar.api as api_module
import radar.collect as collect_module
import radar.config as config_module
from radar.classify import API_URL as ANTHROPIC_API_URL
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
    model_implicated: str = "claude_api_general",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, 1, ?, ?, ?, 'a summary', 'test-model', ?)
        """,
        (post_id, category, model_implicated, severity, now),
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


def test_api_alerts_includes_incident_and_brief_fields(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.get("/api/alerts")

    body = response.json()[0]
    assert body["incident_status"] == "open"
    assert body["exec_brief"] is None
    assert body["exec_brief_generated_at"] is None
    assert body["incident_report"] is None
    assert body["incident_report_generated_at"] is None


def test_api_review_reject_auto_closes_incident_as_false_positive(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", qa_status="pending", category="abuse")
    conn.close()

    response = test_client.post("/api/alerts/t3_a/review", json={"decision": "rejected"})

    assert response.status_code == 200
    conn = get_connection(settings.database_path)
    incident_status = conn.execute(
        "SELECT incident_status FROM alerts WHERE post_id='t3_a'"
    ).fetchone()[0]
    conn.close()
    assert incident_status == "false_positive"


def test_api_review_approve_does_not_touch_incident_status(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", qa_status="pending", category="abuse")
    conn.close()

    test_client.post("/api/alerts/t3_a/review", json={"decision": "approved"})

    conn = get_connection(settings.database_path)
    incident_status = conn.execute(
        "SELECT incident_status FROM alerts WHERE post_id='t3_a'"
    ).fetchone()[0]
    conn.close()
    assert incident_status == "open"


def test_api_alert_transition_updates_status(settings_factory, monkeypatch):
    # No ANTHROPIC_API_KEY -- COA generation falls back to the (fast,
    # network-free) template path, keeping this test deterministic; the
    # Claude-generated-COA behavior itself is covered by dedicated tests below.
    settings = settings_factory(anthropic_api_key="")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    test_client = TestClient(api_module.app)
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.post(
        "/api/alerts/t3_a/transition", json={"status": "acknowledged", "note": "Looking into it"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["post_id"] == "t3_a"
    assert body["incident_status"] == "acknowledged"
    assert body["coa"]  # template fallback still produces something


def test_api_alert_transition_404_when_no_alert(client):
    test_client, _settings = client
    response = test_client.post("/api/alerts/nonexistent/transition", json={"status": "acknowledged"})
    assert response.status_code == 404


def test_api_alert_transition_rejects_invalid_status(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.post("/api/alerts/t3_a/transition", json={"status": "not_a_real_status"})

    assert response.status_code == 422


@respx.mock
def test_api_alert_timeline_returns_events_in_order(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()
    # Not testing COA content here -- a 200 response (rather than a 4xx/5xx,
    # which would trigger RateLimitedClient's real exponential-backoff sleep
    # even though the network call itself is mocked) keeps this test fast.
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )

    test_client.post("/api/alerts/t3_a/transition", json={"status": "acknowledged"})
    test_client.post("/api/alerts/t3_a/transition", json={"status": "resolved", "note": "Fixed"})

    response = test_client.get("/api/alerts/t3_a/timeline")

    assert response.status_code == 200
    body = response.json()
    assert [(e["from_status"], e["to_status"]) for e in body] == [
        ("open", "acknowledged"),
        ("acknowledged", "resolved"),
    ]
    assert body[1]["note"] == "Fixed"


@respx.mock
def test_api_alert_brief_generates_and_persists(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )

    response = test_client.post("/api/alerts/t3_a/brief")

    assert response.status_code == 200
    body = response.json()
    assert body["post_id"] == "t3_a"
    assert "Recommend eng triage" in body["brief"]

    conn = get_connection(settings.database_path)
    stored = conn.execute("SELECT exec_brief FROM alerts WHERE post_id='t3_a'").fetchone()[0]
    conn.close()
    assert stored == body["brief"]


def test_api_alert_brief_404_when_no_alert(client):
    test_client, _settings = client
    response = test_client.post("/api/alerts/nonexistent/brief")
    assert response.status_code == 404


@respx.mock
def test_api_cluster_brief_generates_and_persists(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", category="product_bug")
    _seed_alert(conn, "t3_b", category="product_bug")
    conn.close()
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )

    cluster_key = "product_bug:claude_api_general"
    response = test_client.post(f"/api/clusters/{cluster_key}/brief")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_key"] == cluster_key
    assert "Recommend eng triage" in body["brief"]

    # /api/clusters now surfaces the persisted brief.
    clusters_response = test_client.get("/api/clusters")
    cluster = next(c for c in clusters_response.json() if c["cluster_key"] == cluster_key)
    assert cluster["brief"] == body["brief"]


def test_api_cluster_brief_404_when_cluster_not_found(client):
    test_client, _settings = client
    response = test_client.post("/api/clusters/product_bug:unknown/brief")
    assert response.status_code == 404


@respx.mock
def test_api_alert_report_generates_and_persists(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )
    test_client.post("/api/alerts/t3_a/transition", json={"status": "resolved", "note": "Fixed upstream"})

    response = test_client.post("/api/alerts/t3_a/report", json={"closing_note": "Add a regression test."})

    assert response.status_code == 200
    body = response.json()
    assert "# Post-incident report" in body["report_markdown"]
    assert "Add a regression test." in body["report_markdown"]
    assert "Fixed upstream" in body["report_markdown"]

    conn = get_connection(settings.database_path)
    stored = conn.execute("SELECT incident_report FROM alerts WHERE post_id='t3_a'").fetchone()[0]
    conn.close()
    assert stored == body["report_markdown"]


def test_api_clusters_includes_recurrence_and_brief_fields(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", category="product_bug")
    conn.close()

    response = test_client.get("/api/clusters")

    body = response.json()[0]
    assert body["episode_count"] == 1
    assert body["first_triggered_at"]
    assert body["platforms"] == ["reddit"]
    assert body["brief"] is None


def test_api_get_escalation_criteria_returns_defaults(client):
    test_client, _settings = client
    response = test_client.get("/api/escalation-criteria")

    assert response.status_code == 200
    categories = response.json()["categories"]
    assert categories["safety"]["requires_qa"] is True
    assert categories["product_bug"]["requires_qa"] is False
    assert set(categories.keys()) == {
        "api_abuse", "product_bug", "ux_confusion", "messaging_gap",
        "credential_theft", "abuse", "safety", "other",
    }


def test_api_put_escalation_criteria_persists_and_affects_scoring(tmp_path, monkeypatch, client):
    test_client, settings = client
    yaml_path = tmp_path / "escalation_criteria.yaml"
    monkeypatch.setattr(
        api_module, "load_escalation_criteria", lambda: config_module.load_escalation_criteria(yaml_path)
    )
    monkeypatch.setattr(
        api_module,
        "save_escalation_criteria",
        lambda updates: config_module.save_escalation_criteria(updates, path=yaml_path),
    )

    response = test_client.put(
        "/api/escalation-criteria",
        json={
            "categories": {
                "product_bug": {
                    "requires_qa": True,
                    "velocity_threshold": 3.0,
                    "response_template": "Escalate immediately.",
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()["categories"]
    assert body["product_bug"] == {
        "requires_qa": True,
        "velocity_threshold": 3.0,
        "response_template": "Escalate immediately.",
    }
    # Persisted to disk, not just the response -- and other categories survive untouched.
    reloaded = config_module.load_escalation_criteria(yaml_path)
    assert reloaded["product_bug"]["velocity_threshold"] == 3.0
    assert reloaded["safety"]["requires_qa"] is True


def test_api_put_escalation_criteria_rejects_invalid_shape(client):
    test_client, _settings = client
    response = test_client.put(
        "/api/escalation-criteria",
        json={"categories": {"product_bug": {"requires_qa": "not-a-bool"}}},
    )
    assert response.status_code == 422


def test_api_alerts_includes_client_for_client_scoped_hit(tmp_path, monkeypatch, client):
    test_client, settings = client
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text(
        "subreddits: []\nterms: []\nclients:\n  - McDonald's\nrisk_patterns:\n  - jailbreak\n"
    )
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_client", matched_term="McDonald's jailbreak")
    _seed_alert(conn, "t3_generic", matched_term="Claude API")
    conn.close()

    response = test_client.get("/api/alerts")

    body = {a["post_id"]: a["client"] for a in response.json()}
    assert body["t3_client"] == "McDonald's"
    assert body["t3_generic"] is None


def test_api_watching_includes_client_for_client_scoped_hit(tmp_path, monkeypatch, client):
    test_client, settings = client
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text(
        "subreddits: []\nterms: []\nclients:\n  - Acme Corp\nrisk_patterns:\n  - token theft\n"
    )
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_classification_only(conn, "t3_watch", matched_term="Acme Corp token theft")
    conn.close()

    response = test_client.get("/api/watching")

    assert response.json()[0]["client"] == "Acme Corp"


def test_api_clusters_includes_protection_tier(client):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", category="product_bug", model_implicated="claude_fable")
    conn.close()

    response = test_client.get("/api/clusters")

    cluster = next(c for c in response.json() if c["cluster_key"] == "product_bug:claude_fable")
    assert cluster["protection_tier"] == "flagship"


def test_api_get_model_tiers_returns_defaults(client):
    test_client, _settings = client
    response = test_client.get("/api/model-tiers")

    assert response.status_code == 200
    models = response.json()["models"]
    assert models["claude_opus"]["protection_tier"] == "flagship"
    assert models["claude_fable"]["protection_tier"] == "flagship"
    assert models["claude_haiku"]["protection_tier"] == "standard"


def test_api_put_model_tiers_persists_and_affects_scoring(tmp_path, monkeypatch, client):
    test_client, settings = client
    yaml_path = tmp_path / "model_tiers.yaml"
    monkeypatch.setattr(
        api_module, "load_model_tiers", lambda: config_module.load_model_tiers(yaml_path)
    )
    monkeypatch.setattr(
        api_module,
        "save_model_tiers",
        lambda updates: config_module.save_model_tiers(updates, path=yaml_path),
    )

    response = test_client.put(
        "/api/model-tiers",
        json={
            "models": {
                "claude_haiku": {"protection_tier": "standard", "velocity_threshold": 999.0}
            }
        },
    )

    assert response.status_code == 200
    body = response.json()["models"]
    assert body["claude_haiku"]["velocity_threshold"] == 999.0
    reloaded = config_module.load_model_tiers(yaml_path)
    assert reloaded["claude_haiku"]["velocity_threshold"] == 999.0
    assert reloaded["claude_opus"]["protection_tier"] == "flagship"  # untouched


def test_api_put_model_tiers_rejects_invalid_tier(client):
    test_client, _settings = client
    response = test_client.put(
        "/api/model-tiers",
        json={"models": {"claude_haiku": {"protection_tier": "super-duper", "velocity_threshold": None}}},
    )
    assert response.status_code == 422


@respx.mock
def test_api_alert_brief_mentions_flagship_tier(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_flagship", model_implicated="claude_fable")
    conn.close()
    # 401 (not 429/5xx) forces the template fallback without also triggering
    # RateLimitedClient's real exponential-backoff sleep between retries.
    respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(401))

    response = test_client.post("/api/alerts/t3_flagship/brief")

    assert response.status_code == 200
    assert "FLAGSHIP" in response.json()["brief"]


@respx.mock
def test_api_alert_transition_generates_coa_for_actionable_status(client, load_anthropic_fixture):
    test_client, settings = client
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", category="credential_theft")
    conn.close()
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("brief_text_response.json"))
    )

    response = test_client.post("/api/alerts/t3_a/transition", json={"status": "acknowledged"})

    assert response.status_code == 200
    body = response.json()
    assert body["coa"]
    assert "Recommend eng triage" in body["coa"]

    # Persisted both as the alert's current COA and on the timeline event.
    conn = get_connection(settings.database_path)
    stored = conn.execute("SELECT coa FROM alerts WHERE post_id='t3_a'").fetchone()[0]
    conn.close()
    assert stored == body["coa"]

    timeline = test_client.get("/api/alerts/t3_a/timeline").json()
    assert timeline[0]["coa"] == body["coa"]


@pytest.mark.parametrize("status", ["open", "false_positive"])
def test_api_alert_transition_no_coa_for_non_actionable_status(status, settings_factory, monkeypatch):
    # No respx mock at all -- if this accidentally tried to call Claude, the
    # test would hang/fail on a real network attempt, proving no call happens.
    settings = settings_factory()
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    test_client = TestClient(api_module.app)
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a")
    conn.close()

    response = test_client.post("/api/alerts/t3_a/transition", json={"status": status})

    assert response.status_code == 200
    assert response.json()["coa"] is None


def test_api_alert_transition_coa_mentions_client_when_present(tmp_path, monkeypatch, client):
    test_client, settings = client
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text(
        "subreddits: []\nterms: []\nclients:\n  - McDonald's\nrisk_patterns:\n  - token theft\n"
    )
    monkeypatch.setattr(
        api_module, "load_search_terms", lambda: config_module.load_search_terms(yaml_path)
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings.model_copy(update={"anthropic_api_key": ""}))
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_client", category="credential_theft", matched_term="McDonald's token theft")
    conn.close()

    response = test_client.post("/api/alerts/t3_client/transition", json={"status": "mitigating"})

    assert response.status_code == 200
    assert "McDonald's" in response.json()["coa"]
