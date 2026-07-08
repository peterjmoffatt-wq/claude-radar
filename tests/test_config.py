from __future__ import annotations

import pytest
from pydantic import ValidationError

from radar.config import Settings, load_search_terms


def test_settings_defaults_applied_when_unset():
    settings = Settings(author_hash_pepper="pepper", _env_file=None)
    assert settings.poll_interval_seconds == 7200
    assert settings.top_n == 50
    assert settings.human_qa_categories == ["abuse", "credential_theft", "safety"]
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
