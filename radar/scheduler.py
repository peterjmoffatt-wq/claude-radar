from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from radar.classify import run_classification
from radar.collect import run_collection
from radar.config import Settings, load_classify_schedule_config, load_schedule_config
from radar.db import get_connection, get_last_classified_at, get_last_collected_at, init_db
from radar.score import run_scoring

logger = logging.getLogger("radar.scheduler")

# How often the loop wakes to re-read config/schedule.yaml -- deliberately much
# shorter than any real interval_seconds, so toggling `enabled` or changing the
# interval from the dashboard takes effect within one tick, not on the next
# multi-hour wakeup.
CHECK_INTERVAL_SECONDS = 30


def scheduler_tick(
    settings: Settings,
    last_run_epoch: float | None,
    now_fn: Callable[[], float] = time.time,
) -> float | None:
    """One check: re-reads schedule.yaml fresh, runs a real collection pass if
    enabled and due. Returns the (possibly updated) last_run_epoch -- callers
    thread this back in on the next tick. A failed run is logged, not raised,
    so one bad run doesn't stop future ticks.
    """
    config = load_schedule_config()
    if not config["enabled"]:
        return last_run_epoch

    due = last_run_epoch is None or (now_fn() - last_run_epoch) >= config["interval_seconds"]
    if not due:
        return last_run_epoch

    try:
        run_collection(settings)
    except Exception:
        logger.exception("Scheduled collection run failed")
    return now_fn()


def classify_scheduler_tick(
    settings: Settings,
    last_run_epoch: float | None,
    now_fn: Callable[[], float] = time.time,
) -> float | None:
    """Same shape as scheduler_tick() above, for classification instead of
    collection: independent config (config/classify_schedule.yaml), independent
    interval, independent last-run clock -- collection is free/public APIs so
    it's safe to automate aggressively, classification calls the paid
    Anthropic API, so it's a separate opt-in a user can leave off, or tune to
    a different cadence, without touching collection at all. run_classification()
    itself already no-ops (logs + returns skipped=True) if no API key is
    configured, so this doesn't need its own guard for that.

    Also runs run_scoring() (radar/score.py) right after classifying --
    that's the only thing that actually turns a classified pain point into an
    alert, and it's free/local (no API call), so there's no reason to make it
    a separate opt-in the way classification itself is.
    """
    config = load_classify_schedule_config()
    if not config["enabled"]:
        return last_run_epoch

    due = last_run_epoch is None or (now_fn() - last_run_epoch) >= config["interval_seconds"]
    if not due:
        return last_run_epoch

    try:
        run_classification(settings)
        run_scoring(settings)
    except Exception:
        logger.exception("Scheduled classification run failed")
    return now_fn()


def scheduler_loop(
    settings: Settings,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Runs until `stop_event` is set (never, if not given). Seeds "last run"
    from real history (get_last_collected_at()/get_last_classified_at()) rather
    than "never run", so a `radar serve` restart doesn't immediately re-fire a
    pass that already happened moments before.
    """
    conn = get_connection(settings.database_path)
    init_db(conn)
    last_collected_at = get_last_collected_at(conn)
    last_classified_at = get_last_classified_at(conn)
    conn.close()
    last_run_epoch = last_collected_at.timestamp() if last_collected_at else None
    last_classify_epoch = last_classified_at.timestamp() if last_classified_at else None

    while stop_event is None or not stop_event.is_set():
        sleep_fn(CHECK_INTERVAL_SECONDS)
        last_run_epoch = scheduler_tick(settings, last_run_epoch, now_fn=now_fn)
        last_classify_epoch = classify_scheduler_tick(settings, last_classify_epoch, now_fn=now_fn)


def start_scheduler_thread(settings: Settings) -> threading.Thread:
    thread = threading.Thread(
        target=scheduler_loop, args=(settings,), daemon=True, name="radar-scheduler"
    )
    thread.start()
    return thread
