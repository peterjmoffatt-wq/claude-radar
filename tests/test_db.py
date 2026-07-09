from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from radar.db import (
    count_advertisements,
    get_alerts,
    get_cluster_brief,
    get_connection,
    get_incident_timeline,
    get_snapshot_history,
    get_unscored_pain_points,
    init_db,
    save_cluster_brief,
    save_exec_brief,
    save_incident_report,
    transition_incident,
    write_alert,
    write_classifications,
)
from radar.models import Classification, ModelImplicated, PainCategory, Severity


def test_migrate_adds_is_advertisement_column_to_pre_existing_table(tmp_path):
    # Simulates a database file created before is_advertisement existed: build
    # the OLD-shape classifications table by hand (no is_advertisement column,
    # no schema_meta row) with one real row already in it, then confirm
    # init_db() adds the column via ALTER TABLE without losing that row --
    # `CREATE TABLE IF NOT EXISTS` alone would silently no-op here.
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE classifications (
            post_id           TEXT PRIMARY KEY,
            is_pain_point     INTEGER NOT NULL,
            category          TEXT NOT NULL,
            model_implicated  TEXT NOT NULL,
            severity          TEXT NOT NULL,
            issue_summary     TEXT NOT NULL,
            classifier_model  TEXT NOT NULL,
            classified_at     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO classifications (post_id, is_pain_point, category, model_implicated, "
        "severity, issue_summary, classifier_model, classified_at) "
        "VALUES ('t3_old', 1, 'product_bug', 'claude_api_general', 'high', 'pre-migration row', "
        "'test-model', '2024-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    conn = get_connection(db_path)
    init_db(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(classifications)").fetchall()}
    assert "is_advertisement" in columns

    row = conn.execute(
        "SELECT post_id, is_advertisement FROM classifications WHERE post_id = 't3_old'"
    ).fetchone()
    conn.close()
    assert row == ("t3_old", 0)


def test_init_db_skips_migrate_once_schema_version_matches(tmp_path, monkeypatch):
    import radar.db as db_module

    conn = get_connection(tmp_path / "radar.db")
    db_module.init_db(conn)  # first call: no schema_meta row yet -- must migrate

    calls = []
    monkeypatch.setattr(db_module, "_migrate", lambda c: calls.append(c))
    db_module.init_db(conn)  # second call: version already matches -- must skip
    conn.close()

    assert calls == []


def test_init_db_is_idempotent_against_already_migrated_table(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    init_db(conn)  # must not raise (e.g. duplicate ALTER TABLE)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(classifications)").fetchall()}
    conn.close()
    assert "is_advertisement" in columns


def _classification(post_id: str, is_advertisement: bool) -> Classification:
    return Classification(
        post_id=post_id,
        is_pain_point=False,
        is_advertisement=is_advertisement,
        category=PainCategory.OTHER,
        model_implicated=ModelImplicated.CLAUDE_API_GENERAL,
        severity=Severity.LOW,
        issue_summary="ad post" if is_advertisement else "real post",
    )


def test_write_classifications_round_trips_is_advertisement(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    write_classifications(
        conn,
        [_classification("t3_ad", is_advertisement=True), _classification("t3_real", is_advertisement=False)],
        classifier_model="test-model",
    )

    rows = dict(
        conn.execute("SELECT post_id, is_advertisement FROM classifications").fetchall()
    )
    conn.close()
    assert rows == {"t3_ad": 1, "t3_real": 0}


def test_count_advertisements(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    write_classifications(
        conn,
        [
            _classification("t3_ad1", is_advertisement=True),
            _classification("t3_ad2", is_advertisement=True),
            _classification("t3_real", is_advertisement=False),
        ],
        classifier_model="test-model",
    )

    count = count_advertisements(conn)
    conn.close()
    assert count == 2


def test_count_advertisements_zero_on_empty_db(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    count = count_advertisements(conn)
    conn.close()
    assert count == 0


def _seed_two_snapshots(conn, post_id: str, platform: str = "reddit") -> None:
    """An older, hash-looking/low-engagement snapshot followed by a newer,
    raw-handle/high-engagement one -- get_alerts()/get_unscored_pain_points()
    must reflect the NEWER row, proving the correlated-subquery -> JOIN
    rewrite preserved "latest snapshot per post" semantics.
    """
    older = datetime(2024, 1, 1, tzinfo=timezone.utc)
    newer = older + timedelta(hours=1)
    conn.execute(
        """
        INSERT INTO snapshots (
            post_id, platform, poll_run_id, collected_at, created_at, url, search_pass,
            matched_term, hashed_author, likes, comments, score, shares
        ) VALUES (?, ?, 'run-1', ?, ?, ?, 'top', 'Claude API',
                  'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2', 1, 0, 5, 0)
        """,
        (post_id, platform, older.isoformat(), older.isoformat(), f"https://x/{post_id}"),
    )
    conn.execute(
        """
        INSERT INTO snapshots (
            post_id, platform, poll_run_id, collected_at, created_at, url, search_pass,
            matched_term, hashed_author, likes, comments, score, shares
        ) VALUES (?, ?, 'run-2', ?, ?, ?, 'recent', 'Claude API',
                  'real_handle_99', 42, 9, 150, 3)
        """,
        (post_id, platform, newer.isoformat(), newer.isoformat(), f"https://x/{post_id}"),
    )
    conn.commit()


def test_get_alerts_returns_latest_snapshot_author_and_engagement(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES ('t3_a', 1, 'product_bug', 'claude_api_general', 'high', 'a summary', 'test-model', ?)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES ('t3_a', ?, 100.0, 40.0, 'product_bug', 'high', 'not_required')
        """,
        (now,),
    )
    _seed_two_snapshots(conn, "t3_a")
    conn.commit()

    rows = get_alerts(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    # (post_id, category, severity, velocity, virality_score, qa_status, triggered_at,
    #  issue_summary, model_implicated, url, platform, matched_term,
    #  author, likes, comments, score, shares, created_at)
    assert row[12] == "real_handle_99"
    assert row[13] == 42  # likes
    assert row[14] == 9  # comments
    assert row[15] == 150  # score
    assert row[16] == 3  # shares
    assert row[17] == datetime(2024, 1, 1, 1, tzinfo=timezone.utc).isoformat()


def test_get_alerts_filters_by_post_id(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    for post_id in ("t3_x", "t3_y"):
        conn.execute(
            """
            INSERT INTO classifications (
                post_id, is_pain_point, category, model_implicated, severity,
                issue_summary, classifier_model, classified_at
            ) VALUES (?, 1, 'product_bug', 'claude_api_general', 'high', 'a summary', 'test-model', ?)
            """,
            (post_id, now),
        )
        conn.execute(
            """
            INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
            VALUES (?, ?, 100.0, 40.0, 'product_bug', 'high', 'not_required')
            """,
            (post_id, now),
        )
        _seed_two_snapshots(conn, post_id)
    conn.commit()

    rows = get_alerts(conn, post_id="t3_x")
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "t3_x"


def test_get_unscored_pain_points_returns_latest_snapshot_author_and_engagement(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES ('t3_b', 1, 'product_bug', 'claude_api_general', 'med', 'a summary', 'test-model', ?)
        """,
        (now,),
    )
    _seed_two_snapshots(conn, "t3_b", platform="youtube")
    conn.commit()

    rows = get_unscored_pain_points(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    # (post_id, category, severity, issue_summary, model_implicated,
    #  url, platform, matched_term, author, likes, comments, score, shares, created_at)
    assert row[6] == "youtube"
    assert row[8] == "real_handle_99"
    assert row[9] == 42  # likes
    assert row[10] == 9  # comments
    assert row[11] == 150  # score
    assert row[12] == 3  # shares
    assert row[13] == datetime(2024, 1, 1, 1, tzinfo=timezone.utc).isoformat()


def test_get_snapshot_history_collapses_same_run_duplicates(tmp_path):
    # A post appearing in both the "top" and "recent" search passes of one
    # collection run gets two snapshot rows written seconds apart, sharing a
    # poll_run_id -- get_snapshot_history() must return only the latest row
    # per run, or compute_velocity() would see a near-zero elapsed window
    # between what are really two rows from the SAME run.
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    run1_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    run2_top_at = run1_at + timedelta(hours=2)
    run2_recent_at = run2_top_at + timedelta(seconds=5)

    def insert(collected_at, score, poll_run_id):
        conn.execute(
            """
            INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, virality_score, search_pass)
            VALUES ('t3_dup', 'reddit', ?, ?, ?, 'https://x/t3_dup', ?, 'top')
            """,
            (poll_run_id, collected_at.isoformat(), collected_at.isoformat(), score),
        )

    insert(run1_at, 10.0, "run-1")
    insert(run2_top_at, 50.0, "run-2")
    insert(run2_recent_at, 51.0, "run-2")  # same run as above, a few seconds later
    conn.commit()

    history = get_snapshot_history(conn, "t3_dup")
    conn.close()

    assert len(history) == 2
    assert history[0] == (run1_at, 10.0)
    assert history[1] == (run2_recent_at, 51.0)  # latest row within run-2 wins


def test_migrate_adds_incident_columns_to_pre_existing_alerts_table(tmp_path):
    # Simulates a database file created before the incident-lifecycle/brief/
    # report columns existed: build the OLD-shape alerts table by hand with a
    # real row in it, then confirm init_db() adds the columns via ALTER TABLE
    # without losing that row.
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE alerts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id           TEXT NOT NULL,
            triggered_at      TEXT NOT NULL,
            virality_score    REAL NOT NULL,
            velocity          REAL NOT NULL,
            category          TEXT NOT NULL,
            severity          TEXT NOT NULL,
            qa_status         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, "
        "severity, qa_status) VALUES ('t3_old', '2024-01-01T00:00:00+00:00', 10.0, 5.0, "
        "'product_bug', 'high', 'not_required')"
    )
    conn.commit()
    conn.close()

    conn = get_connection(db_path)
    init_db(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    assert {
        "incident_status",
        "exec_brief",
        "exec_brief_generated_at",
        "incident_report",
        "incident_report_generated_at",
    } <= columns

    row = conn.execute(
        "SELECT post_id, incident_status, exec_brief FROM alerts WHERE post_id = 't3_old'"
    ).fetchone()
    conn.close()
    assert row == ("t3_old", "open", None)


def test_new_alert_defaults_to_open_incident_status(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    write_alert(conn, "t3_new", 10.0, 5.0, "product_bug", "high", "not_required")

    status = conn.execute("SELECT incident_status FROM alerts WHERE post_id = 't3_new'").fetchone()
    conn.close()
    assert status == ("open",)


def test_transition_incident_updates_status_and_logs_event(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    write_alert(conn, "t3_a", 10.0, 5.0, "product_bug", "high", "not_required")

    changed = transition_incident(conn, "t3_a", "acknowledged", note="Looking into it")

    status = conn.execute("SELECT incident_status FROM alerts WHERE post_id = 't3_a'").fetchone()
    timeline = get_incident_timeline(conn, "t3_a")
    conn.close()

    assert changed is True
    assert status == ("acknowledged",)
    assert len(timeline) == 1
    from_status, to_status, note, created_at = timeline[0]
    assert (from_status, to_status, note) == ("open", "acknowledged", "Looking into it")
    assert created_at


def test_transition_incident_returns_false_when_no_alert_exists(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    changed = transition_incident(conn, "t3_missing", "acknowledged")
    conn.close()

    assert changed is False


def test_transition_incident_operates_on_latest_alert_row(tmp_path):
    # A post that re-alerted (accelerated twice) has two alert rows -- the
    # transition (like resolve_alert/get_latest_alert_velocity) must act on
    # the newest one, not an earlier superseded row.
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    write_alert(conn, "t3_re", 10.0, 5.0, "product_bug", "high", "not_required")
    write_alert(conn, "t3_re", 20.0, 15.0, "product_bug", "high", "not_required")

    transition_incident(conn, "t3_re", "acknowledged")

    rows = conn.execute(
        "SELECT velocity, incident_status FROM alerts WHERE post_id = 't3_re' ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows == [(5.0, "open"), (15.0, "acknowledged")]


def test_get_incident_timeline_returns_events_oldest_first(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    write_alert(conn, "t3_b", 10.0, 5.0, "product_bug", "high", "not_required")

    transition_incident(conn, "t3_b", "acknowledged")
    transition_incident(conn, "t3_b", "mitigating")
    transition_incident(conn, "t3_b", "resolved", note="Fixed upstream")

    timeline = get_incident_timeline(conn, "t3_b")
    conn.close()

    assert [(f, t) for f, t, _, _ in timeline] == [
        ("open", "acknowledged"),
        ("acknowledged", "mitigating"),
        ("mitigating", "resolved"),
    ]
    assert timeline[-1][2] == "Fixed upstream"


def test_save_exec_brief_persists_and_returns_true(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    write_alert(conn, "t3_c", 10.0, 5.0, "product_bug", "high", "not_required")

    changed = save_exec_brief(conn, "t3_c", "Users report X. Velocity Y. Recommend Z.")

    row = conn.execute(
        "SELECT exec_brief, exec_brief_generated_at FROM alerts WHERE post_id = 't3_c'"
    ).fetchone()
    conn.close()
    assert changed is True
    assert row[0] == "Users report X. Velocity Y. Recommend Z."
    assert row[1]  # timestamp written


def test_save_exec_brief_returns_false_when_no_alert_exists(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    changed = save_exec_brief(conn, "t3_missing", "brief text")
    conn.close()

    assert changed is False


def test_save_incident_report_persists_and_returns_true(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    write_alert(conn, "t3_d", 10.0, 5.0, "product_bug", "high", "not_required")

    changed = save_incident_report(conn, "t3_d", "# Post-incident report\n...")

    row = conn.execute(
        "SELECT incident_report, incident_report_generated_at FROM alerts WHERE post_id = 't3_d'"
    ).fetchone()
    conn.close()
    assert changed is True
    assert row[0] == "# Post-incident report\n..."
    assert row[1]


def test_cluster_brief_save_and_get_round_trip(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    save_cluster_brief(conn, "product_bug:claude_code", "This cluster is spreading.")
    brief = get_cluster_brief(conn, "product_bug:claude_code")
    conn.close()

    assert brief == "This cluster is spreading."


def test_get_cluster_brief_returns_none_when_absent(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    brief = get_cluster_brief(conn, "product_bug:claude_code")
    conn.close()

    assert brief is None


def test_save_cluster_brief_upserts_existing_key(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)

    save_cluster_brief(conn, "safety:unknown", "First version.")
    save_cluster_brief(conn, "safety:unknown", "Regenerated version.")
    brief = get_cluster_brief(conn, "safety:unknown")
    conn.close()

    assert brief == "Regenerated version."
