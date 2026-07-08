from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    REDDIT = "reddit"
    HACKERNEWS = "hackernews"  # Phase 2+
    YOUTUBE = "youtube"  # Phase 2+
    X = "x"  # Phase 2+, feature-flagged


class Metrics(BaseModel):
    likes: int = 0
    comments: int = 0
    score: int = 0  # net upvotes (Reddit) / generic engagement score
    shares: int = 0  # reposts/shares, where the platform exposes it


class RawPost(BaseModel):
    id: str  # platform-native id, e.g. Reddit "t3_abc123"
    platform: Platform
    author: str  # raw handle, pre-hash -- never persisted as-is if HASH_AUTHORS
    text: str
    url: str
    created_at: datetime  # tz-aware UTC
    metrics: Metrics
    subreddit: str | None = None
    matched_term: str | None = None  # which search_terms.yaml term surfaced this post


class PainCategory(str, Enum):
    API_ABUSE = "api_abuse"
    PRODUCT_BUG = "product_bug"
    UX_CONFUSION = "ux_confusion"
    MESSAGING_GAP = "messaging_gap"
    CREDENTIAL_THEFT = "credential_theft"
    ABUSE = "abuse"
    SAFETY = "safety"
    OTHER = "other"


class ModelImplicated(str, Enum):
    CLAUDE_OPUS = "claude_opus"
    CLAUDE_SONNET = "claude_sonnet"
    CLAUDE_HAIKU = "claude_haiku"
    CLAUDE_API_GENERAL = "claude_api_general"
    CLAUDE_CODE = "claude_code"
    OTHER_LLM = "other_llm"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


class Classification(BaseModel):
    """Data contract the Phase 2 classifier targets. No DB table yet."""

    post_id: str
    is_pain_point: bool
    category: PainCategory
    model_implicated: ModelImplicated
    severity: Severity
    issue_summary: str = Field(..., max_length=120)
