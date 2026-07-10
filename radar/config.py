from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from radar.models import ModelImplicated, PainCategory

DEFAULT_SEARCH_TERMS_PATH = Path("config/search_terms.yaml")
DEFAULT_KNOWN_INCIDENTS_PATH = Path("config/known_incidents.yaml")
DEFAULT_ESCALATION_CRITERIA_PATH = Path("config/escalation_criteria.yaml")
DEFAULT_MODEL_TIERS_PATH = Path("config/model_tiers.yaml")
DEFAULT_SCHEDULE_PATH = Path("config/schedule.yaml")
DEFAULT_CLASSIFY_SCHEDULE_PATH = Path("config/classify_schedule.yaml")

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

_ESCALATION_CRITERIA_HEADER = (
    "# Per-category escalation criteria: whether it requires human QA, an\n"
    "# optional velocity-threshold override, and a first-response playbook.\n"
    "# Dashboard-editable (Settings tab) -- see radar/config.py.\n"
)

# Seeds config/escalation_criteria.yaml the first time it's saved, and fills
# in any category missing from an already-saved file (e.g. a category added
# to PainCategory after the file was last written). requires_qa defaults
# match this project's original hardcoded HUMAN_QA_CATEGORIES list
# (abuse/credential_theft/safety) -- migrating to this file changes nothing
# about existing behavior until someone edits it from the dashboard.
DEFAULT_ESCALATION_CRITERIA: dict[str, dict[str, Any]] = {
    "api_abuse": {
        "requires_qa": False,
        "velocity_threshold": None,
        "response_template": "Confirm the abuse pattern (rate limits, scripted misuse) with "
        "eng before any public response. Not customer-facing until confirmed.",
        "action_items": [
            "Confirm with engineering",
            "Alert enterprise AE",
            "Throttle output and monitor prompt usage",
        ],
    },
    "product_bug": {
        "requires_qa": False,
        "velocity_threshold": None,
        "response_template": "Reproduce with eng, get a fix/workaround ETA, then respond with "
        "what's confirmed -- avoid speculating on cause publicly.",
        "action_items": ["File engineering ticket", "Alert marketing/PMM", "Log for minor triage"],
    },
    "ux_confusion": {
        "requires_qa": False,
        "velocity_threshold": None,
        "response_template": "Low urgency unless velocity is high. A short clarifying reply "
        "or docs link is usually enough.",
        "action_items": ["Send clarifying reply"],
    },
    "messaging_gap": {
        "requires_qa": False,
        "velocity_threshold": None,
        "response_template": "Loop in Comms/PMM -- usually a documentation or "
        "expectation-setting gap, not a bug.",
        "action_items": ["Notify Comms/PMM"],
    },
    "credential_theft": {
        "requires_qa": True,
        "velocity_threshold": None,
        "response_template": "Escalate to security immediately. Do not confirm specifics "
        "publicly. Coordinate a private response with the reporter if possible.",
        "action_items": [
            "File a ToS report",
            "Lock the user's account",
            "File a report to the platform to alert the affected user",
        ],
    },
    "abuse": {
        "requires_qa": True,
        "velocity_threshold": None,
        "response_template": "Escalate to Trust & Safety. Do not engage publicly until T&S "
        "has assessed scope and intent.",
        "action_items": ["Escalate to Trust & Safety"],
    },
    "safety": {
        "requires_qa": True,
        "velocity_threshold": None,
        "response_template": "Escalate immediately to Safety and executive channels. Treat "
        "as highest priority until triaged.",
        "action_items": ["Escalate to Safety & Exec"],
    },
    "other": {
        "requires_qa": False,
        "velocity_threshold": None,
        "response_template": "Triage manually -- doesn't fit an existing category cleanly.",
        "action_items": ["Log manual triage"],
    },
}

_SCHEDULE_HEADER = (
    "# Automatic collection: whether radar serve's background scheduler is on,\n"
    "# and how often it polls (seconds). Off by default -- same \"opt-in, not\n"
    "# because it costs anything\" convention as ENABLE_HACKERNEWS_SOURCE/\n"
    "# ENABLE_STACKOVERFLOW_SOURCE. Dashboard-editable (Settings tab) -- see\n"
    "# radar/config.py and radar/scheduler.py.\n"
)

# 7200s (2h) matches Settings.poll_interval_seconds's own default -- the two
# are independent (this drives real recurring execution; that one is only a
# first-run lookback fallback in radar/collect.py), but there's no reason to
# start them out of sync.
DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 7200,
}

