from __future__ import annotations

from dataclasses import dataclass

from radar.config import Settings, get_settings
from radar.db import get_alerts_for_clustering, get_connection, init_db

_SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}


@dataclass
class ClusterSummary:
    cluster_key: str
    label: str
    alert_count: int
    max_severity: str
    latest_triggered_at: str
    representative_issue_summary: str


def _label(category: str, model_implicated: str) -> str:
    return f"{category.replace('_', ' ').capitalize()} — {model_implicated.replace('_', ' ')}"


def get_clusters(conn) -> list[ClusterSummary]:
    """Deterministic root-cause grouping by (category, model_implicated) -- a pure function
    of already-classified alerts, computed at query time rather than persisted.
    """
    rows = get_alerts_for_clustering(conn)

    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for category, model_implicated, severity, issue_summary, triggered_at in rows:
        groups.setdefault((category, model_implicated), []).append(
            (severity, issue_summary, triggered_at)
        )

    summaries = []
    for (category, model_implicated), entries in groups.items():
        latest = max(entries, key=lambda e: e[2])
        max_severity = max(entries, key=lambda e: _SEVERITY_RANK.get(e[0], 0))[0]
        summaries.append(
            ClusterSummary(
                cluster_key=f"{category}:{model_implicated}",
                label=_label(category, model_implicated),
                alert_count=len(entries),
                max_severity=max_severity,
                latest_triggered_at=latest[2],
                representative_issue_summary=latest[1],
            )
        )

    summaries.sort(key=lambda s: s.alert_count, reverse=True)
    return summaries


def list_clusters(settings: Settings | None = None) -> list[ClusterSummary]:
    settings = settings or get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    try:
        return get_clusters(conn)
    finally:
        conn.close()


def main() -> None:
    clusters = list_clusters()
    if not clusters:
        print("No clusters yet -- run `radar score` first.")
        return
    for c in clusters:
        print(
            f"{c.label}\tn={c.alert_count}\tmax_severity={c.max_severity}\t"
            f"latest={c.latest_triggered_at}\t{c.representative_issue_summary}"
        )


if __name__ == "__main__":
    main()
