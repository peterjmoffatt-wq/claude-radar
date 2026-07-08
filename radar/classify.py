from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from radar.config import Settings, get_settings
from radar.db import get_connection, get_unclassified_posts, init_db, write_classifications
from radar.http_utils import RateLimitedClient
from radar.models import Classification, ModelImplicated, PainCategory, Severity

logger = logging.getLogger("radar.classify")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

MAX_TEXT_CHARS = 4000  # keep prompt size (and cost) bounded; posts are short anyway
MAX_TOKENS = 512

TOOL_NAME = "record_classification"

CLASSIFICATION_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Record a structured classification of a public post about Claude "
        "(Anthropic's AI) or the Claude API."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_pain_point": {
                "type": "boolean",
                "description": "True if the post describes a genuine pain point (a bug, "
                "outage, confusing behavior, abuse, or safety/credential issue) rather than "
                "e.g. praise, unrelated chatter, or a resolved non-issue.",
            },
            "category": {
                "type": "string",
                "enum": [c.value for c in PainCategory],
                "description": "The type of issue, scoped to issue TYPES only -- never "
                "specific people or communities.",
            },
            "model_implicated": {
                "type": "string",
                "enum": [m.value for m in ModelImplicated],
                "description": "Which Claude model or product the post is about, if any.",
            },
            "severity": {
                "type": "string",
                "enum": [s.value for s in Severity],
            },
            "issue_summary": {
                "type": "string",
                "maxLength": 120,
                "description": "A short, neutral, one-sentence summary of the issue.",
            },
        },
        "required": [
            "is_pain_point",
            "category",
            "model_implicated",
            "severity",
            "issue_summary",
        ],
    },
}


class ClassifierCredentialsMissingError(RuntimeError):
    """Raised when the classifier is invoked without an ANTHROPIC_API_KEY configured."""


class ClassifierAPIError(RuntimeError):
    """Raised when a Claude API request fails, or returns an unusable response."""


def _build_prompt(platform: str, text: str, url: str) -> str:
    truncated = text[:MAX_TEXT_CHARS]
    return (
        "You are triaging public posts for a product-signal monitoring tool that watches "
        "for real pain points affecting Claude (Anthropic's AI) and the Claude API. "
        "Decide whether this post describes a genuine pain point, and if so, classify it. "
        "Use `record_classification` to record your answer.\n\n"
        f"Platform: {platform}\n"
        f"URL: {url}\n"
        f"Post text:\n{truncated}"
    )


class ClaudeClassifier:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client()
        self._rate_limited = RateLimitedClient(self._client, sleep_fn=sleep_fn)

    def classify(self, post_id: str, platform: str, text: str, url: str) -> Classification:
        if not self._settings.anthropic_api_key:
            raise ClassifierCredentialsMissingError("ANTHROPIC_API_KEY is not configured")

        payload = {
            "model": self._settings.classifier_model,
            "max_tokens": MAX_TOKENS,
            "tools": [CLASSIFICATION_TOOL],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": _build_prompt(platform, text, url)}],
        }

        try:
            response = self._rate_limited.request(
                "POST",
                API_URL,
                json=payload,
                headers={
                    "x-api-key": self._settings.anthropic_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ClassifierAPIError(f"Claude classify request failed: {exc}") from exc

        data = response.json()
        tool_input = self._extract_tool_input(data, post_id)

        try:
            return Classification(post_id=post_id, **tool_input)
        except ValidationError as exc:
            raise ClassifierAPIError(
                f"Claude returned an unusable classification for post_id={post_id}: {exc}"
            ) from exc

    @staticmethod
    def _extract_tool_input(data: dict[str, Any], post_id: str) -> dict[str, Any]:
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                return block["input"]
        raise ClassifierAPIError(
            f"Claude response for post_id={post_id} had no {TOOL_NAME!r} tool_use block"
        )


@dataclass
class ClassificationResult:
    posts_classified: int
    skipped: bool


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def run_classification(
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    limit: int | None = None,
) -> ClassificationResult:
    settings = settings or get_settings()

    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY missing; skipping classification.")
        return ClassificationResult(posts_classified=0, skipped=True)

    classifier = ClaudeClassifier(settings, sleep_fn=sleep_fn)
    conn = get_connection(settings.database_path)
    init_db(conn)

    try:
        pending = get_unclassified_posts(conn, limit=limit or settings.classify_batch_limit)

        classifications: list[Classification] = []
        for post_id, platform, raw_text, url in pending:
            try:
                classification = classifier.classify(post_id, platform, raw_text or "", url)
            except ClassifierAPIError:
                logger.exception("post_id=%s classification failed; skipping", post_id)
                continue
            classifications.append(classification)
            logger.info(
                "post_id=%s is_pain_point=%s category=%s severity=%s",
                post_id,
                classification.is_pain_point,
                classification.category.value,
                classification.severity.value,
            )

        written = write_classifications(conn, classifications, settings.classifier_model)
    finally:
        conn.close()

    return ClassificationResult(posts_classified=written, skipped=False)


def main() -> None:
    configure_logging()
    result = run_classification()
    if result.skipped:
        sys.exit(0)
    print(f"Classified {result.posts_classified} posts.")


if __name__ == "__main__":
    main()