_CLASSIFY_SCHEDULE_HEADER = (
    "# Automatic classification: whether radar serve's background scheduler\n"
    "# also runs classify passes (not just collection), and how often. Off by\n"
    "# default -- unlike collection this calls the paid Anthropic API, so this\n"
    "# is a separate opt-in from config/schedule.yaml, not folded into it.\n"
    "# Dashboard-editable (Settings tab) -- see radar/config.py and\n"
    "# radar/scheduler.py.\n"
)

# Independent from DEFAULT_SCHEDULE's interval -- collection and classification
# are separate scheduler ticks (radar/scheduler.py) so they can be tuned apart,
# e.g. classifying less often than collecting to control API spend.
DEFAULT_CLASSIFY_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 7200,
}

_MODEL_TIERS_HEADER = (
    "# Per-model protection tier: how much a leak/incident on this specific\n"
    "# model matters (a flagship-model incident isn't the same story as one on\n"
    "# a small/cheap model), plus an optional velocity-threshold override.\n"
    "# Dashboard-editable (Settings tab) -- see radar/config.py.\n"
)

# Seeds config/model_tiers.yaml the first time it's saved. "flagship" for the
# current-generation, headline models; "standard" for smaller/cheaper models
# and the generic/non-model-specific buckets -- nothing here changes existing
# scoring behavior until a tier's velocity_threshold is actually set.
DEFAULT_MODEL_TIERS: dict[str, dict[str, Any]] = {
    "claude_opus": {"protection_tier": "flagship", "velocity_threshold": None},
    "claude_sonnet": {"protection_tier": "flagship", "velocity_threshold": None},
    "claude_fable": {"protection_tier": "flagship", "velocity_threshold": None},
    "claude_haiku": {"protection_tier": "standard", "velocity_threshold": None},
    "claude_api_general": {"protection_tier": "standard", "velocity_threshold": None},
    "claude_code": {"protection_tier": "standard", "velocity_threshold": None},
    "other_llm": {"protection_tier": "standard", "velocity_threshold": None},
    "not_applicable": {"protection_tier": "standard", "velocity_threshold": None},
    "unknown": {"protection_tier": "standard", "velocity_threshold": None},
}


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
    # A quiet gap longer than this starts a new "episode" when counting how
    # many times a root-cause cluster has recurred (see radar/cluster.py).
    recurrence_gap_hours: float = 48.0

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


def load_escalation_criteria(
    path: Path = DEFAULT_ESCALATION_CRITERIA_PATH,
) -> dict[str, dict[str, Any]]:
    """Every PainCategory value always gets a full {requires_qa,
    velocity_threshold, response_template} entry -- saved values win, missing
    ones (a category added after the file was last saved, or the file simply
    not existing yet) fall back to DEFAULT_ESCALATION_CRITERIA.
    """
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        saved = (data or {}).get("categories", {})
    else:
        saved = {}
    return {
        category.value: {**DEFAULT_ESCALATION_CRITERIA[category.value], **saved.get(category.value, {})}
        for category in PainCategory
    }


def save_escalation_criteria(
    updates: dict[str, dict[str, Any]], path: Path = DEFAULT_ESCALATION_CRITERIA_PATH
) -> dict[str, dict[str, Any]]:
    """Merges `updates` (keyed by PainCategory value, each a partial
    {requires_qa, velocity_threshold, response_template}) into the
    currently-saved criteria and writes it back -- same merge-on-save shape as
    save_search_terms(), so a caller updating one category's fields doesn't
    need to resend every other category's row.
    """
    current = load_escalation_criteria(path)
    for category_value, fields in updates.items():
        current.setdefault(category_value, dict(DEFAULT_ESCALATION_CRITERIA.get(category_value, {})))
        current[category_value].update(fields)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_ESCALATION_CRITERIA_HEADER)
        yaml.safe_dump({"categories": current}, f, sort_keys=False, default_flow_style=False)
    return current


def category_requires_qa(criteria: dict[str, dict[str, Any]], category: str) -> bool:
    return bool(criteria.get(category, {}).get("requires_qa", False))


def load_model_tiers(path: Path = DEFAULT_MODEL_TIERS_PATH) -> dict[str, dict[str, Any]]:
    """Every ModelImplicated value always gets a full {protection_tier,
    velocity_threshold} entry -- same fallback shape as
    load_escalation_criteria(): saved values win, missing ones (a model added
    after the file was last saved, or the file not existing yet) fall back to
    DEFAULT_MODEL_TIERS.
    """
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        saved = (data or {}).get("models", {})
    else:
        saved = {}
    return {
        model.value: {**DEFAULT_MODEL_TIERS[model.value], **saved.get(model.value, {})}
        for model in ModelImplicated
    }


