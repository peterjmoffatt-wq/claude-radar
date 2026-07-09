from __future__ import annotations

from datetime import datetime, timedelta, timezone

import radar.score as score_module
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


def _insert_snapshot(
    conn,
    post_id: str,
    collected_at: datetime,
    score_value: float,
    poll_run_id: str = "run-1",
    platform: str = "reddit",
) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, virality_score, search_pass)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'top')
        """,
        (
            post_id,
            platform,
            poll_run_id,
            collected_at.isoformat(),
            collected_at.isoformat(),
            f"https://x/{post_id}",
            score_value,
        ),
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
    _insert_snapshot(conn, "t3_a", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_a", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")  # velocity = 40/hr
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
    _insert_snapshot(conn, "t3_b", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_b", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")
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
    _insert_snapshot(conn, "t3_c", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_c", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")  # velocity = 40/hr, below 100
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
    _insert_snapshot(conn, "t3_d", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_d", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")
    _insert_classification(conn, "t3_d", is_pain_point=False)
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 0


def test_run_scoring_suppresses_repeat_alert_unless_accelerating(settings_factory):
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_e", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_e", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")  # velocity 40/hr
    _insert_classification(conn, "t3_e")
    conn.close()

    first = run_scoring(settings)
    assert first.alerts_written == 1

    # Same velocity again (another snapshot at the same rate) -- not accelerating further.
    conn = get_connection(settings.database_path)
    _insert_snapshot(conn, "t3_e", BASE + timedelta(hours=2), 90.0, poll_run_id="run-3")  # still 40/hr
    conn.close()

    second = run_scoring(settings)
    assert second.alerts_written == 0

    # Now it accelerates past the last alert's velocity -- should re-fire.
    conn = get_connection(settings.database_path)
    _insert_snapshot(
        conn, "t3_e", BASE + timedelta(hours=2, minutes=30), 150.0, poll_run_id="run-4"
    )  # 120/hr over last 30 min
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


def test_velocity_threshold_overrides_default_to_unchanged_behavior(settings_factory):
    # No overrides configured -- every platform must behave exactly like the
    # flat global threshold did before this feature existed.
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_yt", BASE, 10.0, poll_run_id="run-1", platform="youtube")
    _insert_snapshot(conn, "t3_yt", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2", platform="youtube")
    _insert_classification(conn, "t3_yt")
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 1


def test_velocity_threshold_override_applies_per_platform(settings_factory):
    settings = settings_factory(
        velocity_threshold=10.0, velocity_threshold_overrides={"youtube": 500.0}
    )
    conn = _seeded_conn(settings)
    # YouTube post accelerating at 40/hr -- below its 500/hr override, no alert.
    _insert_snapshot(conn, "t3_yt", BASE, 10.0, poll_run_id="run-1", platform="youtube")
    _insert_snapshot(conn, "t3_yt", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2", platform="youtube")
    _insert_classification(conn, "t3_yt")
    # Reddit post at the same 40/hr -- still only needs the global 10/hr default.
    _insert_snapshot(conn, "t3_reddit", BASE, 10.0, poll_run_id="run-1", platform="reddit")
    _insert_snapshot(conn, "t3_reddit", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2", platform="reddit")
    _insert_classification(conn, "t3_reddit")
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 1
    conn = get_connection(settings.database_path)
    alerted = {row[0] for row in conn.execute("SELECT post_id FROM alerts").fetchall()}
    conn.close()
    assert alerted == {"t3_reddit"}


def _stub_criteria(criteria: dict):
    return lambda *args, **kwargs: criteria


def test_qa_status_driven_by_escalation_criteria_not_hardcoded_list(settings_factory, monkeypatch):
    # requires_qa=True for a category NOT in the old hardcoded
    # HUMAN_QA_CATEGORIES list -- proves qa_status now comes from the loaded
    # criteria, not a leftover Settings field.
    monkeypatch.setattr(
        score_module,
        "load_escalation_criteria",
        _stub_criteria({"ux_confusion": {"requires_qa": True, "velocity_threshold": None}}),
    )
    settings = settings_factory(velocity_threshold=10.0)
    conn = _seeded_conn(settings)
    _insert_snapshot(conn, "t3_f", BASE, 10.0, poll_run_id="run-1")
    _insert_snapshot(conn, "t3_f", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2")
    _insert_classification(conn, "t3_f", category="ux_confusion")
    conn.close()

    run_scoring(settings)

    conn = get_connection(settings.database_path)
    row = conn.execute("SELECT qa_status FROM alerts WHERE post_id = 't3_f'").fetchone()
    conn.close()
    assert row[0] == "pending"


def test_category_velocity_threshold_override_wins_over_platform_override(
    settings_factory, monkeypatch
):
    # A category override (from escalation_criteria.yaml) must win over a
    # platform override (velocity_threshold_overrides) for the same alert --
    # see effective_velocity_threshold()'s documented precedence.
    monkeypatch.setattr(
        score_module,
        "load_escalation_criteria",
        _stub_criteria({"credential_theft": {"requires_qa": True, "velocity_threshold": 5.0}}),
    )
    settings = settings_factory(
        velocity_threshold=10.0, velocity_threshold_overrides={"youtube": 500.0}
    )
    conn = _seeded_conn(settings)
    # 40/hr: below the youtube platform override (500) but above the
    # category override (5) -- must alert only because of the category override.
    _insert_snapshot(conn, "t3_g", BASE, 10.0, poll_run_id="run-1", platform="youtube")
    _insert_snapshot(conn, "t3_g", BASE + timedelta(hours=1), 50.0, poll_run_id="run-2", platform="youtube")
    _insert_classification(conn, "t3_g", category="credential_theft")
    conn.close()

    result = run_scoring(settings)

    assert result.alerts_written == 1
