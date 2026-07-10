from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from radar.collect import run_collection
from radar.config import Settings, load_schedule_config
from radar.db import get_connection, get_last_collected_at, init_db

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


def scheduler_loop(
    settings: Settings,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Runs until `stop_event` is set (never, if not given). Seeds "last run"
    from the real `collected_at` history (get_last_collected_at()) rather than
    "never run", so a `radar serve` restart doesn't immediately re-fire a
    collection pass that already happened moments before.
    """
    conn = get_connection(settings.database_path)
    init_db(conn)
    last_run_at = get_last_collected_at(conn)
    conn.close()
    last_run_epoch = last_run_at.timestamp() if last_run_at else None

    while stop_event is None or not stop_event.is_set():
        sleep_fn(CHECK_INTERVAL_SECONDS)
        last_run_epoch = scheduler_tick(settings, last_run_epoch, now_fn=now_fn)


def start_scheduler_thread(settings: Settings) -> threading.Thread:
    thread = threading.Thread(
        target=scheduler_loop, args=(settings,), daemon=True, name="radar-scheduler"
    )
    thread.start()
    return thread
