from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.cluster import get_clusters
from radar.db import get_connection, init_db

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _seed(
    conn, post_id, category, model_implicated, severity, issue_summary, triggered_at, platform="reddit"
):
    conn.execute(
        """
        INSERT INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, 1, ?, ?, ?, ?, 'test-model', ?)
        """,
        (post_id, category, model_implicated, severity, issue_summary, triggered_at.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, search_pass)
        VALUES (?, ?, 'run-1', ?, ?, ?, 'top')
        """,
        (post_id, platform, triggered_at.isoformat(), triggered_at.isoformat(), f"https://x/{post_id}"),
    )
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES (?, ?, 100.0, 40.0, ?, ?, 'not_required')
        """,
        (post_id, triggered_at.isoformat(), category, severity),
    )
    conn.commit()


def test_groups_by_category_and_model(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_api_general", "high", "summary a", BASE)
    _seed(conn, "t3_b", "product_bug", "claude_api_general", "med", "summary b", BASE + timedelta(hours=1))
    _seed(conn, "t3_c", "ux_confusion", "claude_code", "low", "summary c", BASE + timedelta(hours=2))

    clusters = get_clusters(conn)
    conn.close()

    assert len(clusters) == 2
    bug_cluster = next(c for c in clusters if c.cluster_key == "product_bug:claude_api_general")
    assert bug_cluster.alert_count == 2
    assert bug_cluster.max_severity == "high"
    assert bug_cluster.representative_issue_summary == "summary b"  # latest of the two

    ux_cluster = next(c for c in clusters if c.cluster_key == "ux_confusion:claude_code")
    assert ux_cluster.alert_count == 1


def test_clusters_sorted_by_alert_count_desc(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_api_general", "high", "s", BASE)
    _seed(conn, "t3_b", "safety", "claude_opus", "high", "s", BASE)
    _seed(conn, "t3_c", "safety", "claude_opus", "high", "s", BASE + timedelta(hours=1))
    _seed(conn, "t3_d", "safety", "claude_opus", "high", "s", BASE + timedelta(hours=2))

    clusters = get_clusters(conn)
    conn.close()

    assert clusters[0].cluster_key == "safety:claude_opus"
    assert clusters[0].alert_count == 3


def test_no_alerts_returns_empty_list(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)

    clusters = get_clusters(conn)
    conn.close()

    assert clusters == []


def test_episode_count_is_one_for_a_single_tight_burst(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_code", "high", "s", BASE)
    _seed(conn, "t3_b", "product_bug", "claude_code", "high", "s", BASE + timedelta(hours=1))
    _seed(conn, "t3_c", "product_bug", "claude_code", "high", "s", BASE + timedelta(hours=2))

    clusters = get_clusters(conn, recurrence_gap_hours=48.0)
    conn.close()

    cluster = next(c for c in clusters if c.cluster_key == "product_bug:claude_code")
    assert cluster.episode_count == 1
    assert cluster.first_triggered_at == BASE.isoformat()
    assert cluster.latest_triggered_at == (BASE + timedelta(hours=2)).isoformat()


def test_episode_count_increments_after_a_quiet_gap(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_code", "high", "s", BASE)
    # A gap of 10 days (> the 48h default) before this one -- a genuinely
    # separate recurrence, not just continued chatter from the same burst.
    _seed(
        conn, "t3_b", "product_bug", "claude_code", "high", "s", BASE + timedelta(days=10)
    )

    clusters = get_clusters(conn, recurrence_gap_hours=48.0)
    conn.close()

    cluster = next(c for c in clusters if c.cluster_key == "product_bug:claude_code")
    assert cluster.episode_count == 2


def test_episode_count_respects_custom_gap_threshold(settings_factory):
    # The same two alerts as above, but with a wider gap threshold that
    # swallows the 10-day gap into a single episode.
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_code", "high", "s", BASE)
    _seed(
        conn, "t3_b", "product_bug", "claude_code", "high", "s", BASE + timedelta(days=10)
    )

    clusters = get_clusters(conn, recurrence_gap_hours=24 * 30)
    conn.close()

    cluster = next(c for c in clusters if c.cluster_key == "product_bug:claude_code")
    assert cluster.episode_count == 1


def test_platforms_lists_every_distinct_platform_in_the_cluster(settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "product_bug", "claude_code", "high", "s", BASE, platform="reddit")
    _seed(
        conn, "t3_b", "product_bug", "claude_code", "high", "s",
        BASE + timedelta(hours=1), platform="youtube",
    )
    _seed(
        conn, "t3_c", "product_bug", "claude_code", "high", "s",
        BASE + timedelta(hours=2), platform="reddit",
    )

    clusters = get_clusters(conn)
    conn.close()

    cluster = next(c for c in clusters if c.cluster_key == "product_bug:claude_code")
    assert cluster.platforms == ["reddit", "youtube"]


def test_list_clusters_uses_settings_recurrence_gap_hours(settings_factory):
    from radar.cluster import list_clusters

    settings = settings_factory(recurrence_gap_hours=1.0)
    conn = get_connection(settings.database_path)
    init_db(conn)
    _seed(conn, "t3_a", "safety", "unknown", "high", "s", BASE)
    # A 2-hour gap, wider than the configured 1-hour threshold -- must count
    # as a second episode when list_clusters() reads settings.recurrence_gap_hours.
    _seed(conn, "t3_b", "safety", "unknown", "high", "s", BASE + timedelta(hours=2))
    conn.close()

    clusters = list_clusters(settings)

    cluster = next(c for c in clusters if c.cluster_key == "safety:unknown")
    assert cluster.episode_count == 2
