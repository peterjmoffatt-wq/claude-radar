from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from radar.config import Settings, get_settings, load_search_terms
from radar.db import get_connection, init_db, write_snapshots
from radar.sources.github import GitHubSource
from radar.sources.hackernews import HackerNewsSource
from radar.sources.reddit import RedditSource
from radar.sources.stackoverflow import StackOverflowSource
from radar.sources.x import XSource
from radar.sources.youtube import YouTubeSource

logger = logging.getLogger("radar.collect")


@dataclass
class CollectionResult:
    snapshots_written: int
    skipped: bool


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _configured_sources(
    settings: Settings, search_config: dict[str, Any], sleep_fn: Callable[[float], None]
) -> list[tuple[str, Any]]:
    sources: list[tuple[str, Any]] = []

    if settings.has_reddit_credentials():
        sources.append(
            (
                "reddit",
                RedditSource(
                    settings, subreddits=search_config.get("subreddits", []), sleep_fn=sleep_fn
                ),
            )
        )
    else:
        logger.warning(
            "Reddit credentials missing (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET); "
            "skipping Reddit collection."
        )

    if settings.has_youtube_credentials():
        sources.append(("youtube", YouTubeSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.warning("YOUTUBE_API_KEY missing; skipping YouTube collection.")

    if settings.has_x_credentials():
        sources.append(("x", XSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.info("X source not enabled (ENABLE_X_SOURCE/X_BEARER_TOKEN); skipping.")

    if settings.enable_hackernews_source:
        sources.append(("hackernews", HackerNewsSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.info("Hacker News source disabled (ENABLE_HACKERNEWS_SOURCE=false); skipping.")

    if settings.enable_stackoverflow_source:
        sources.append(("stackoverflow", StackOverflowSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.info(
            "Stack Overflow source disabled (ENABLE_STACKOVERFLOW_SOURCE=false); skipping."
        )

    if settings.has_github_credentials():
        sources.append(("github", GitHubSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.warning("GITHUB_TOKEN missing; skipping GitHub Issues collection.")

    return sources


def run_collection(
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    settings = settings or get_settings()
    search_config = load_search_terms()
    sources = _configured_sources(settings, search_config, sleep_fn)

    if not sources:
        logger.warning("No sources configured; skipping collection.")
        return CollectionResult(snapshots_written=0, skipped=True)

    conn = get_connection(settings.database_path)
    init_db(conn)

    poll_run_id = str(uuid.uuid4())
    since = datetime.now(timezone.utc) - timedelta(seconds=settings.poll_interval_seconds)
    seen: set[tuple[str, str, str]] = set()  # (source_name, post_id, search_pass)
    total = 0

    try:
        for source_name, source in sources:
            for term in search_config.get("terms", []):
                passes = (
                    ("top", source.search_top(term, window="week", limit=settings.top_n)),
                    ("recent", source.search_recent(term, since=since, limit=settings.top_n)),
                )
                for search_pass, posts in passes:
                    fresh = [p for p in posts if (source_name, p.id, search_pass) not in seen]
                    seen.update((source_name, p.id, search_pass) for p in fresh)
                    total += write_snapshots(
                        conn, fresh, poll_run_id, search_pass=search_pass, settings=settings
                    )
                    logger.info(
                        "source=%s term=%r pass=%s wrote=%d", source_name, term, search_pass, len(fresh)
                    )
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
