from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from radar.config import Settings
from radar.hashing import hash_author
from radar.models import Classification, RawPost
from radar.virality import virality_score

SCHEMA_VERSION = "3"

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

CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           TEXT NOT NULL,
    triggered_at      TEXT NOT NULL,
    virality_score    REAL NOT NULL,
    velocity          REAL NOT NULL,
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL,
    qa_status         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_post_id ON alerts(post_id);
CREATE INDEX IF NOT EXISTS idx_alerts_triggered_at ON alerts(triggered_at);

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


def get_pain_point_posts(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(post_id, category, severity) for every post classified as a pain point."""
    return conn.execute(
        "SELECT post_id, category, severity FROM classifications WHERE is_pain_point = 1"
    ).fetchall()


def get_snapshot_history(conn: sqlite3.Connection, post_id: str) -> list[tuple[datetime, float]]:
    """(collected_at, virality_score) for every snapshot of a post, oldest first."""
    rows = conn.execute(
        "SELECT collected_at, virality_score FROM snapshots WHERE post_id = ? ORDER BY collected_at ASC, id ASC",
        (post_id,),
    ).fetchall()
    return [(datetime.fromisoformat(collected_at), score) for collected_at, score in rows]


def get_latest_alert_velocity(conn: sqlite3.Connection, post_id: str) -> float | None:
    row = conn.execute(
        "SELECT velocity FROM alerts WHERE post_id = ? ORDER BY triggered_at DESC, id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    return row[0] if row is not None else None


def list_pending_alerts(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, float, str, str]]:
    """Pending alerts -- (post_id, category, severity, velocity, issue_summary, url) --
    joined with their classification and the post's most recent snapshot url.

    Only considers each post's *latest* alert row: a post can re-alert (accelerate
    again) before its first alert is reviewed, and only the latest one is what
    `resolve_alert` acts on -- an older still-'pending' row must not linger here.
    """
    return conn.execute(
        """
        SELECT a.post_id, a.category, a.severity, a.velocity, c.issue_summary,
               (SELECT s.url FROM snapshots s WHERE s.post_id = a.post_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS url
        FROM alerts a
        JOIN classifications c ON c.post_id = a.post_id
        WHERE a.id = (
            SELECT a2.id FROM alerts a2 WHERE a2.post_id = a.post_id
            ORDER BY a2.triggered_at DESC, a2.id DESC LIMIT 1
        )
        AND a.qa_status = 'pending'
        ORDER BY a.triggered_at DESC
        """
    ).fetchall()


def get_alerts(
    conn: sqlite3.Connection,
    status: str | None = None,
    category: str | None = None,
    severity: str | None = None,
) -> list[tuple]:
    """Each post's latest alert -- for the dashboard's filterable alert list --
    joined with its classification and most recent snapshot's url/platform.
    """
    query = """
        SELECT a.post_id, a.category, a.severity, a.velocity, a.virality_score,
               a.qa_status, a.triggered_at, c.issue_summary, c.model_implicated,
               (SELECT s.url FROM snapshots s WHERE s.post_id = a.post_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS url,
               (SELECT s.platform FROM snapshots s WHERE s.post_id = a.post_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS platform
        FROM alerts a
        JOIN classifications c ON c.post_id = a.post_id
        WHERE a.id = (
            SELECT a2.id FROM alerts a2 WHERE a2.post_id = a.post_id
            ORDER BY a2.triggered_at DESC, a2.id DESC LIMIT 1
        )
    """
    params: list[str] = []
    if status:
        query += " AND a.qa_status = ?"
        params.append(status)
    if category:
        query += " AND a.category = ?"
        params.append(category)
    if severity:
        query += " AND a.severity = ?"
        params.append(severity)
    query += " ORDER BY a.triggered_at DESC"

    return conn.execute(query, params).fetchall()


def get_unscored_pain_points(conn: sqlite3.Connection) -> list[tuple]:
    """Pain-point classifications with no alert ever fired for them -- either they
    haven't had a second snapshot yet to compute velocity from, or their velocity
    never crossed VELOCITY_THRESHOLD. Gives the dashboard visibility into real
    signal that exists but isn't (yet, or ever) surfaced as an alert.
    """
    return conn.execute(
        """
        SELECT c.post_id, c.category, c.severity, c.issue_summary, c.model_implicated,
               (SELECT s.url FROM snapshots s WHERE s.post_id = c.post_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS url,
               (SELECT s.platform FROM snapshots s WHERE s.post_id = c.post_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS platform
        FROM classifications c
        WHERE c.is_pain_point = 1
        AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.post_id = c.post_id)
        ORDER BY c.classified_at DESC
        """
    ).fetchall()


def resolve_alert(conn: sqlite3.Connection, post_id: str, decision: str) -> bool:
    """Resolve the most recent pending alert for a post to 'approved'/'rejected'.

    Returns whether a row was actually updated (False if no pending alert exists).
    """
    row = conn.execute(
        "SELECT id FROM alerts WHERE post_id = ? AND qa_status = 'pending' "
        "ORDER BY triggered_at DESC, id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE alerts SET qa_status = ? WHERE id = ?", (decision, row[0]))
    conn.commit()
    return True


def get_first_seen_by_pass(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(post_id, search_pass, first_collected_at) -- earliest snapshot per (post_id, search_pass)."""
    return conn.execute(
        """
        SELECT post_id, search_pass, MIN(collected_at) AS first_collected_at
        FROM snapshots
        GROUP BY post_id, search_pass
        """
    ).fetchall()


def get_alert_timestamps(conn: sqlite3.Connection) -> list[datetime]:
    rows = conn.execute("SELECT triggered_at FROM alerts").fetchall()
    return [datetime.fromisoformat(row[0]) for row in rows]


def get_alerts_for_clustering(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str]]:
    """(category, model_implicated, severity, issue_summary, triggered_at) for every alert."""
    return conn.execute(
        """
        SELECT a.category, c.model_implicated, a.severity, c.issue_summary, a.triggered_at
        FROM alerts a
        JOIN classifications c ON c.post_id = a.post_id
        """
    ).fetchall()


def write_alert(
    conn: sqlite3.Connection,
    post_id: str,
    virality_score_value: float,
    velocity: float,
    category: str,
    severity: str,
    qa_status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO alerts (post_id, triggered_at, virality_score, velocity, category, severity, qa_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            datetime.now(timezone.utc).isoformat(),
            virality_score_value,
            velocity,
            category,
            severity,
            qa_status,
        ),
    )
    conn.commit()
