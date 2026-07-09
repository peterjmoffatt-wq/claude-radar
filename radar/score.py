from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime

from radar.config import Settings, get_settings
from radar.db import (
    get_connection,
    get_latest_alert_velocity,
    get_pain_point_posts,
    get_snapshot_history,
    init_db,
    write_alert,
)

logger = logging.getLogger("radar.score")


def compute_velocity(history: list[tuple[datetime, float]]) -> float | None:
    """Virality-score change per hour between the two most recent snapshots.

    None if there's fewer than two data points yet, or they're timestamped
    identically (no elapsed time to compute a rate over).
    """
    if len(history) < 2:
        return None

    prev_at, prev_score = history[-2]
    latest_at, latest_score = history[-1]
    elapsed_hours = (latest_at - prev_at).total_seconds() / 3600
    if elapsed_hours <= 0:
        return None

    return (latest_score - prev_score) / elapsed_hours


@dataclass
class ScoringResult:
    alerts_written: int
    skipped: bool


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def run_scoring(settings: Settings | None = None) -> ScoringResult:
    settings = settings or get_settings()

    conn = get_connection(settings.database_path)
    init_db(conn)

    try:
        pain_points = get_pain_point_posts(conn)
        if not pain_points:
            return ScoringResult(alerts_written=0, skipped=True)

        written = 0
        for post_id, category, severity, platform in pain_points:
            history = get_snapshot_history(conn, post_id)
            velocity = compute_velocity(history)
            if velocity is None or velocity < settings.velocity_threshold_for(platform):
                continue

            last_alert_velocity = get_latest_alert_velocity(conn, post_id)
            if last_alert_velocity is not None and velocity <= last_alert_velocity:
                # Suppress repeat alerts unless engagement is accelerating further.
                continue

            qa_status = "pending" if category in settings.human_qa_categories else "not_required"
            latest_score = history[-1][1]
            write_alert(conn, post_id, latest_score, velocity, category, severity, qa_status)
            written += 1
            logger.info(
                "post_id=%s velocity=%.2f qa_status=%s", post_id, velocity, qa_status
            )
    finally:
        conn.close()

    return ScoringResult(alerts_written=written, skipped=False)


def main() -> None:
    configure_logging()
    result = run_scoring()
    if result.skipped:
        sys.exit(0)
    print(f"Wrote {result.alerts_written} alert rows.")


if __name__ == "__main__":
    main()
