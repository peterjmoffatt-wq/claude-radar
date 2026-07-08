from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.backtest import run_backtest

BASE = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _incident(name, starts_at, ends_at):
    return {"name": name, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}


def test_hit_when_alert_fires_before_incident_window():
    starts_at = BASE
    ends_at = BASE + timedelta(hours=6)
    incidents = [_incident("outage", starts_at, ends_at)]
    # Alerted 2 hours before the incident officially started.
    timestamps = [BASE - timedelta(hours=2)]

    report = run_backtest(timestamps, incidents)

    assert report.hit_rate == 1.0
    assert report.results[0].hit is True
    assert report.results[0].lead_time_seconds == timedelta(hours=2).total_seconds()


def test_hit_when_alert_fires_during_incident_window():
    starts_at = BASE
    ends_at = BASE + timedelta(hours=6)
    incidents = [_incident("outage", starts_at, ends_at)]
    timestamps = [BASE + timedelta(hours=3)]  # inside the window

    report = run_backtest(timestamps, incidents)

    result = report.results[0]
    assert result.hit is True
    assert result.lead_time_seconds == -timedelta(hours=3).total_seconds()  # alerted late


def test_miss_when_no_alert_in_window():
    starts_at = BASE
    ends_at = BASE + timedelta(hours=6)
    incidents = [_incident("outage", starts_at, ends_at)]
    timestamps = [BASE - timedelta(days=30)]  # way outside the lookback

    report = run_backtest(timestamps, incidents)

    assert report.results[0].hit is False
    assert report.results[0].lead_time_seconds is None
    assert report.hit_rate == 0.0


def test_lookback_bounds_how_far_back_a_hit_can_be_found():
    starts_at = BASE
    ends_at = BASE + timedelta(hours=6)
    incidents = [_incident("outage", starts_at, ends_at)]
    timestamps = [BASE - timedelta(days=10)]

    just_outside = run_backtest(timestamps, incidents, lookback=timedelta(days=7))
    assert just_outside.results[0].hit is False

    wide_enough = run_backtest(timestamps, incidents, lookback=timedelta(days=14))
    assert wide_enough.results[0].hit is True


def test_hit_rate_aggregates_across_multiple_incidents():
    incidents = [
        _incident("hit-one", BASE, BASE + timedelta(hours=1)),
        _incident("miss-one", BASE + timedelta(days=30), BASE + timedelta(days=30, hours=1)),
    ]
    timestamps = [BASE - timedelta(hours=1)]  # only covers the first incident

    report = run_backtest(timestamps, incidents)

    assert report.results[0].hit is True
    assert report.results[1].hit is False
    assert report.hit_rate == 0.5
