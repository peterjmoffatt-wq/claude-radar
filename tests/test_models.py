from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from radar.models import (
    Classification,
    Metrics,
    ModelImplicated,
    PainCategory,
    Platform,
    RawPost,
    Severity,
)


def test_rawpost_valid_construction():
    post = RawPost(
        id="t3_abc123",
        platform=Platform.REDDIT,
        author="someone",
        text="hello world",
        url="https://reddit.com/r/x/abc123",
        created_at=datetime.now(timezone.utc),
        metrics=Metrics(likes=1, comments=2, score=3, shares=0),
    )
    assert post.platform == Platform.REDDIT
    assert post.metrics.score == 3


def test_rawpost_rejects_invalid_platform():
    with pytest.raises(ValidationError):
        RawPost(
            id="t3_abc123",
            platform="not-a-platform",
            author="someone",
            text="hello",
            url="https://reddit.com/x",
            created_at=datetime.now(timezone.utc),
            metrics=Metrics(),
        )


def test_metrics_defaults_to_zero():
    m = Metrics()
    assert (m.likes, m.comments, m.score, m.shares) == (0, 0, 0, 0)


def test_classification_valid_construction():
    c = Classification(
        post_id="t3_abc123",
        is_pain_point=True,
        category=PainCategory.API_ABUSE,
        model_implicated=ModelImplicated.CLAUDE_API_GENERAL,
        severity=Severity.HIGH,
        issue_summary="Users hitting rate limits during peak hours",
    )
    assert c.category == PainCategory.API_ABUSE
    assert c.severity == Severity.HIGH


def test_classification_rejects_invalid_category():
    with pytest.raises(ValidationError):
        Classification(
            post_id="t3_abc123",
            is_pain_point=True,
            category="not-a-real-category",
            model_implicated=ModelImplicated.UNKNOWN,
            severity=Severity.LOW,
            issue_summary="short",
        )


def test_classification_issue_summary_too_long_rejected():
    with pytest.raises(ValidationError):
        Classification(
            post_id="t3_abc123",
            is_pain_point=True,
            category=PainCategory.OTHER,
            model_implicated=ModelImplicated.UNKNOWN,
            severity=Severity.LOW,
            issue_summary="x" * 121,
        )
