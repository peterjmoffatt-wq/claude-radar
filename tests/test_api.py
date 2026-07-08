from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import radar.api as api_module
from radar.db import get_connection, init_db


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
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass)
        VALUES (?, 'reddit', 'run-1', ?, ?, ?, 'top')
        """,
        (post_id, now, now, f"https://x/{post_id}"),
    )
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES (?, ?, 100.0, 40.0, ?, ?, ?)
        """,
        (post_id, now, category, severity, qa_status),
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


def test_static_index_served_at_root(client):
    test_client, _settings = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
