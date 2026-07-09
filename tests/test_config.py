from __future__ import annotations

import pytest
from pydantic import ValidationError

from radar.config import (
    DEFAULT_ESCALATION_CRITERIA,
    DEFAULT_RISK_PATTERNS,
    MAX_EFFECTIVE_TERMS,
    Settings,
    category_requires_qa,
    effective_terms,
    effective_velocity_threshold,
    load_escalation_criteria,
    load_known_incidents,
    load_search_terms,
    save_escalation_criteria,
    save_search_terms,
)


def test_settings_defaults_applied_when_unset():
    settings = Settings(author_hash_pepper="pepper", _env_file=None)
    assert settings.poll_interval_seconds == 7200
    assert settings.top_n == 50
    assert settings.recurrence_gap_hours == 48.0
    assert settings.classifier_model == "claude-haiku-4-5-20251001"


def test_settings_loads_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDDIT_CLIENT_ID=abc\n"
        "REDDIT_CLIENT_SECRET=xyz\n"
        "AUTHOR_HASH_PEPPER=pepper123\n"
        "TOP_N=10\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.reddit_client_id == "abc"
    assert settings.reddit_client_secret == "xyz"
    assert settings.top_n == 10
    assert settings.has_reddit_credentials() is True


def test_has_reddit_credentials_false_when_blank():
    settings = Settings(author_hash_pepper="pepper", _env_file=None)
    assert settings.has_reddit_credentials() is False


def test_pepper_required_when_hash_authors_enabled():
    with pytest.raises(ValidationError):
        Settings(hash_authors=True, author_hash_pepper="", _env_file=None)


def test_hash_authors_false_does_not_require_pepper():
    settings = Settings(hash_authors=False, author_hash_pepper="", _env_file=None)
    assert settings.hash_authors is False


def test_load_search_terms_parses_yaml(tmp_path):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text('subreddits:\n  - TestSub\nterms:\n  - "test term"\n')
    data = load_search_terms(path=yaml_path)
    assert data["subreddits"] == ["TestSub"]
    assert data["terms"] == ["test term"]


def test_load_known_incidents_parses_yaml(tmp_path):
    yaml_path = tmp_path / "known_incidents.yaml"
    yaml_path.write_text(
        'incidents:\n  - name: "Test incident"\n    starts_at: "2024-01-01T00:00:00Z"\n'
        '    ends_at: "2024-01-01T06:00:00Z"\n'
    )
    incidents = load_known_incidents(path=yaml_path)
    assert incidents == [
        {"name": "Test incident", "starts_at": "2024-01-01T00:00:00Z", "ends_at": "2024-01-01T06:00:00Z"}
    ]


def test_load_known_incidents_empty_file_returns_empty_list(tmp_path):
    yaml_path = tmp_path / "known_incidents.yaml"
    yaml_path.write_text("")
    assert load_known_incidents(path=yaml_path) == []


def test_load_search_terms_defaults_clients_and_risk_patterns(tmp_path):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text('subreddits:\n  - TestSub\nterms:\n  - "test term"\n')
    data = load_search_terms(path=yaml_path)
    assert data["clients"] == []
    assert data["risk_patterns"] == DEFAULT_RISK_PATTERNS


def test_save_search_terms_round_trips_and_preserves_untouched_fields(tmp_path):
    yaml_path = tmp_path / "search_terms.yaml"
    yaml_path.write_text(
        "subreddits:\n  - ClaudeAI\nterms:\n  - old term\nclients: []\nrisk_patterns: []\n"
    )

    save_search_terms(
        {"terms": ["new term"], "clients": ["McDonald's"], "risk_patterns": ["jailbreak"]},
        path=yaml_path,
    )

    reloaded = load_search_terms(path=yaml_path)
    assert reloaded["subreddits"] == ["ClaudeAI"]  # untouched by the update
    assert reloaded["terms"] == ["new term"]
    assert reloaded["clients"] == ["McDonald's"]
    assert reloaded["risk_patterns"] == ["jailbreak"]


def test_effective_terms_crosses_clients_with_risk_patterns():
    config = {
        "terms": ["Claude API", "claude down"],
        "clients": ["McDonald's", "Acme Corp"],
        "risk_patterns": ["jailbreak", "credential leak"],
    }
    assert effective_terms(config) == [
        "Claude API",
        "claude down",
        "McDonald's jailbreak",
        "McDonald's credential leak",
        "Acme Corp jailbreak",
        "Acme Corp credential leak",
    ]


