from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"

MAX_PAGE_LIMIT = 50  # kept consistent with the other sources; Algolia allows up to 1000

_WINDOW_DELTAS: dict[SearchWindow, timedelta | None] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
    "all": None,
}


class HackerNewsAPIError(RuntimeError):
    """Raised when a Hacker News (Algolia) API request fails after exhausting retries."""


class HackerNewsSource:
    """No credentials needed at all -- Algolia's HN Search API is free and keyless."""

    name = "hackernews"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_pages: int = 10,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client()
        self._rate_limited = RateLimitedClient(self._client, sleep_fn=sleep_fn)
        self._max_pages = max_pages

    # -- Source interface -------------------------------------------------

    def search_top(self, query: str, window: SearchWindow, limit: int = 50) -> list[RawPost]:
        delta = _WINDOW_DELTAS.get(window)
        since = datetime.now(timezone.utc) - delta if delta else None
        return self._paginate(SEARCH_URL, query, limit=limit, since=since)

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(SEARCH_BY_DATE_URL, query, limit=limit, since=since)
        return [p for p in posts if p.created_at >= since][:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self, url: str, query: str, limit: int, since: datetime | None
    ) -> list[RawPost]:
        posts: list[RawPost] = []
        page = 0

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            params: dict[str, Any] = {
                "query": query,
                "tags": "story",
                "page": page,
                "hitsPerPage": min(MAX_PAGE_LIMIT, limit - len(posts)),
            }
            if since is not None:
                params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"

            data = self._search(url, params)
            hits = data.get("hits", [])
            if not hits:
                break

            posts.extend(self._map_post(hit, matched_term=query) for hit in hits)

            page += 1
            if page >= data.get("nbPages", page):
                break

        return posts[:limit]

    def _search(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._rate_limited.request("GET", url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HackerNewsAPIError(f"Hacker News search request failed: {exc}") from exc
        return response.json()

    @staticmethod
    def _map_post(hit: dict[str, Any], matched_term: str) -> RawPost:
        title = hit.get("title") or ""
        story_text = hit.get("story_text")
        text = f"{title}\n\n{story_text}" if story_text else title
        object_id = hit["objectID"]

        return RawPost(
            id=f"hn_{object_id}",
            platform=Platform.HACKERNEWS,
            author=hit.get("author") or "unknown",
            text=text,
            url=hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
            created_at=datetime.fromisoformat(hit["created_at"]),
            metrics=Metrics(
                likes=0,
                comments=hit.get("num_comments") or 0,
                score=hit.get("points") or 0,
                shares=0,
            ),
            matched_term=matched_term,
        )