def save_model_tiers(
    updates: dict[str, dict[str, Any]], path: Path = DEFAULT_MODEL_TIERS_PATH
) -> dict[str, dict[str, Any]]:
    """Merges `updates` (keyed by ModelImplicated value) into the
    currently-saved tiers and writes it back -- same merge-on-save shape as
    save_escalation_criteria().
    """
    current = load_model_tiers(path)
    for model_value, fields in updates.items():
        current.setdefault(model_value, dict(DEFAULT_MODEL_TIERS.get(model_value, {})))
        current[model_value].update(fields)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_MODEL_TIERS_HEADER)
        yaml.safe_dump({"models": current}, f, sort_keys=False, default_flow_style=False)
    return current


def load_schedule_config(path: Path = DEFAULT_SCHEDULE_PATH) -> dict[str, Any]:
    """{enabled, interval_seconds} -- saved values win, missing ones (file
    doesn't exist yet, or a key was added after it was last saved) fall back
    to DEFAULT_SCHEDULE. Read fresh on every call (radar/scheduler.py's loop
    re-reads this every tick) rather than cached at process startup, so a
    dashboard edit takes effect without restarting `radar serve`.
    """
    if path.exists():
        with open(path, encoding="utf-8") as f:
            saved = yaml.safe_load(f) or {}
    else:
        saved = {}
    return {**DEFAULT_SCHEDULE, **saved}


def save_schedule_config(
    updates: dict[str, Any], path: Path = DEFAULT_SCHEDULE_PATH
) -> dict[str, Any]:
    """Merges `updates` into the currently-saved schedule and writes it back --
    same merge-on-save shape as save_model_tiers().
    """
    current = load_schedule_config(path)
    current.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SCHEDULE_HEADER)
        yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    return current


def load_classify_schedule_config(path: Path = DEFAULT_CLASSIFY_SCHEDULE_PATH) -> dict[str, Any]:
    """{enabled, interval_seconds} for the classify scheduler -- same
    read-fresh-every-call, fall-back-to-defaults shape as load_schedule_config().
    """
    if path.exists():
        with open(path, encoding="utf-8") as f:
            saved = yaml.safe_load(f) or {}
    else:
        saved = {}
    return {**DEFAULT_CLASSIFY_SCHEDULE, **saved}


def save_classify_schedule_config(
    updates: dict[str, Any], path: Path = DEFAULT_CLASSIFY_SCHEDULE_PATH
) -> dict[str, Any]:
    """Merges `updates` into the currently-saved classify schedule and writes
    it back -- same merge-on-save shape as save_schedule_config().
    """
    current = load_classify_schedule_config(path)
    current.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CLASSIFY_SCHEDULE_HEADER)
        yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    return current


def protection_tier_for(model_tiers: dict[str, dict[str, Any]], model: str) -> str:
    return model_tiers.get(model, {}).get("protection_tier", "standard")


def effective_velocity_threshold(
    settings: "Settings",
    criteria: dict[str, dict[str, Any]],
    platform: str,
    category: str,
    model_tiers: dict[str, dict[str, Any]] | None = None,
    model: str | None = None,
) -> float:
    """Precedence: category override (escalation_criteria.yaml) > model
    override (model_tiers.yaml) > platform override
    (velocity_threshold_overrides, env-only) > global default.

    Category stays most specific/intentional; model protection is the next
    most specific signal (a flagship-model incident should alert sooner than
    the same issue on a small model); the platform override is a
    data-normalization default (YouTube views vs. Reddit upvotes), the most
    generic of the three.
    """
    category_override = criteria.get(category, {}).get("velocity_threshold")
    if category_override is not None:
        return float(category_override)
    if model_tiers is not None and model is not None:
        model_override = model_tiers.get(model, {}).get("velocity_threshold")
        if model_override is not None:
            return float(model_override)
    return settings.velocity_threshold_for(platform)


def client_for_matched_term(
    matched_term: str | None, search_config: dict[str, Any]
) -> str | None:
    """Which watched client (if any) a post's matched_term came from -- e.g.
    "McDonald's jailbreak" -> "McDonald's". None for a generic (non-client-
    scoped) term. Lets the dashboard filter the footprint graph down to one
    client's posts specifically, cutting out unrelated noise.
    """
    if not matched_term:
        return None
    term_to_client = {
        f"{client} {pattern}": client
        for client in search_config.get("clients", [])
        for pattern in search_config.get("risk_patterns", [])
    }
    return term_to_client.get(matched_term)
