from __future__ import annotations

import random
import time
from typing import Callable

import httpx


class RateLimitedClient:
    """Wraps an httpx.Client with polite pacing, rate-limit-header awareness,
    and exponential backoff+jitter on 429/5xx. Shared by any Source that
    talks to a rate-limited HTTP API.
    """

    def __init__(
        self,
        client: httpx.Client,
        min_interval: float = 1.2,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep_fn = sleep_fn
        self._last_request_at: float | None = None

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            self._pace()
            self._last_request_at = time.monotonic()
            response = self._client.request(method, url, **kwargs)

            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > self._max_retries:
                    response.raise_for_status()
                self._sleep_fn(self._backoff_delay(attempt))
                continue

            self._respect_rate_limit_headers(response)
            return response

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._min_interval - elapsed
        if remaining > 0:
            self._sleep_fn(remaining)

    def _backoff_delay(self, attempt: int) -> float:
        delay = min(self._backoff_cap, self._backoff_base * (2 ** (attempt - 1)))
        return delay + random.uniform(0, delay * 0.1)

    def _respect_rate_limit_headers(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            remaining_f = float(remaining)
            reset_f = float(reset)
        except ValueError:
            return
        if remaining_f < 1:
            self._sleep_fn(max(0.0, reset_f))
