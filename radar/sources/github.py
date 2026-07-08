from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

SEARCH_URL = "https://api.github.com/search/issues"
API_VERSION = "2022-11-28"

MAX_PAGE_LIMIT = 100  # GitHub's per-request cap (search results capped at 1000 total)

_WINDOW_DELTAS: dict[SearchWindow, timedelta | None] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
    "all": None,
}


class CredentialsMissingError(RuntimeError):
    """Raised when GitHub API calls are attempted without a configured token."""


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request fails after exhausting retries."""


class GitHubSource:
    """Scoped to Issues only (`is:issue` in every query) -- Pull Requests share this
    same search endpoint but aren't what this source is for. Discussions aren't
    covered by this REST endpoint at all (they need the separate GraphQL API), so
    they're out of scope rather than bolting on a second client for one source.
    """

    name = "github"

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
        return self._paginate(query, sort="reactions", limit=limit, since=since)

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(query, sort="created", limit=limit, since=since)
        return [p for p in posts if p.created_at >= since][:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self, query: str, sort: str, limit: int, since: datetime | None
    ) -> list[RawPost]:
        if not self._settings.github_token:
            raise CredentialsMissingError("GITHUB_TOKEN is not configured")

        q = f"{query} is:issue"
        if since is not None:
            q += f" created:>{since.strftime('%Y-%m-%dT%H:%M:%S+00:00')}"

        posts: list[RawPost] = []
        page = 1

        for _ in range(self._max_pages):
            if len(posts) >= limit:
                break

            params: dict[str, Any] = {
                "q": q,
                "sort": sort,
                "order": "desc",
                "page": page,
                "per_page": min(MAX_PAGE_LIMIT, limit - len(posts)),
            }

            response = self._search(params)
            data = response.json()
            items = data.get("items", [])
            if not items:
                break

            posts.extend(self._map_post(item, matched_term=query) for item in items)

            page += 1
            if not self._has_next_page(response):
                break

        return posts[:limit]

    def _search(self, params: dict[str, Any]) -> httpx.Response:
        try:
            response = self._rate_limited.request(
                "GET",
                SEARCH_URL,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._settings.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"GitHub search request failed: {exc}") from exc
        return response

    @staticmethod
    def _has_next_page(response: httpx.Response) -> bool:
        # GitHub signals pagination continuation via the standard Link header
        # (rel="next"), not a field in the response body.
        return 'rel="next"' in response.headers.get("link", "")

    @staticmethod
    def _map_post(item: dict[str, Any], matched_term: str) -> RawPost:
        title = item.get("title") or ""
        body = item.get("body")
        text = f"{title}\n\n{body}" if body else title
        user = item.get("user") or {}
        reactions = item.get("reactions") or {}

        return RawPost(
            id=f"gh_{item['id']}",
            platform=Platform.GITHUB,
            author=user.get("login") or "unknown",
            # GitHub issue bodies are markdown/plain text, unlike SO's HTML -- safe
            # to include directly.
            text=text,
            url=item.get("html_url", ""),
            created_at=datetime.fromisoformat(item["created_at"]),
            metrics=Metrics(
                likes=0,
                comments=item.get("comments") or 0,
                score=reactions.get("total_count") or 0,
                shares=0,
            ),
            matched_term=matched_term,
        )
