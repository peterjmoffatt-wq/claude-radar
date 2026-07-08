from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

TOKEN_EXPIRY_BUFFER_SECONDS = 60
MAX_PAGE_LIMIT = 100  # Reddit's per-request cap


class CredentialsMissingError(RuntimeError):
    """Raised when Reddit API calls are attempted without configured credentials."""


class RedditAPIError(RuntimeError):
    """Raised when a Reddit API request fails after exhausting retries."""


class RedditSource:
    name = "reddit"

    def __init__(
        self,
        settings: Settings,
        subreddits: list[str],
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_pages: int = 10,
    ) -> None:
        self._settings = settings
        self._subreddit_path = "+".join(subreddits)
        self._client = client or httpx.Client()
        self._rate_limited = RateLimitedClient(self._client, sleep_fn=sleep_fn)
        self._max_pages = max_pages
        self._access_token: str | None = None
        self._token_expires_at: float | None = None

    # -- auth -----------------------------------------------------------

    def _get_access_token(self) -> str:
        if not self._settings.has_reddit_credentials():
            raise CredentialsMissingError(
                "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not configured"
            )

        if self._access_token and self._token_expires_at and time.monotonic() < self._token_expires_at:
            return self._access_token

        response = self._rate_limited.request(
            "POST",
            TOKEN_URL,
            auth=(self._settings.reddit_client_id, self._settings.reddit_client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self._settings.reddit_user_agent},
        )
        response.raise_for_status()
        payload = response.json()

        self._access_token = payload["access_token"]
        expires_in = payload.get("expires_in", 3600)
        self._token_expires_at = time.monotonic() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS
        return self._access_token

    # -- Source interface -------------------------------------------------

    def search_top(self, query: str, window: SearchWindow, limit: int = 50) -> list[RawPost]:
        params = {"sort": "top", "t": window}
        posts = self._paginate(query, params, limit)
        return posts[:limit]

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        params = {"sort": "new"}
        posts = self._paginate(query, params, limit, since=since)
        posts = [p for p in posts if p.created_at >= since]
        return posts[:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self,
        query: str,
        extra_params: dict[str, Any],
        limit: int,
        since: datetime | None = None,
    ) -> list[RawPost]:
        posts: list[RawPost] = []
        after: str | None = None

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            page_limit = min(MAX_PAGE_LIMIT, limit - len(posts))
            params = {
                "q": query,
                "restrict_sr": "true",
                "raw_json": "1",
                "limit": page_limit,
                **extra_params,
            }
            if after:
                params["after"] = after

            data = self._search(params)
            listing = data.get("data", {})
            children = listing.get("children", [])
            if not children:
                break

            page_posts = [self._map_post(child["data"], matched_term=query) for child in children]
            posts.extend(page_posts)

            after = listing.get("after")

            if since is not None and page_posts and page_posts[-1].created_at < since:
                break
            if not after:
                break

        return posts

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        token = self._get_access_token()
        url = f"{API_BASE}/r/{self._subreddit_path}/search"
        try:
            response = self._rate_limited.request(
                "GET",
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self._settings.reddit_user_agent,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RedditAPIError(f"Reddit search request failed: {exc}") from exc
        return response.json()

    @staticmethod
    def _map_post(data: dict[str, Any], matched_term: str) -> RawPost:
        text = data.get("title", "")
        selftext = data.get("selftext")
        if selftext:
            text = f"{text}\n\n{selftext}"

        return RawPost(
            id=data["name"],
            platform=Platform.REDDIT,
            author=data.get("author", "[deleted]"),
            text=text,
            url=f"https://www.reddit.com{data['permalink']}",
            created_at=datetime.fromtimestamp(data["created_utc"], tz=timezone.utc),
            metrics=Metrics(
                likes=0,
                comments=data.get("num_comments", 0),
                score=data.get("score", 0),
                shares=0,
            ),
            subreddit=data.get("subreddit"),
            matched_term=matched_term,
        )
