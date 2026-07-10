from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from typing import Any

from radar.config import Settings, get_settings, load_model_tiers, protection_tier_for
from radar.db import get_alerts_for_clustering, get_connection, init_db

_SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}

DEFAULT_RECURRENCE_GAP_HOURS = 48.0


@dataclass
class ClusterSummary:
    cluster_key: str
    label: str
    alert_count: int
    max_severity: str
    latest_triggered_at: str
    first_triggered_at: str
    episode_count: int
    platforms: list[str]
    protection_tier: str
    representative_issue_summary: str


def _label(category: str, model_implicated: str) -> str:
    return f"{category.replace('_', ' ').capitalize()} — {model_implicated.replace('_', ' ')}"


def _count_episodes(sorted_timestamps: list[datetime], gap_hours: float) -> int:
    """A quiet gap longer than `gap_hours` between consecutive alerts starts a
    new "episode" -- episode_count > 1 means this root cause has resurfaced
    after going quiet, not just alerted repeatedly in one tight burst.
    """
    if not sorted_timestamps:
        return 0
    episodes = 1
    for prev, curr in zip(sorted_timestamps, sorted_timestamps[1:]):
        if (curr - prev).total_seconds() / 3600 > gap_hours:
            episodes += 1
    return episodes


def get_clusters(
    conn,
    recurrence_gap_hours: float = DEFAULT_RECURRENCE_GAP_HOURS,
    model_tiers: dict[str, dict[str, Any]] | None = None,
) -> list[ClusterSummary]:
    """Deterministic root-cause grouping by (category, model_implicated) -- a pure function
    of already-classified alerts, computed at query time rather than persisted.
    """
    rows = get_alerts_for_clustering(conn)
    model_tiers = model_tiers or {}

    groups: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
    for category, model_implicated, severity, issue_summary, triggered_at, platform in rows:
        groups.setdefault((category, model_implicated), []).append(
            (severity, issue_summary, triggered_at, platform)
        )

    summaries = []
    for (category, model_implicated), entries in groups.items():
        latest = max(entries, key=lambda e: e[2])
        earliest = min(entries, key=lambda e: e[2])
        max_severity = max(entries, key=lambda e: _SEVERITY_RANK.get(e[0], 0))[0]
        timestamps = sorted(datetime.fromisoformat(e[2]) for e in entries)
        platforms = sorted({e[3] for e in entries})
        summaries.append(
            ClusterSummary(
                cluster_key=f"{category}:{model_implicated}",
                label=_label(category, model_implicated),
                alert_count=len(entries),
                max_severity=max_severity,
                latest_triggered_at=latest[2],
                first_triggered_at=earliest[2],
                episode_count=_count_episodes(timestamps, recurrence_gap_hours),
                platforms=platforms,
                protection_tier=protection_tier_for(model_tiers, model_implicated),
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
        return get_clusters(
            conn,
            recurrence_gap_hours=settings.recurrence_gap_hours,
            model_tiers=load_model_tiers(),
        )
    finally:
        conn.close()


def main() -> None:
    clusters = list_clusters()
    if not clusters:
        print("No clusters yet -- run `radar score` first.")
        return
    for c in clusters:
        recurring = f"\trecurring x{c.episode_count}" if c.episode_count > 1 else ""
        flagship = "\tFLAGSHIP" if c.protection_tier == "flagship" else ""
        print(
            f"{c.label}\tn={c.alert_count}\tmax_severity={c.max_severity}\t"
            f"latest={c.latest_triggered_at}{recurring}{flagship}\t{c.representative_issue_summary}"
        )


if __name__ == "__main__":
    main()
