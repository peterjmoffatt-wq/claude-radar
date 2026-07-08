from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from radar.config import Settings, get_settings, load_search_terms
from radar.db import get_connection, init_db, write_snapshots
from radar.sources.reddit import RedditSource

logger = logging.getLogger("radar.collect")


@dataclass
class CollectionResult:
    snapshots_written: int
    skipped: bool


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def run_collection(
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    settings = settings or get_settings()

    if not settings.has_reddit_credentials():
        logger.warning(
            "Reddit credentials missing (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET); "
            "skipping Reddit collection."
        )
        return CollectionResult(snapshots_written=0, skipped=True)

    search_config = load_search_terms()
    source = RedditSource(
        settings, subreddits=search_config.get("subreddits", []), sleep_fn=sleep_fn
    )

    conn = get_connection(settings.database_path)
    init_db(conn)

    poll_run_id = str(uuid.uuid4())
    since = datetime.now(timezone.utc) - timedelta(seconds=settings.poll_interval_seconds)
    seen: set[tuple[str, str]] = set()
    total = 0

    try:
        for term in search_config.get("terms", []):
            passes = (
                ("top", source.search_top(term, window="week", limit=settings.top_n)),
                ("recent", source.search_recent(term, since=since, limit=settings.top_n)),
            )
            for search_pass, posts in passes:
                fresh = [p for p in posts if (p.id, search_pass) not in seen]
                seen.update((p.id, search_pass) for p in fresh)
                total += write_snapshots(
                    conn, fresh, poll_run_id, search_pass=search_pass, settings=settings
                )
                logger.info("term=%r pass=%s wrote=%d", term, search_pass, len(fresh))
    finally:
        conn.close()

    return CollectionResult(snapshots_written=total, skipped=False)


def main() -> None:
    configure_logging()
    result = run_collection()
    if result.skipped:
        sys.exit(0)
    print(f"Wrote {result.snapshots_written} snapshot rows.")


if __name__ == "__main__":
    main()
