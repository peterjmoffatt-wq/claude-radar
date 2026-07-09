from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SEARCH_TERMS_PATH = Path("config/search_terms.yaml")
DEFAULT_KNOWN_INCIDENTS_PATH = Path("config/known_incidents.yaml")

# Applies to each of the terms/clients/risk_patterns lists independently --
# enforced in the API layer (radar/api.py), not here, so this stays a plain
# shared constant rather than pulling validation into a config-loading module.
MAX_WATCHLIST_ITEMS = 10

# A full watchlist (10 generic terms + 10 clients x 10 risk patterns) can reach
# 110 effective terms -- at 2 passes x 100 units/request, that alone exhausts
# YouTube's 10,000/day search quota in one run. 40 keeps the worst case at
# 40 x 2 x 100 = 8,000 units, leaving headroom for the statistics follow-up
# calls (see radar/sources/youtube.py's _fetch_statistics).
MAX_EFFECTIVE_TERMS = 40

_SEARCH_TERMS_HEADER = (
    "# The tuned watchlist. Easy to add/edit/re-run -- see README for `radar tune` "
    "(Phase 2+).\n"
)

# A starting set of attack-pattern phrases to cross with each watched client
# name (see effective_terms()) -- covers the shape of the McDonald's-chatbot
# incident (prompt injection -> credential/token theft via induced code
# execution), not just generic "is Claude broken" chatter.
DEFAULT_RISK_PATTERNS = [
    "jailbreak",
    "prompt injection",
    "credential leak",
    "api key leak",
    "token theft",
    "code execution exploit",
    "system prompt leak",
    "data exfiltration",
]


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
    # YouTube view-counts and Reddit upvotes are fundamentally different
    # engagement scales -- one global threshold conflates them. This is the
    # configuration seam for per-platform tuning (e.g.
    # VELOCITY_THRESHOLD_OVERRIDES={"youtube": 500}), not a set of invented
    # multipliers -- empty by default, so behavior is unchanged until there's
    # enough real cross-platform volume to calibrate specific values against.
    velocity_threshold_overrides: dict[str, float] = {}
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

    # Mastodon (one configured instance's /api/v2/search -- status search needs a
    # bearer token even though account/hashtag search doesn't; confirmed live
    # against mastodon.social before wiring this up)
    mastodon_instance_url: str = ""
    mastodon_access_token: str = ""

    def has_reddit_credentials(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def has_youtube_credentials(self) -> bool:
        return bool(self.youtube_api_key)

    def has_x_credentials(self) -> bool:
        return self.enable_x_source and bool(self.x_bearer_token)

    def has_github_credentials(self) -> bool:
        return bool(self.github_token)

    def has_mastodon_credentials(self) -> bool:
        return bool(self.mastodon_instance_url and self.mastodon_access_token)

    def velocity_threshold_for(self, platform: str) -> float:
        return self.velocity_threshold_overrides.get(platform, self.velocity_threshold)

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
    data = data or {}
    data.setdefault("subreddits", [])
    data.setdefault("terms", [])
    data.setdefault("clients", [])
    data.setdefault("risk_patterns", list(DEFAULT_RISK_PATTERNS))
    return data


def save_search_terms(
    updates: dict[str, Any], path: Path = DEFAULT_SEARCH_TERMS_PATH
) -> dict[str, Any]:
    """Merges `updates` into the currently-saved config (so a caller only
    passing `terms`/`clients`/`risk_patterns` doesn't clobber `subreddits`)
    and writes it back. `yaml.safe_dump` drops comments, so the one header
    comment that matters is re-added by hand rather than lost on every save.
    """
    current = load_search_terms(path)
    current.update(updates)
    payload = {
        "subreddits": current.get("subreddits", []),
        "terms": current.get("terms", []),
        "clients": current.get("clients", []),
        "risk_patterns": current.get("risk_patterns", []),
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SEARCH_TERMS_HEADER)
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)
    return payload


def _client_scoped_term_list(search_config: dict[str, Any]) -> list[str]:
    """Deterministic (client, risk_pattern) cross-product, e.g. client
    "McDonald's" + pattern "jailbreak" -> "McDonald's jailbreak". Shared,
    order-preserving base for client_scoped_terms() and effective_terms().
    """
    return [
        f"{client} {pattern}"
        for client in search_config.get("clients", [])
        for pattern in search_config.get("risk_patterns", [])
    ]


def client_scoped_terms(search_config: dict[str, Any]) -> set[str]:
    """The client x risk-pattern cross-product half of effective_terms(), as a
    set for O(1) membership checks. Exposed separately so callers
    (radar/collect.py's Reddit branch) can tell a targeted-attack term apart
    from a generic one: these are meant to catch a client's incident being
    reported *anywhere*, not just in the configured subreddits.
    """
    return set(_client_scoped_term_list(search_config))


def effective_terms(search_config: dict[str, Any]) -> list[str]:
    """The full set of search strings a collection run actually queries: the
    generic `terms` list, plus one combined string per (client, risk_pattern)
    pair (see client_scoped_terms()). This is the real client-scoped
    targeted-attack detection mechanism, not just a UI preview --
    radar/collect.py's run_collection() iterates this, not the raw `terms`
    list. Truncated to MAX_EFFECTIVE_TERMS -- generic terms are always kept,
    cross-product terms fill in until the cap (see MAX_EFFECTIVE_TERMS for why).
    """
    terms = list(search_config.get("terms", [])) + _client_scoped_term_list(search_config)
    return terms[:MAX_EFFECTIVE_TERMS]


def load_known_incidents(path: Path = DEFAULT_KNOWN_INCIDENTS_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("incidents", [])
