from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"

MAX_PAGE_LIMIT = 100  # X API v2's per-request cap for recent search

_WINDOW_DELTAS: dict[SearchWindow, timedelta | None] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),  # recent search only covers ~7 days on the standard tier anyway
    "year": timedelta(days=365),
    "all": None,
}

TWEET_FIELDS = "created_at,public_metrics,author_id"


class CredentialsMissingError(RuntimeError):
    """Raised when X API calls are attempted without ENABLE_X_SOURCE + a bearer token."""


class XAPIError(RuntimeError):
    """Raised when an X API request fails after exhausting retries."""


class XSource:
    """X/Twitter recent-search source. Behind the ENABLE_X_SOURCE feature flag -- see
    Settings.has_x_credentials(). Modern X API v2 has no free tier for search, so this is
    fully unit-tested via mocked HTTP but inert without a paid bearer token.
    """

    name = "x"

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
        start_time = datetime.now(timezone.utc) - delta if delta else None
        return self._paginate(query, sort_order="relevancy", limit=limit, start_time=start_time)

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(query, sort_order="recency", limit=limit, start_time=since)
        return [p for p in posts if p.created_at >= since][:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self, query: str, sort_order: str, limit: int, start_time: datetime | None
    ) -> list[RawPost]:
        if not self._settings.has_x_credentials():
            raise CredentialsMissingError(
                "ENABLE_X_SOURCE and X_BEARER_TOKEN must both be set to use the X source"
            )

        posts: list[RawPost] = []
        next_token: str | None = None

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            params: dict[str, Any] = {
                "query": query,
                "sort_order": sort_order,
                "tweet.fields": TWEET_FIELDS,
                "max_results": min(MAX_PAGE_LIMIT, max(10, limit - len(posts))),
            }
            if start_time is not None:
                params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            if next_token:
                params["next_token"] = next_token

            data = self._search(params)
            tweets = data.get("data", [])
            if not tweets:
                break

            posts.extend(self._map_post(tweet, matched_term=query) for tweet in tweets)

            next_token = data.get("meta", {}).get("next_token")
            if not next_token:
                break

        return posts[:limit]

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._rate_limited.request(
                "GET",
                SEARCH_URL,
                params=params,
                headers={"Authorization": f"Bearer {self._settings.x_bearer_token}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise XAPIError(f"X search request failed: {exc}") from exc
        return response.json()

    @staticmethod
    def _map_post(tweet: dict[str, Any], matched_term: str) -> RawPost:
        metrics = tweet.get("public_metrics", {})
        return RawPost(
            id=f"x_{tweet['id']}",
            platform=Platform.X,
            author=tweet.get("author_id", "unknown"),
            text=tweet.get("text", ""),
            url=f"https://x.com/i/web/status/{tweet['id']}",
            created_at=datetime.fromisoformat(tweet["created_at"]),
            metrics=Metrics(
                likes=metrics.get("like_count", 0),
                comments=metrics.get("reply_count", 0),
                score=metrics.get("retweet_count", 0),
                shares=metrics.get("quote_count", 0),
            ),
            matched_term=matched_term,
        )