def test_effective_terms_with_no_clients_returns_generic_terms_only():
    config = {"terms": ["Claude API"], "clients": [], "risk_patterns": ["jailbreak"]}
    assert effective_terms(config) == ["Claude API"]


def test_load_escalation_criteria_returns_defaults_when_file_missing(tmp_path):
    criteria = load_escalation_criteria(path=tmp_path / "does_not_exist.yaml")
    assert criteria == DEFAULT_ESCALATION_CRITERIA
    # Every PainCategory value is present, matching the original hardcoded
    # HUMAN_QA_CATEGORIES = ["abuse", "credential_theft", "safety"] default.
    assert criteria["abuse"]["requires_qa"] is True
    assert criteria["credential_theft"]["requires_qa"] is True
    assert criteria["safety"]["requires_qa"] is True
    assert criteria["product_bug"]["requires_qa"] is False


def test_load_escalation_criteria_fills_in_category_missing_from_saved_file(tmp_path):
    yaml_path = tmp_path / "escalation_criteria.yaml"
    yaml_path.write_text(
        "categories:\n  safety:\n    requires_qa: false\n    velocity_threshold: 5.0\n"
        "    response_template: custom template\n"
    )

    criteria = load_escalation_criteria(path=yaml_path)

    assert criteria["safety"] == {
        "requires_qa": False,
        "velocity_threshold": 5.0,
        "response_template": "custom template",
    }
    # A category absent from the saved file still gets its full default row.
    assert criteria["product_bug"] == DEFAULT_ESCALATION_CRITERIA["product_bug"]


def test_save_escalation_criteria_merges_and_preserves_other_categories(tmp_path):
    yaml_path = tmp_path / "escalation_criteria.yaml"

    save_escalation_criteria(
        {"credential_theft": {"velocity_threshold": 2.0}}, path=yaml_path
    )
    reloaded = load_escalation_criteria(path=yaml_path)

    assert reloaded["credential_theft"]["velocity_threshold"] == 2.0
    assert reloaded["credential_theft"]["requires_qa"] is True  # untouched by the update
    assert reloaded["product_bug"] == DEFAULT_ESCALATION_CRITERIA["product_bug"]


def test_category_requires_qa():
    criteria = {"safety": {"requires_qa": True}, "product_bug": {"requires_qa": False}}
    assert category_requires_qa(criteria, "safety") is True
    assert category_requires_qa(criteria, "product_bug") is False
    assert category_requires_qa(criteria, "unknown_category") is False


def test_effective_velocity_threshold_category_override_wins():
    settings = Settings(
        author_hash_pepper="pepper",
        velocity_threshold=10.0,
        velocity_threshold_overrides={"youtube": 500.0},
        _env_file=None,
    )
    criteria = {"credential_theft": {"velocity_threshold": 1.0}}
    assert effective_velocity_threshold(settings, criteria, "youtube", "credential_theft") == 1.0


def test_effective_velocity_threshold_falls_back_to_platform_override():
    settings = Settings(
        author_hash_pepper="pepper",
        velocity_threshold=10.0,
        velocity_threshold_overrides={"youtube": 500.0},
        _env_file=None,
    )
    criteria = {"credential_theft": {"velocity_threshold": None}}
    assert effective_velocity_threshold(settings, criteria, "youtube", "credential_theft") == 500.0


def test_effective_velocity_threshold_falls_back_to_global_default():
    settings = Settings(author_hash_pepper="pepper", velocity_threshold=10.0, _env_file=None)
    assert effective_velocity_threshold(settings, {}, "reddit", "product_bug") == 10.0


def test_effective_terms_truncated_to_max_with_generic_terms_kept_first():
    # A full watchlist (10 generic + 10 clients x 10 patterns = 110) can
    # exhaust YouTube's daily search quota in one run -- effective_terms()
    # truncates to MAX_EFFECTIVE_TERMS, keeping every generic term before any
    # cross-product term is dropped.
    generic = [f"term{i}" for i in range(10)]
    clients = [f"client{i}" for i in range(10)]
    patterns = [f"pattern{i}" for i in range(10)]
    config = {"terms": generic, "clients": clients, "risk_patterns": patterns}

    result = effective_terms(config)

    assert len(result) == MAX_EFFECTIVE_TERMS
    assert result[: len(generic)] == generic
    assert all(term not in generic for term in result[len(generic) :])
