from __future__ import annotations

import threading

from radar.db import get_connection, init_db
from radar.scheduler import classify_scheduler_tick, scheduler_loop, scheduler_tick


def test_scheduler_tick_does_nothing_when_disabled(monkeypatch, settings_factory):
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": False, "interval_seconds": 100})
    calls = []
    monkeypatch.setattr("radar.scheduler.run_collection", lambda settings: calls.append(settings))

    result = scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert calls == []
    assert result is None


def test_scheduler_tick_runs_when_never_run_before(monkeypatch, settings_factory):
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": True, "interval_seconds": 100})
    calls = []
    monkeypatch.setattr("radar.scheduler.run_collection", lambda settings: calls.append(settings))

    result = scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert len(calls) == 1
    assert result == 1000.0


def test_scheduler_tick_skips_when_not_enough_time_elapsed(monkeypatch, settings_factory):
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": True, "interval_seconds": 100})
    calls = []
    monkeypatch.setattr("radar.scheduler.run_collection", lambda settings: calls.append(settings))

    # last run 50s ago, interval is 100s -- not due yet
    result = scheduler_tick(settings_factory(), last_run_epoch=1000.0, now_fn=lambda: 1050.0)

    assert calls == []
    assert result == 1000.0  # unchanged


def test_scheduler_tick_runs_once_interval_has_elapsed(monkeypatch, settings_factory):
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": True, "interval_seconds": 100})
    calls = []
    monkeypatch.setattr("radar.scheduler.run_collection", lambda settings: calls.append(settings))

    result = scheduler_tick(settings_factory(), last_run_epoch=1000.0, now_fn=lambda: 1101.0)

    assert len(calls) == 1
    assert result == 1101.0


def test_scheduler_tick_logs_and_continues_when_run_collection_fails(monkeypatch, settings_factory):
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": True, "interval_seconds": 100})

    def _boom(settings):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("radar.scheduler.run_collection", _boom)

    # Must not raise -- a failed run is logged, not propagated, so the loop survives it.
    result = scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert result == 1000.0  # still counted as "ran" (attempted) so it doesn't retry-storm


def test_classify_scheduler_tick_does_nothing_when_disabled(monkeypatch, settings_factory):
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": False, "interval_seconds": 100}
    )
    calls = []
    monkeypatch.setattr("radar.scheduler.run_classification", lambda settings: calls.append(settings))

    result = classify_scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert calls == []
    assert result is None


def test_classify_scheduler_tick_runs_when_never_run_before(monkeypatch, settings_factory):
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": True, "interval_seconds": 100}
    )
    calls = []
    monkeypatch.setattr("radar.scheduler.run_classification", lambda settings: calls.append(settings))

    result = classify_scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert len(calls) == 1
    assert result == 1000.0


def test_classify_scheduler_tick_also_runs_scoring(monkeypatch, settings_factory):
    # Classification alone never produces an alert -- run_scoring() is the
    # only thing that does, so the classify tick must call both, not just
    # run_classification().
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": True, "interval_seconds": 100}
    )
    monkeypatch.setattr("radar.scheduler.run_classification", lambda settings: None)
    calls = []
    monkeypatch.setattr("radar.scheduler.run_scoring", lambda settings: calls.append(settings))

    result = classify_scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert len(calls) == 1
    assert result == 1000.0


def test_classify_scheduler_tick_skips_when_not_enough_time_elapsed(monkeypatch, settings_factory):
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": True, "interval_seconds": 100}
    )
    calls = []
    monkeypatch.setattr("radar.scheduler.run_classification", lambda settings: calls.append(settings))

    # last run 50s ago, interval is 100s -- not due yet
    result = classify_scheduler_tick(settings_factory(), last_run_epoch=1000.0, now_fn=lambda: 1050.0)

    assert calls == []
    assert result == 1000.0  # unchanged


def test_classify_scheduler_tick_runs_once_interval_has_elapsed(monkeypatch, settings_factory):
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": True, "interval_seconds": 100}
    )
    calls = []
    monkeypatch.setattr("radar.scheduler.run_classification", lambda settings: calls.append(settings))

    result = classify_scheduler_tick(settings_factory(), last_run_epoch=1000.0, now_fn=lambda: 1101.0)

    assert len(calls) == 1
    assert result == 1101.0


def test_classify_scheduler_tick_logs_and_continues_when_run_classification_fails(monkeypatch, settings_factory):
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": True, "interval_seconds": 100}
    )

    def _boom(settings):
        raise RuntimeError("simulated API failure")

    monkeypatch.setattr("radar.scheduler.run_classification", _boom)

    # Must not raise -- a failed run is logged, not propagated, so the loop survives it.
    result = classify_scheduler_tick(settings_factory(), last_run_epoch=None, now_fn=lambda: 1000.0)

    assert result == 1000.0  # still counted as "ran" (attempted) so it doesn't retry-storm


def test_scheduler_loop_seeds_last_run_from_db_and_stops_on_event(monkeypatch, settings_factory):
    settings = settings_factory()
    conn = get_connection(settings.database_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO snapshots (post_id, platform, poll_run_id, collected_at, created_at, url, "
        "search_pass) VALUES "
        "('t3_seed', 'reddit', 'run-1', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', "
        "'https://x/t3_seed', 'top')"
    )
    conn.execute(
        "INSERT INTO classifications (post_id, is_pain_point, category, model_implicated, severity, "
        "issue_summary, classifier_model, classified_at, is_advertisement) VALUES "
        "('t3_seed', 1, 'product_bug', 'claude_api_general', 'med', 'test summary', "
        "'claude-haiku-4-5-20251001', '2024-01-02T00:00:00+00:00', 0)"
    )
    conn.commit()
    conn.close()

    # Both sides explicitly isolated from real config/*.yaml, same reasoning as
    # the tests above -- this test cares about seeding/wiring, not about
    # whichever enabled/interval_seconds happen to be saved on disk right now.
    monkeypatch.setattr("radar.scheduler.load_schedule_config", lambda: {"enabled": False, "interval_seconds": 100})
    monkeypatch.setattr(
        "radar.scheduler.load_classify_schedule_config", lambda: {"enabled": False, "interval_seconds": 100}
    )
    seen_last_run_epoch = []
    seen_last_classify_epoch = []

    def _fake_tick(settings, last_run_epoch, now_fn):
        seen_last_run_epoch.append(last_run_epoch)
        return last_run_epoch

    def _fake_classify_tick(settings, last_run_epoch, now_fn):
        seen_last_classify_epoch.append(last_run_epoch)
        return last_run_epoch

    monkeypatch.setattr("radar.scheduler.scheduler_tick", _fake_tick)
    monkeypatch.setattr("radar.scheduler.classify_scheduler_tick", _fake_classify_tick)

    stop_event = threading.Event()

    def _sleep_then_stop(_seconds):
        stop_event.set()

    scheduler_loop(settings, sleep_fn=_sleep_then_stop, stop_event=stop_event)

    assert len(seen_last_run_epoch) == 1
    # Seeded from the real collected_at above (2024-01-01), not None/"never run".
    assert seen_last_run_epoch[0] is not None
    assert seen_last_run_epoch[0] > 0

    assert len(seen_last_classify_epoch) == 1
    # Seeded from the real classified_at above (2024-01-02), not None/"never run".
    assert seen_last_classify_epoch[0] is not None
    assert seen_last_classify_epoch[0] > 0
