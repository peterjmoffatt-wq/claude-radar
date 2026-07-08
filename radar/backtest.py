from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from radar.config import Settings, get_settings, load_known_incidents
from radar.db import get_alert_timestamps, get_connection, init_db

DEFAULT_LOOKBACK = timedelta(days=7)


@dataclass
class IncidentResult:
    name: str
    hit: bool
    # Positive: an alert fired before the incident window even started.
    lead_time_seconds: float | None


@dataclass
class BacktestReport:
    results: list[IncidentResult]

    @property
    def hit_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.hit) / len(self.results)


def _earliest_in_window(
    timestamps: list[datetime], starts_at: datetime, ends_at: datetime, lookback: timedelta
) -> datetime | None:
    window_start = starts_at - lookback
    candidates = [t for t in timestamps if window_start <= t <= ends_at]
    return min(candidates) if candidates else None


def run_backtest(
    alert_timestamps: list[datetime],
    incidents: list[dict[str, Any]],
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> BacktestReport:
    results = []
    for incident in incidents:
        starts_at = datetime.fromisoformat(incident["starts_at"])
        ends_at = datetime.fromisoformat(incident["ends_at"])
        earliest = _earliest_in_window(alert_timestamps, starts_at, ends_at, lookback)
        if earliest is None:
            results.append(IncidentResult(incident["name"], hit=False, lead_time_seconds=None))
        else:
            lead_time = (starts_at - earliest).total_seconds()
            results.append(IncidentResult(incident["name"], hit=True, lead_time_seconds=lead_time))
    return BacktestReport(results)


def main() -> None:
    settings: Settings = get_settings()
    incidents = load_known_incidents()
    if not incidents:
        print("No known incidents configured -- edit config/known_incidents.yaml.")
        return

    conn = get_connection(settings.database_path)
    init_db(conn)
    try:
        timestamps = get_alert_timestamps(conn)
    finally:
        conn.close()

    report = run_backtest(timestamps, incidents)
    for r in report.results:
        if r.hit:
            print(f"HIT  {r.name}: lead_time={r.lead_time_seconds / 60:.1f} min")
        else:
            print(f"MISS {r.name}: no alert found in window")
    print(f"Hit rate: {report.hit_rate:.0%}")


if __name__ == "__main__":
    main()
