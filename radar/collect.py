from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from radar.config import Settings, client_scoped_terms, effective_terms, get_settings, load_search_terms
from radar.db import get_connection, get_last_collected_at, init_db, purge_old_raw_text, write_snapshots
from radar.sources.github import GitHubSource
from radar.sources.hackernews import HackerNewsSource
from radar.sources.mastodon import MastodonSource
from radar.sources.reddit import RedditSource
from radar.sources.stackoverflow import StackOverflowSource
from radar.sources.x import XSource
from radar.sources.youtube import YouTubeSource

logger = logging.getLogger("radar.collect")


@dataclass
class CollectionResult:
    snapshots_written: int
    skipped: bool
    sources_run: list[str]
    # Sources that were configured/attempted but errored out (e.g. a live
    # timeout or exhausted retries) before finishing their terms -- a subset
    # of sources_run, not a separate list of names.
    sources_failed: list[str] = field(default_factory=list)


def source_availability(settings: Settings) -> dict[str, bool]:
    """Which real sources are currently configured (credentials present, or
    enabled for the keyless ones) -- drives both `_configured_sources()` below
    and the `/api/sources` endpoint, so the two can't drift.
    """
    return {
        "reddit": settings.has_reddit_credentials(),
        "youtube": settings.has_youtube_credentials(),
        "x": settings.has_x_credentials(),
        "hackernews": settings.enable_hackernews_source,
        "stackoverflow": settings.enable_stackoverflow_source,
        "github": settings.has_github_credentials(),
        "mastodon": settings.has_mastodon_credentials(),
    }


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

    if settings.has_mastodon_credentials():
        sources.append(("mastodon", MastodonSource(settings, sleep_fn=sleep_fn)))
    else:
        logger.info(
            "Mastodon source not configured (MASTODON_INSTANCE_URL/MASTODON_ACCESS_TOKEN); "
            "skipping."
        )

    return sources


def run_collection(
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    sources: set[str] | None = None,
) -> CollectionResult:
    """`sources`, if given, narrows collection to that subset of otherwise-
    configured source names (e.g. from the dashboard's source picker) --
    `None` preserves the original "poll everything configured" behavior the
    CLI relies on.
    """
    settings = settings or get_settings()
    search_config = load_search_terms()
    all_configured = _configured_sources(settings, search_config, sleep_fn)
    configured_sources = (
        all_configured
        if sources is None
        else [(name, src) for name, src in all_configured if name in sources]
    )

    if not configured_sources:
        logger.warning("No sources configured; skipping collection.")
        return CollectionResult(snapshots_written=0, skipped=True, sources_run=[])

    conn = get_connection(settings.database_path)
    init_db(conn)

    poll_run_id = str(uuid.uuid4())
    # Prefer the actual last collection timestamp over the configured interval
    # -- a late/missed run (laptop asleep, cron skipped a tick) would otherwise
    # leave a silent gap of posts created but never captured by the "recent"
    # pass. Only an empty database (first-ever run) falls back to the interval.
    since = get_last_collected_at(conn) or (
        datetime.now(timezone.utc) - timedelta(seconds=settings.poll_interval_seconds)
    )
    seen: set[tuple[str, str, str]] = set()  # (source_name, post_id, search_pass)
    total = 0
    sources_failed: list[str] = []
    # Client x risk-pattern terms are meant to catch a targeted-attack report
    # anywhere, not just in the subreddits configured for generic terms --
    # every other source already searches site-wide, so this is Reddit-only.
    client_scoped = client_scoped_terms(search_config)

    try:
        for source_name, source in configured_sources:
            try:
                for term in effective_terms(search_config):
                    reddit_kwargs = (
                        {"site_wide": term in client_scoped} if source_name == "reddit" else {}
                    )
                    passes = (
                        (
                            "top",
                            source.search_top(term, window="week", limit=settings.top_n, **reddit_kwargs),
                        ),
                        (
                            "recent",
                            source.search_recent(term, since=since, limit=settings.top_n, **reddit_kwargs),
                        ),
                    )
                    for search_pass, posts in passes:
                        fresh = [p for p in posts if (source_name, p.id, search_pass) not in seen]
                        seen.update((source_name, p.id, search_pass) for p in fresh)
                        total += write_snapshots(
                            conn, fresh, poll_run_id, search_pass=search_pass, settings=settings
                        )
                        logger.info(
                            "source=%s term=%r pass=%s wrote=%d",
                            source_name,
                            term,
                            search_pass,
                            len(fresh),
                        )
            except Exception:
                # A live source failing (timeout, exhausted retries, transient
                # 5xx) must not take down every OTHER configured source's
                # results in the same run -- especially now that this is
                # reachable from a synchronous, user-facing button click
                # (POST /api/collect), not just an unattended CLI poll.
                # Rows already written for this and prior sources are safe:
                # write_snapshots() commits per pass, not once at the end.
                logger.exception(
                    "source=%s failed; skipping its remaining terms for this run.", source_name
                )
                sources_failed.append(source_name)
        purge_old_raw_text(conn, settings.raw_text_retention_days)
    finally:
        conn.close()

    return CollectionResult(
        snapshots_written=total,
        skipped=False,
        sources_run=[name for name, _ in configured_sources],
        sources_failed=sources_failed,
    )


def main() -> None:
    configure_logging()
    result = run_collection()
    if result.skipped:
        sys.exit(0)
    print(f"Wrote {result.snapshots_written} snapshot rows.")


if __name__ == "__main__":
    main()
