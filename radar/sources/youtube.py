from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from radar.config import Settings
from radar.http_utils import RateLimitedClient
from radar.models import Metrics, Platform, RawPost
from radar.sources.base import SearchWindow

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

MAX_PAGE_LIMIT = 50  # YouTube's per-request cap for search.list

_WINDOW_DELTAS: dict[SearchWindow, timedelta | None] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
    "all": None,
}


class CredentialsMissingError(RuntimeError):
    """Raised when YouTube API calls are attempted without a configured API key."""


class YouTubeAPIError(RuntimeError):
    """Raised when a YouTube API request fails after exhausting retries."""


class YouTubeSource:
    name = "youtube"

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
        published_after = datetime.now(timezone.utc) - delta if delta else None
        return self._paginate(query, order="viewCount", limit=limit, published_after=published_after)

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        posts = self._paginate(query, order="date", limit=limit, published_after=since)
        return [p for p in posts if p.created_at >= since][:limit]

    # -- pagination / requests --------------------------------------------

    def _paginate(
        self,
        query: str,
        order: str,
        limit: int,
        published_after: datetime | None,
    ) -> list[RawPost]:
        if not self._settings.youtube_api_key:
            raise CredentialsMissingError("YOUTUBE_API_KEY is not configured")

        video_ids: list[str] = []
        snippets: dict[str, dict[str, Any]] = {}
        page_token: str | None = None

        for _ in range(self._max_pages):
            if len(video_ids) >= limit:
                break

            params: dict[str, Any] = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": order,
                "maxResults": min(MAX_PAGE_LIMIT, limit - len(video_ids)),
                "key": self._settings.youtube_api_key,
            }
            if published_after is not None:
                params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
            if page_token:
                params["pageToken"] = page_token

            data = self._get(SEARCH_URL, params)
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                video_id = item["id"]["videoId"]
                video_ids.append(video_id)
                snippets[video_id] = item["snippet"]

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        if not video_ids:
            return []

        selected_ids = video_ids[:limit]
        stats = self._fetch_statistics(selected_ids)
        return [
            self._map_post(video_id, snippets[video_id], stats.get(video_id, {}), matched_term=query)
            for video_id in selected_ids
        ]

    def _fetch_statistics(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for i in range(0, len(video_ids), MAX_PAGE_LIMIT):
            chunk = video_ids[i : i + MAX_PAGE_LIMIT]
            data = self._get(
                VIDEOS_URL,
                {"part": "statistics", "id": ",".join(chunk), "key": self._settings.youtube_api_key},
            )
            for item in data.get("items", []):
                stats[item["id"]] = item.get("statistics", {})
        return stats

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._rate_limited.request("GET", url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise YouTubeAPIError(f"YouTube API request failed: {exc}") from exc
        return response.json()

    @staticmethod
    def _map_post(
        video_id: str, snippet: dict[str, Any], statistics: dict[str, Any], matched_term: str
    ) -> RawPost:
        title = snippet.get("title", "")
        description = snippet.get("description", "")
        text = f"{title}\n\n{description}" if description else title

        return RawPost(
            id=f"yt_{video_id}",
            platform=Platform.YOUTUBE,
            author=snippet.get("channelTitle", "unknown"),
            text=text,
            url=f"https://www.youtube.com/watch?v={video_id}",
            created_at=datetime.fromisoformat(snippet["publishedAt"]),
            metrics=Metrics(
                likes=int(statistics.get("likeCount", 0)),
                comments=int(statistics.get("commentCount", 0)),
                score=int(statistics.get("viewCount", 0)),
                shares=0,
            ),
            matched_term=matched_term,
        )
