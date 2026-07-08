from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

from radar.config import Settings, get_settings
from radar.db import get_connection, get_first_seen_by_pass, init_db


@dataclass
class LeadTimeEntry:
    post_id: str
    first_recent_seen_at: datetime | None
    first_top_seen_at: datetime | None
    # Positive: the 'recent' (early-warning) pass caught this post before it was prominent
    # enough to surface in the 'top' (most-engaged) pass -- our proxy for "went viral"
    # absent external ground truth. None if we don't have both passes for this post.
    lead_time_seconds: float | None


def compute_lead_times(conn) -> list[LeadTimeEntry]:
    rows = get_first_seen_by_pass(conn)

    by_post: dict[str, dict[str, datetime]] = {}
    for post_id, search_pass, first_collected_at in rows:
        by_post.setdefault(post_id, {})[search_pass] = datetime.fromisoformat(first_collected_at)

    entries = []
    for post_id, passes in by_post.items():
        recent_at = passes.get("recent")
        top_at = passes.get("top")
        lead_time = (top_at - recent_at).total_seconds() if recent_at and top_at else None
        entries.append(LeadTimeEntry(post_id, recent_at, top_at, lead_time))
    return entries


def summarize_lead_times(entries: list[LeadTimeEntry]) -> dict:
    positive = [
        e.lead_time_seconds for e in entries if e.lead_time_seconds is not None and e.lead_time_seconds > 0
    ]
    return {
        "posts_with_both_passes": sum(1 for e in entries if e.lead_time_seconds is not None),
        "posts_caught_early": len(positive),
        "median_lead_time_seconds": median(positive) if positive else None,
        "mean_lead_time_seconds": mean(positive) if positive else None,
    }


def get_lead_time_summary(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    try:
        return summarize_lead_times(compute_lead_times(conn))
    finally:
        conn.close()


def main() -> None:
    summary = get_lead_time_summary()
    print(f"Posts seen by both passes: {summary['posts_with_both_passes']}")
    print(f"Posts caught early (positive lead time): {summary['posts_caught_early']}")
    median_s = summary["median_lead_time_seconds"]
    mean_s = summary["mean_lead_time_seconds"]
    print(f"Median lead time: {median_s / 60:.1f} min" if median_s is not None else "Median lead time: n/a")
    print(f"Mean lead time: {mean_s / 60:.1f} min" if mean_s is not None else "Mean lead time: n/a")


if __name__ == "__main__":
    main()
