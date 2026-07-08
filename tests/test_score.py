from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.db import get_connection, init_db
from radar.score import compute_velocity, run_scoring

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_compute_velocity_none_with_fewer_than_two_points():
    assert compute_velocity([]) is None
    assert compute_velocity([(BASE, 10.0)]) is None


def test_compute_velocity_none_when_elapsed_time_is_zero():
    assert compute_velocity([(BASE, 10.0), (BASE, 20.0)]) is None


def test_compute_velocity_computes_rate_per_hour():
    history = [(BASE, 10.0), (BASE + timedelta(hours=2), 50.0)]
    assert compute_velocity(history) == 20.0


def test_compute_velocity_can_be_negative():
    history = [(BASE, 50.0), (BASE + timedelta(hours=1), 10.0)]
    assert compute_velocity(history) == -40.0


def _insert_snapshot(conn, post_id: str, collected_at: datetime, score_value: float) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, virality_score, search_pass)
        VALUES (?, 'reddit', 'run-1', ?, ?, ?, ?, 'top')
        """,
        (post_id, collected_at.isoformat(), collected_at.isoformat(), f"https://x/{post_id}", score_value),
    )
    conn.commit()


def _insert_classification(
    conn, post_id: str, category: str = "product_bug", severity: str = "high", is_pain_point: bool = True
) -> None:
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, ?, ?, 'claude_api_general', ?, 'summary', 'test-model', ?)
        """,
        (post_id, int(is_pain_point), category, severity, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _seeded_conn(settings):
    conn = get_connection(settings.database_path)
    init_db(conn)
    return conn


def test_run_scoring_writes_alert_for_accelerating_pain_point(settings_factory):
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_a", BASE, 10.0)
    _insert_snapshot(conn, "t3_a", BASE + timedelta(hours=1), 50.0)  # velocity = 40/hr
    _insert_classification(conn, "t3_a", category="product_bug")
    conn.close()

    result = run_scoring(settings)

    assert result.skipped is False
    assert result.alerts_written == 1

    conn = get_connection(settings.database_path)
    row = conn.execute("SELECT velocity, qa_status FROM alerts WHERE post_id = 't3_a'").fetchone()
    conn.close()
    assert row[0] == 40.0
    assert row[1] == "not_required"


def test_run_scoring_sets_pending_qa_for_sensitive_category(settings_factory):
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_b", BASE, 10.0)
    _insert_snapshot(conn, "t3_b", BASE + timedelta(hours=1), 50.0)
    _insert_classification(conn, "t3_b", category="abuse")
    conn.close()

    run_scoring(settings)

    conn = get_connection(settings.database_path)
    row = conn.execute("SELECT qa_status FROM alerts WHERE post_id = 't3_b'").fetchone()
    conn.close()
    assert row[0] == "pending"


def test_run_scoring_skips_below_threshold(settings_factory):
    settings = settings_factory(velocity_threshold=100.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_c", BASE, 10.0)
    _insert_snapshot(conn, "t3_c", BASE + timedelta(hours=1), 50.0)  # velocity = 40/hr, below 100
    _insert_classification(conn, "t3_c")
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 0
    conn = get_connection(settings.database_path)
    count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    conn.close()
    assert count == 0


def test_run_scoring_ignores_non_pain_points(settings_factory):
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_d", BASE, 10.0)
    _insert_snapshot(conn, "t3_d", BASE + timedelta(hours=1), 50.0)
    _insert_classification(conn, "t3_d", is_pain_point=False)
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 0


def test_run_scoring_suppresses_repeat_alert_unless_accelerating(settings_factory):
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_e", BASE, 10.0)
    _insert_snapshot(conn, "t3_e", BASE + timedelta(hours=1), 50.0)  # velocity 40/hr
    _insert_classification(conn, "t3_e")
    conn.close()

    first = run_scoring(settings)
    assert first.alerts_written == 1

    # Same velocity again (another snapshot at the same rate) -- not accelerating further.
    conn = get_connection(settings.database_path)
    _insert_snapshot(conn, "t3_e", BASE + timedelta(hours=2), 90.0)  # still 40/hr
    conn.close()

    second = run_scoring(settings)
    assert second.alerts_written == 0

    # Now it accelerates past the last alert's velocity -- should re-fire.
    conn = get_connection(settings.database_path)
    _insert_snapshot(conn, "t3_e", BASE + timedelta(hours=2, minutes=30), 150.0)  # 120/hr over last 30 min
    conn.close()

    third = run_scoring(settings)
    assert third.alerts_written == 1

    conn = get_connection(settings.database_path)
    count = conn.execute("SELECT COUNT(*) FROM alerts WHERE post_id = 't3_e'").fetchone()[0]
    conn.close()
    assert count == 2


def test_run_scoring_skipped_when_no_pain_points(settings_factory):
    settings = settings_factory()
    conn = _seeded_conn(settings)
    conn.close()

    result = run_scoring(settings)

    assert result.skipped is True
    assert result.alerts_written == 0
