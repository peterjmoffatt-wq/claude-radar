from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.cluster import get_clusters
from radar.db import get_connection, init_db

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _seed(conn, post_id, category, model_implicated, severity, issue_summary, triggered_at):
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
