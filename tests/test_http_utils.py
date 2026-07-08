from __future__ import annotations

import time

import httpx
import respx

from radar.http_utils import RateLimitedClient

URL = "https://example.com/resource"


def make_client(sleep_fn=None) -> RateLimitedClient:
    return RateLimitedClient(httpx.Client(), sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_relative_reset_header_sleeps_for_given_seconds():
    # Reddit-style: x-ratelimit-reset is seconds remaining until the window ends.
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"}
        )
    )

    sleeps: list[float] = []
    client = make_client(sleep_fn=sleeps.append)
    client.request("GET", URL)

    assert sleeps == [30.0]


@respx.mock
def test_absolute_epoch_reset_header_sleeps_for_remaining_time_not_the_raw_value():
    # GitHub-style: x-ratelimit-reset is an absolute Unix epoch timestamp. Treating
    # it as relative would previously sleep for ~1.7 billion seconds instead of ~45.
    reset_at = time.time() + 45
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset_at)}
        )
    )

    sleeps: list[float] = []
    client = make_client(sleep_fn=sleeps.append)
    client.request("GET", URL)

    assert len(sleeps) == 1
    # Allow a little slack for real time elapsed between reset_at being computed
    # and the client evaluating time.time() again.
    assert 40 <= sleeps[0] <= 45


@respx.mock
def test_missing_rate_limit_headers_does_not_sleep():
    respx.get(URL).mock(return_value=httpx.Response(200))

    sleeps: list[float] = []
    client = make_client(sleep_fn=sleeps.append)
    client.request("GET", URL)

    assert sleeps == []


@respx.mock
def test_remaining_above_one_does_not_sleep_even_with_reset_header():
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, headers={"x-ratelimit-remaining": "5", "x-ratelimit-reset": "30"}
        )
    )

    sleeps: list[float] = []
    client = make_client(sleep_fn=sleeps.append)
    client.request("GET", URL)

    assert sleeps == []


@respx.mock
def test_backoff_then_retry_on_429_still_works(monkeypatch):
    # Guard against a regression breaking the existing 429-retry path while fixing
    # the rate-limit-header handling.
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200)]
    )

    sleeps: list[float] = []
    client = make_client(sleep_fn=sleeps.append)
    response = client.request("GET", URL)

    assert route.call_count == 2
    assert response.status_code == 200
    assert any(delay > 0 for delay in sleeps)
