from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from radar.config import Settings
from radar.hashing import hash_author
from radar.models import Classification, RawPost
from radar.virality import virality_score

SCHEMA_VERSION = "2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,
    hashed_author     TEXT,
    poll_run_id       TEXT NOT NULL,
    collected_at      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    subreddit         TEXT,
    matched_term      TEXT,
    url               TEXT NOT NULL,
    likes             INTEGER NOT NULL DEFAULT 0,
    comments          INTEGER NOT NULL DEFAULT 0,
    score             INTEGER NOT NULL DEFAULT 0,
    shares            INTEGER NOT NULL DEFAULT 0,
    virality_score    REAL NOT NULL DEFAULT 0.0,
    raw_text          TEXT,
    search_pass       TEXT NOT NULL,
    inserted_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_post_id ON snapshots(post_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_platform_created_at ON snapshots(platform, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_collected_at ON snapshots(collected_at);

CREATE TABLE IF NOT EXISTS classifications (
    post_id           TEXT PRIMARY KEY,
    is_pain_point     INTEGER NOT NULL,
    category          TEXT NOT NULL,
    model_implicated  TEXT NOT NULL,
    severity          TEXT NOT NULL,
    issue_summary     TEXT NOT NULL,
    classifier_model  TEXT NOT NULL,
    classified_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_connection(database_path: Path) -> sqlite3.Connection:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def write_snapshots(
    conn: sqlite3.Connection,
    posts: list[RawPost],
    poll_run_id: str,
    search_pass: str,
    settings: Settings,
) -> int:
    if not posts:
        return 0

    collected_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for post in posts:
        hashed_author = (
            hash_author(post.author, settings.author_hash_pepper)
            if settings.hash_authors
            else post.author
        )
        rows.append(
            (
                post.id,
                post.platform.value,
                hashed_author,
                poll_run_id,
                collected_at,
                post.created_at.isoformat(),
                post.subreddit,
                post.matched_term,
                post.url,
                post.metrics.likes,
                post.metrics.comments,
                post.metrics.score,
                post.metrics.shares,
                virality_score(post.metrics),
                post.text,
                search_pass,
            )
        )

    conn.executemany(
        """
        INSERT INTO snapshots (
            post_id, platform, hashed_author, poll_run_id, collected_at, created_at,
            subreddit, matched_term, url, likes, comments, score, shares,
            virality_score, raw_text, search_pass
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def get_unclassified_posts(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str, str, str]]:
    """One row per post_id (its most recent snapshot) that has no row in
    `classifications` yet -- (post_id, platform, raw_text, url).
    """
    rows = conn.execute(
        """
        SELECT s.post_id, s.platform, s.raw_text, s.url
        FROM snapshots s
        WHERE s.id = (
            SELECT s2.id FROM snapshots s2
            WHERE s2.post_id = s.post_id
            ORDER BY s2.collected_at DESC, s2.id DESC
            LIMIT 1
        )
        AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.post_id = s.post_id)
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def write_classifications(
    conn: sqlite3.Connection, classifications: list[Classification], classifier_model: str
) -> int:
    if not classifications:
        return 0

    classified_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            c.post_id,
            int(c.is_pain_point),
            c.category.value,
            c.model_implicated.value,
            c.severity.value,
            c.issue_summary,
            classifier_model,
            classified_at,
        )
        for c in classifications
    ]

    conn.executemany(
        """
        INSERT OR REPLACE INTO classifications (
            post_id, is_pain_point, category, model_implicated, severity,
            issue_summary, classifier_model, classified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)
