from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.db import get_connection, init_db
from radar.leadtime import LeadTimeEntry, compute_lead_times, summarize_lead_times

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _insert_snapshot(conn, post_id: str, search_pass: str, collected_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass)
        VALUES (?, 'reddit', 'run-1', ?, ?, ?, ?)
        """,
        (post_id, collected_at.isoformat(), collected_at.isoformat(), f"https://x/{post_id}", search_pass),
    )
    conn.commit()


def test_summarize_lead_times_only_counts_positive_values():
    entries = [
        LeadTimeEntry("a", BASE, BASE + timedelta(minutes=10), 600.0),
        LeadTimeEntry("b", BASE, BASE + timedelta(minutes=30), 1800.0),
        LeadTimeEntry("c", BASE, BASE - timedelta(minutes=5), -300.0),  # top came first: not "caught early"
        LeadTimeEntry("d", None, BASE, None),  # only one pass
    ]

    summary = summarize_lead_times(entries)

    assert summary["posts_with_both_passes"] == 3
    assert summary["posts_caught_early"] == 2
    assert summary["median_lead_time_seconds"] == 1200.0
    assert summary["mean_lead_time_seconds"] == 1200.0


def test_summarize_lead_times_handles_no_data():
    summary = summarize_lead_times([])
    assert summary["posts_caught_early"] == 0
    assert summary["median_lead_time_seconds"] is None
    assert summary["mean_lead_time_seconds"] is None


def test_compute_lead_times_positive_when_recent_pass_is_earlier(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _insert_snapshot(conn, "t3_a", "recent", BASE)
    _insert_snapshot(conn, "t3_a", "top", BASE + timedelta(hours=2))

    entries = compute_lead_times(conn)
    conn.close()

    entry = next(e for e in entries if e.post_id == "t3_a")
    assert entry.lead_time_seconds == timedelta(hours=2).total_seconds()


def test_compute_lead_times_none_when_only_one_pass_seen(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _insert_snapshot(conn, "t3_b", "top", BASE)

    entries = compute_lead_times(conn)
    conn.close()

    entry = next(e for e in entries if e.post_id == "t3_b")
    assert entry.lead_time_seconds is None
    assert entry.first_recent_seen_at is None


def test_compute_lead_times_uses_earliest_snapshot_per_pass(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _insert_snapshot(conn, "t3_c", "recent", BASE + timedelta(hours=1))
    _insert_snapshot(conn, "t3_c", "recent", BASE)  # earlier -- should win
    _insert_snapshot(conn, "t3_c", "top", BASE + timedelta(hours=3))

    entries = compute_lead_times(conn)
    conn.close()

    entry = next(e for e in entries if e.post_id == "t3_c")
    assert entry.first_recent_seen_at == BASE
    assert entry.lead_time_seconds == timedelta(hours=3).total_seconds()
