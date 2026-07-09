from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

SEARCH_URL = "https://api.stackexchange.com/2.3/search/advanced"

MAX_PAGE_LIMIT = 100  # Stack Exchange's per-request cap

_TAG_RE = re.compile(r"<[^>]+>")

_WINDOW_DELTAS: dict[SearchWindow, timedelta | None] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
    "all": None,
}


class StackOverflowAPIError(RuntimeError):
    """Raised when a Stack Exchange API request fails after exhausting retries."""


class StackOverflowSource:
    """No credentials required -- Stack Exchange's API works unauthenticated at a
    lower shared-IP quota (300/day); an optional STACKOVERFLOW_API_KEY raises that
    to 10,000/day.
    """

    name = "stackoverflow"

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
        self._sleep_fn = sleep_fn

    # -- Source interface -------------------------------------------------

    def search_top(self, query: str, window: SearchWindow, limit: int = 50) -> list[RawPost]:
        delta = _WINDOW_DELTAS.get(window)
        since = datetime.now(timezone.utc) - delta if delta else None
        return self._paginate(query, sort="activity", limit=limit, since=since)

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(query, sort="creation", limit=limit, since=since)
        return [p for p in posts if p.created_at >= since][:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self, query: str, sort: str, limit: int, since: datetime | None
    ) -> list[RawPost]:
        posts: list[RawPost] = []
        page = 1  # Stack Exchange pagination is 1-indexed

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            params: dict[str, Any] = {
                "site": "stackoverflow",
                "q": query,
                "sort": sort,
                "order": "desc",
                "page": page,
                "pagesize": min(MAX_PAGE_LIMIT, limit - len(posts)),
                "filter": "withbody",  # includes the question body -- otherwise only the title is returned
            }
            if since is not None:
                params["fromdate"] = int(since.timestamp())
            if self._settings.stackoverflow_api_key:
                params["key"] = self._settings.stackoverflow_api_key

            data = self._search(params)
            items = data.get("items", [])
            if not items:
                break

            posts.extend(self._map_post(item, matched_term=query) for item in items)

            page += 1
            if not data.get("has_more"):
                break

        return posts[:limit]

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._rate_limited.request("GET", SEARCH_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StackOverflowAPIError(f"Stack Overflow search request failed: {exc}") from exc
        data = response.json()
        # Stack Exchange's documented throttling contract: a `backoff` field
        # in the JSON body (not a header, and can appear on a 200) means
        # "wait this many seconds before your next request" -- ignoring it
        # risks an IP ban, unlike the generic RateLimitedClient's header-only
        # handling which doesn't know about this API's body shape.
        backoff = data.get("backoff")
        if backoff:
            self._sleep_fn(float(backoff))
        return data

    @staticmethod
    def _map_post(item: dict[str, Any], matched_term: str) -> RawPost:
        question_id = item["question_id"]
        owner = item.get("owner") or {}
        title = item.get("title", "")
        body_text = html.unescape(_TAG_RE.sub("", item.get("body") or ""))
        text = f"{title}\n\n{body_text}" if body_text else title

        return RawPost(
            id=f"so_{question_id}",
            platform=Platform.STACK_OVERFLOW,
            author=owner.get("display_name") or "unknown",
            text=text,
            url=item.get("link", f"https://stackoverflow.com/q/{question_id}"),
            created_at=datetime.fromtimestamp(item["creation_date"], tz=timezone.utc),
            metrics=Metrics(
                likes=0,
                comments=item.get("answer_count") or 0,
                score=item.get("score") or 0,
                shares=0,
            ),
            matched_term=matched_term,
        )
