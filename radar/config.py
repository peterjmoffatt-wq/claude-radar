from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SEARCH_TERMS_PATH = Path("config/search_terms.yaml")
DEFAULT_KNOWN_INCIDENTS_PATH = Path("config/known_incidents.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Reddit (OAuth2 client_credentials grant -- see radar/sources/reddit.py)
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "claude-radar/0.1"
    # Unused escape hatch to the password grant; present for a possible future need.
    reddit_username: str | None = None
    reddit_password: str | None = None

    # Privacy
    hash_authors: bool = True
    author_hash_pepper: str = ""

    # Collector tuning
    poll_interval_seconds: int = 7200
    top_n: int = 50
    velocity_threshold: float = 10.0
    raw_text_retention_days: int = 14
    # Scoped to issue TYPES only -- never specific people or communities.
    human_qa_categories: list[str] = ["abuse", "credential_theft", "safety"]

    # Storage
    database_path: Path = Path("data/radar.db")

    # Classifier
    anthropic_api_key: str = ""
    classifier_model: str = "claude-haiku-4-5-20251001"
    classify_batch_limit: int = 100

    # YouTube (Data API v3, simple API-key auth)
    youtube_api_key: str = ""

    # X/Twitter (feature-flagged, off by default -- see radar/sources/x.py)
    enable_x_source: bool = False
    x_bearer_token: str = ""

    # Hacker News (Algolia HN Search API -- free, keyless). Off by default like
    # every other source, even though it needs no credentials: adding a source to
    # this codebase shouldn't silently start hitting a new external host for
    # existing configs/tests that never opted in.
    enable_hackernews_source: bool = False

    # Stack Overflow (Stack Exchange API -- free, key optional for a higher quota)
    enable_stackoverflow_source: bool = False
    stackoverflow_api_key: str = ""

    # GitHub Issues (needs a personal access token -- unauthenticated search is
    # rate-limited too low, 10 req/min, to be usable across several search terms)
    github_token: str = ""

    def has_reddit_credentials(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def has_youtube_credentials(self) -> bool:
        return bool(self.youtube_api_key)

    def has_x_credentials(self) -> bool:
        return self.enable_x_source and bool(self.x_bearer_token)

    def has_github_credentials(self) -> bool:
        return bool(self.github_token)

    @model_validator(mode="after")
    def _pepper_required_if_hashing(self) -> "Settings":
        if self.hash_authors and not self.author_hash_pepper:
            raise ValueError("AUTHOR_HASH_PEPPER is required when HASH_AUTHORS=true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_search_terms(path: Path = DEFAULT_SEARCH_TERMS_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_known_incidents(path: Path = DEFAULT_KNOWN_INCIDENTS_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("incidents", [])
