from __future__ import annotations

import json
from pathlib import Path

import pytest

from radar.config import Settings
from radar.db import get_connection, init_db

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_reddit_fixture():
    def _load(name: str) -> dict:
        path = FIXTURES_DIR / "reddit" / name
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return _load


@pytest.fixture
def settings_factory(tmp_path):
    def _make(**overrides) -> Settings:
        defaults = dict(
            reddit_client_id="test-client-id",
            reddit_client_secret="test-client-secret",
            reddit_user_agent="claude-radar-test/0.1",
            hash_authors=True,
            author_hash_pepper="test-pepper",
            database_path=tmp_path / "radar.db",
        )
        defaults.update(overrides)
        return Settings(_env_file=None, **defaults)

    return _make


@pytest.fixture
def tmp_db_conn(tmp_path):
    conn = get_connection(tmp_path / "radar.db")
    init_db(conn)
    yield conn
    conn.close()
