from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.db import get_connection, init_db
from radar.qa import list_pending, review


def _seed_alert(conn, post_id: str, category: str, qa_status: str) -> None:
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, 1, ?, 'claude_api_general', 'high', 'a summary', 'test-model', ?)
        """,
        (post_id, category, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        """
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass)
        VALUES (?, 'reddit', 'run-1', ?, ?, ?, 'top')
        """,
        (post_id, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), f"https://x/{post_id}"),
    )
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES (?, ?, 100.0, 40.0, ?, 'high', ?)
        """,
        (post_id, datetime.now(timezone.utc).isoformat(), category, qa_status),
    )
    conn.commit()


def test_list_pending_returns_only_pending_alerts(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_pending", "abuse", "pending")
    _seed_alert(conn, "t3_resolved", "abuse", "approved")
    conn.close()

    pending = list_pending(settings)

    assert len(pending) == 1
    assert pending[0][0] == "t3_pending"
    assert pending[0][4] == "a summary"
    assert pending[0][5] == "https://x/t3_pending"


def test_list_pending_only_considers_latest_alert_per_post(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_multi", "abuse", "pending")
    # Post re-accelerates before review -- a second, later alert row is created.
    conn.execute(
        "INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status) "
        "VALUES (?, ?, 100.0, 80.0, 'abuse', 'high', 'pending')",
        ("t3_multi", (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()),
    )
    conn.commit()
    conn.close()

    # Only the latest alert row counts -- not two rows for the same post.
    assert len(list_pending(settings)) == 1

    review("t3_multi", "approved", settings)

    # Approving the latest alert must not leave an older 'pending' row still surfacing.
    assert list_pending(settings) == []


def test_review_approve_updates_status(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_a", "abuse", "pending")
    conn.close()

    changed = review("t3_a", "approved", settings)

    assert changed is True
    conn = get_connection(settings.database_path)
    status = conn.execute("SELECT qa_status FROM alerts WHERE post_id = 't3_a'").fetchone()[0]
    conn.close()
    assert status == "approved"


def test_review_reject_updates_status(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_b", "credential_theft", "pending")
    conn.close()

    changed = review("t3_b", "rejected", settings)

    assert changed is True
    conn = get_connection(settings.database_path)
    status = conn.execute("SELECT qa_status FROM alerts WHERE post_id = 't3_b'").fetchone()[0]
    conn.close()
    assert status == "rejected"


def test_review_returns_false_when_no_pending_alert(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.close()

    changed = review("nonexistent", "approved", settings)

    assert changed is False


def test_review_does_not_touch_already_resolved_alert(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed_alert(conn, "t3_c", "abuse", "approved")
    conn.close()

    changed = review("t3_c", "rejected", settings)

    assert changed is False
    conn = get_connection(settings.database_path)
    status = conn.execute("SELECT qa_status FROM alerts WHERE post_id = 't3_c'").fetchone()[0]
    conn.close()
    assert status == "approved"
