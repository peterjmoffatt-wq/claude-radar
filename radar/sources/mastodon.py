from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

MAX_PAGE_LIMIT = 40  # Mastodon's per-request cap for search

_TAG_RE = re.compile(r"<[^>]+>")


class CredentialsMissingError(RuntimeError):
    """Raised when Mastodon API calls are attempted without a configured instance/token."""


class MastodonAPIError(RuntimeError):
    """Raised when a Mastodon API request fails after exhausting retries."""


class MastodonSource:
    """Searches one configured Mastodon instance's known/federated statuses --
    not "all of Mastodon" (there is no such global index; the protocol is
    federated). Status search requires a bearer token even though account/
    hashtag search doesn't -- confirmed live against mastodon.social before
    writing this (unauthenticated status search returns an empty list, not an
    error, which would otherwise look like "no results" instead of "no access").

    The search endpoint has no engagement-based sort param at all, unlike every
    other source here -- results come back in the instance's own search-relevance
    order. `search_top` fetches a few pages and sorts client-side by
    (favourites + reblogs); `search_recent` relies on `created_at` plus an
    early-stop heuristic, since strict chronological ordering isn't guaranteed
    by the API contract.
    """

    name = "mastodon"

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
        posts = self._paginate(query, limit=max(limit, MAX_PAGE_LIMIT))
        posts.sort(key=lambda p: p.metrics.likes + p.metrics.shares, reverse=True)
        return posts[:limit]

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(query, limit=limit, since=since)
        posts = [p for p in posts if p.created_at >= since]
        posts.sort(key=lambda p: p.created_at, reverse=True)
        return posts[:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self, query: str, limit: int, since: datetime | None = None
    ) -> list[RawPost]:
        posts: list[RawPost] = []
        offset = 0

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            params: dict[str, Any] = {
                "q": query,
                "type": "statuses",
                "limit": min(MAX_PAGE_LIMIT, limit - len(posts)),
                "offset": offset,
            }

            data = self._search(params)
            statuses = data.get("statuses", [])
            if not statuses:
                break

            page_posts = [self._map_post(s, matched_term=query) for s in statuses]
            posts.extend(page_posts)
            offset += len(statuses)

            # Ordering isn't guaranteed, so this is a heuristic, not a hard
            # stop: only bail early if *every* post on this page already
            # predates `since` -- a mixed page keeps paginating.
            if since is not None and all(p.created_at < since for p in page_posts):
                break
            if len(statuses) < params["limit"]:
                break

        return posts

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.has_mastodon_credentials():
            raise CredentialsMissingError(
                "MASTODON_INSTANCE_URL / MASTODON_ACCESS_TOKEN are not configured"
            )

        url = f"{self._settings.mastodon_instance_url.rstrip('/')}/api/v2/search"
        try:
            response = self._rate_limited.request(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._settings.mastodon_access_token}"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MastodonAPIError(f"Mastodon search request failed: {exc}") from exc
        return response.json()

    @staticmethod
    def _map_post(status: dict[str, Any], matched_term: str) -> RawPost:
        account = status.get("account") or {}
        # Status content is HTML -- strip tags and unescape entities. This
        # parses the API's own JSON field; it isn't scraping a rendered page.
        text = html.unescape(_TAG_RE.sub("", status.get("content") or ""))

        return RawPost(
            id=f"mastodon_{status['id']}",
            platform=Platform.MASTODON,
            author=account.get("acct") or "unknown",
            text=text,
            url=status.get("url") or status.get("uri") or "",
            created_at=datetime.fromisoformat(status["created_at"].replace("Z", "+00:00")),
            metrics=Metrics(
                likes=status.get("favourites_count") or 0,
                comments=status.get("replies_count") or 0,
                score=0,
                shares=status.get("reblogs_count") or 0,
            ),
            matched_term=matched_term,
        )
