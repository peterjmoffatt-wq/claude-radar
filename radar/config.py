from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SEARCH_TERMS_PATH = Path("config/search_terms.yaml")


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

    # Classifier (Phase 2 -- not called yet, wired up now so config won't need rework)
    anthropic_api_key: str = ""
    classifier_model: str = "claude-haiku-4-5-20251001"

    def has_reddit_credentials(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

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
