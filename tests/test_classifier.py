from __future__ import annotations

import httpx
import pytest
import respx

from radar.classify import (
    API_URL,
    ClassifierAPIError,
    ClassifierCredentialsMissingError,
    ClaudeClassifier,
)


def make_classifier(settings_factory, sleep_fn=None, **overrides):
    settings = settings_factory(**overrides)
    return ClaudeClassifier(settings, sleep_fn=sleep_fn or (lambda s: None))


@respx.mock
def test_classify_maps_fixture_to_pain_point(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("classify_pain_point.json"))
    )

    classifier = make_classifier(settings_factory)
    result = classifier.classify(
        post_id="t3_top1",
        platform="reddit",
        text="Getting 529s constantly during peak hours.",
        url="https://www.reddit.com/r/ClaudeAI/comments/top1/",
    )

    assert result.post_id == "t3_top1"
    assert result.is_pain_point is True
    assert result.category.value == "product_bug"
    assert result.model_implicated.value == "claude_api_general"
    assert result.severity.value == "high"
    assert "529" in result.issue_summary


@respx.mock
def test_classify_maps_fixture_to_non_pain_point(settings_factory, load_anthropic_fixture):
    respx.post(API_URL).mock(
        return_value=httpx.Response(
            200, json=load_anthropic_fixture("classify_not_pain_point.json")
        )
    )

    classifier = make_classifier(settings_factory)
    result = classifier.classify(
        post_id="t3_top2", platform="reddit", text="Claude is great at coding!", url="https://x"
    )

    assert result.is_pain_point is False
    assert result.severity.value == "low"


@respx.mock
def test_sends_expected_headers_and_forced_tool_choice(settings_factory, load_anthropic_fixture):
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("classify_pain_point.json"))
    )

    classifier = make_classifier(settings_factory, anthropic_api_key="my-secret-key")
    classifier.classify(post_id="t3_top1", platform="reddit", text="hello", url="https://x")

    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "my-secret-key"
    assert sent.headers["anthropic-version"]

    import json

    body = json.loads(sent.content)
    assert body["tool_choice"] == {"type": "tool", "name": "record_classification"}
    assert body["tools"][0]["name"] == "record_classification"


@respx.mock
def test_missing_credentials_raises_before_any_http_call(settings_factory):
    classifier = make_classifier(settings_factory, anthropic_api_key="")

    with pytest.raises(ClassifierCredentialsMissingError):
        classifier.classify(post_id="t3_top1", platform="reddit", text="hello", url="https://x")


@respx.mock
def test_response_without_tool_use_raises_classifier_api_error(
    settings_factory, load_anthropic_fixture
):
    respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("classify_no_tool_use.json"))
    )

    classifier = make_classifier(settings_factory)

    with pytest.raises(ClassifierAPIError):
        classifier.classify(post_id="t3_top1", platform="reddit", text="hello", url="https://x")


@respx.mock
def test_backoff_then_retry_succeeds(settings_factory, load_anthropic_fixture):
    route = respx.post(API_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_anthropic_fixture("classify_pain_point.json")),
        ]
    )

    sleeps: list[float] = []
    classifier = make_classifier(settings_factory, sleep_fn=sleeps.append)
    result = classifier.classify(
        post_id="t3_top1", platform="reddit", text="hello", url="https://x"
    )

    assert route.call_count == 2
    assert result.is_pain_point is True
    assert any(delay > 0 for delay in sleeps)


@respx.mock
def test_retries_exhausted_raises(settings_factory):
    route = respx.post(API_URL).mock(return_value=httpx.Response(503))

    classifier = make_classifier(settings_factory)

    with pytest.raises(ClassifierAPIError):
        classifier.classify(post_id="t3_top1", platform="reddit", text="hello", url="https://x")

    assert route.call_count > 1
