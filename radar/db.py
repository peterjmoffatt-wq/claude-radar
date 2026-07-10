from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from radar.config import Settings
from radar.hashing import hash_author
from radar.models import Classification, RawPost
from radar.virality import virality_score

SCHEMA_VERSION = "8"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,
    hashed_author     TEXT, -- raw handle when HASH_AUTHORS=false, else a one-way hash
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
    is_advertisement  INTEGER NOT NULL DEFAULT 0,
    category          TEXT NOT NULL,
    model_implicated  TEXT NOT NULL,
    severity          TEXT NOT NULL,
    issue_summary     TEXT NOT NULL,
    classifier_model  TEXT NOT NULL,
    classified_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id                       TEXT NOT NULL,
    triggered_at                  TEXT NOT NULL,
    virality_score                REAL NOT NULL,
    velocity                      REAL NOT NULL,
    category                      TEXT NOT NULL,
    severity                      TEXT NOT NULL,
    qa_status                     TEXT NOT NULL,
    -- Independent of qa_status: qa_status gates whether the classification
    -- itself is legitimate, incident_status tracks whether someone is
    -- actually working the incident. See radar/db.py's transition_incident().
    incident_status               TEXT NOT NULL DEFAULT 'open',
    exec_brief                    TEXT,
    exec_brief_generated_at       TEXT,
    incident_report               TEXT,
    incident_report_generated_at  TEXT,
    -- Current recommended Course of Action -- same fast-read/overwrite-on-
    -- regenerate shape as exec_brief. See radar/brief.py's generate_coa().
    coa                           TEXT,
    coa_generated_at              TEXT,
    -- Which PM has this alert on their personal Board (freeform name, no
    -- auth system exists) -- distinct from incident_status/qa_status: an
    -- alert can sit unclaimed in the team-wide Alerts tab indefinitely.
    -- See claim_alert()/release_alert() below.
    claimed_by                    TEXT,
    claimed_at                    TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_post_id ON alerts(post_id);
CREATE INDEX IF NOT EXISTS idx_alerts_triggered_at ON alerts(triggered_at);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_attempts (
    post_id         TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_failed_at  TEXT
);

-- Append-only timeline for one alert's incident lifecycle -- also exactly the
-- data a post-incident report needs (radar/brief.py's generate_incident_report).
CREATE TABLE IF NOT EXISTS incident_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     INTEGER NOT NULL,
    from_status  TEXT NOT NULL,
    to_status    TEXT NOT NULL,
    note         TEXT,
    -- The system-recommended Course of Action generated for *this specific*
    -- transition (Kanban board), distinct from `note` (a human-entered
    -- reason) -- kept so the timeline shows what was recommended at each
    -- stage, not just the latest one (see alerts.coa for that).
    coa          TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incident_events_alert_id ON incident_events(alert_id);

-- Clusters aren't otherwise persisted (get_clusters() computes them fresh per
-- request) -- this just caches a generated exec brief per cluster_key so it
-- isn't silently re-billed/regenerated on every page load.
CREATE TABLE IF NOT EXISTS cluster_briefs (
    cluster_key   TEXT PRIMARY KEY,
    brief         TEXT NOT NULL,
    generated_at  TEXT NOT NULL
);

-- A human confirming a COA's recommendation was actually carried out --
-- distinct from incident_events (which logs status transitions). Gates
-- transition_incident()'s move to 'resolved': see radar/api.py's
-- api_alert_transition().
CREATE TABLE IF NOT EXISTS alert_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       TEXT NOT NULL,
    action_label  TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_actions_post_id ON alert_actions(post_id);
"""


def get_connection(database_path: Path) -> sqlite3.Connection:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers (dashboard polling) proceed while a writer (a live
    # /api/collect run) holds the connection open; busy_timeout makes SQLite
    # retry for a bit instead of immediately raising "database is locked"
    # on the rare remaining contention.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Handles columns added to a table that already exists on disk --
    `CREATE TABLE IF NOT EXISTS` in _SCHEMA above is a no-op against an
    existing table, so a brand new column needs an explicit ALTER TABLE the
    first time this runs against an older database file. Each addition
    follows this same "check PRAGMA table_info, ALTER if missing" shape.
    """
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(classifications)").fetchall()
    }
    if "is_advertisement" not in existing_columns:
        conn.execute(
            "ALTER TABLE classifications ADD COLUMN is_advertisement INTEGER NOT NULL DEFAULT 0"
        )

    alerts_columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "incident_status" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN incident_status TEXT NOT NULL DEFAULT 'open'")
    if "exec_brief" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN exec_brief TEXT")
    if "exec_brief_generated_at" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN exec_brief_generated_at TEXT")
    if "incident_report" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN incident_report TEXT")
    if "incident_report_generated_at" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN incident_report_generated_at TEXT")
    if "coa" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN coa TEXT")
    if "coa_generated_at" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN coa_generated_at TEXT")
    if "claimed_by" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN claimed_by TEXT")
    if "claimed_at" not in alerts_columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN claimed_at TEXT")

    incident_events_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(incident_events)").fetchall()
    }
    if "coa" not in incident_events_columns:
        conn.execute("ALTER TABLE incident_events ADD COLUMN coa TEXT")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        _migrate(conn)
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
        ORDER BY s.collected_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def record_classification_failure(conn: sqlite3.Connection, post_id: str) -> int:
    """Upserts a failure count for a post whose classification attempt raised
    -- returns the new attempt count so the caller can decide whether to give
    up (write a sentinel row) or let it retry on the next run.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO classification_attempts (post_id, attempts, last_failed_at)
        VALUES (?, 1, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            attempts = attempts + 1,
            last_failed_at = excluded.last_failed_at
        """,
        (post_id, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM classification_attempts WHERE post_id = ?", (post_id,)
    ).fetchone()
    return row[0]


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
            int(c.is_advertisement),
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
            post_id, is_pain_point, is_advertisement, category, model_implicated,
            severity, issue_summary, classifier_model, classified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def count_advertisements(conn: sqlite3.Connection) -> int:
    """How many classified posts were caught as promotional/competitor-poaching
    spam rather than genuine pain points -- surfaced as a signal-quality stat
    on the dashboard, not just silently excluded from Watching/Alerts.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM classifications WHERE is_advertisement = 1"
    ).fetchone()
    return row[0] if row else 0


def get_pain_point_posts(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str]]:
    """(post_id, category, severity, platform, model_implicated) for every post
    classified as a pain point -- platform comes from the post's latest
    snapshot (per-platform velocity threshold); model_implicated lets
    run_scoring() apply a per-model velocity threshold override too.
    """
    return conn.execute(
        """
        SELECT c.post_id, c.category, c.severity, ls.platform, c.model_implicated
        FROM classifications c
        JOIN (
            SELECT s.post_id, s.platform
            FROM snapshots s
            WHERE s.id = (
                SELECT s2.id FROM snapshots s2
                WHERE s2.post_id = s.post_id
                ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
            )
        ) ls ON ls.post_id = c.post_id
        WHERE c.is_pain_point = 1
        """
    ).fetchall()


def get_snapshot_history(conn: sqlite3.Connection, post_id: str) -> list[tuple[datetime, float]]:
    """(collected_at, virality_score) for a post, one row per poll_run_id (the
    latest snapshot within each run), oldest first.

    A post appearing in both the "top" and "recent" search passes of the same
    collection run gets two snapshot rows written milliseconds apart -- if
    compute_velocity() saw both of those as the "last two" data points, the
    elapsed time would be ~0, producing a None/masked velocity or (if a metric
    ticked up between the two writes) an absurd spike. Collapsing to one row
    per run means the last two rows returned here are always from two
    genuinely time-separated collection runs.
    """
    rows = conn.execute(
        """
        SELECT collected_at, virality_score FROM snapshots s
        WHERE post_id = ?
        AND s.id = (
            SELECT s2.id FROM snapshots s2
            WHERE s2.post_id = s.post_id AND s2.poll_run_id = s.poll_run_id
            ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
        )
        ORDER BY collected_at ASC, id ASC
        """,
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
    post_id: str | None = None,
) -> list[tuple]:
    """Each post's latest alert -- for the dashboard's filterable alert list --
    joined with its classification and most recent snapshot's url/platform/
    author/engagement.
    """
    query = """
        SELECT a.post_id, a.category, a.severity, a.velocity, a.virality_score,
               a.qa_status, a.triggered_at, c.issue_summary, c.model_implicated,
               ls.url, ls.platform, ls.matched_term,
               ls.hashed_author, ls.likes, ls.comments, ls.score, ls.shares, ls.created_at,
               a.incident_status, a.exec_brief, a.exec_brief_generated_at,
               a.incident_report, a.incident_report_generated_at,
               a.coa, a.coa_generated_at,
               (SELECT COUNT(*) FROM alert_actions aa WHERE aa.post_id = a.post_id) AS action_count,
               (SELECT MAX(created_at) FROM incident_events
                WHERE alert_id = a.id AND to_status = 'resolved') AS resolved_at,
               a.claimed_by, a.claimed_at
        FROM alerts a
        JOIN classifications c ON c.post_id = a.post_id
        JOIN (
            SELECT s.post_id, s.url, s.platform, s.matched_term,
                   s.hashed_author, s.likes, s.comments, s.score, s.shares, s.created_at
            FROM snapshots s
            WHERE s.id = (
                SELECT s2.id FROM snapshots s2
                WHERE s2.post_id = s.post_id
                ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
            )
        ) ls ON ls.post_id = a.post_id
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
    if post_id:
        query += " AND a.post_id = ?"
        params.append(post_id)
    query += " ORDER BY a.triggered_at DESC"

    return conn.execute(query, params).fetchall()


def get_unscored_pain_points(conn: sqlite3.Connection, post_id: str | None = None) -> list[tuple]:
    """Pain-point classifications with no alert ever fired for them -- either they
    haven't had a second snapshot yet to compute velocity from, or their velocity
    never crossed VELOCITY_THRESHOLD. Gives the dashboard visibility into real
    signal that exists but isn't (yet, or ever) surfaced as an alert. `post_id`
    narrows to a single row -- used by the manual "send to Alerts" promotion path,
    which needs one targeted lookup rather than the full watching list.
    """
    query = """
        SELECT c.post_id, c.category, c.severity, c.issue_summary, c.model_implicated,
               ls.url, ls.platform, ls.matched_term,
               ls.hashed_author, ls.likes, ls.comments, ls.score, ls.shares, ls.created_at
        FROM classifications c
        JOIN (
            SELECT s.post_id, s.url, s.platform, s.matched_term,
                   s.hashed_author, s.likes, s.comments, s.score, s.shares, s.created_at
            FROM snapshots s
            WHERE s.id = (
                SELECT s2.id FROM snapshots s2
                WHERE s2.post_id = s.post_id
                ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
            )
        ) ls ON ls.post_id = c.post_id
        WHERE c.is_pain_point = 1
        AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.post_id = c.post_id)
    """
    params: list[str] = []
    if post_id:
        query += " AND c.post_id = ?"
        params.append(post_id)
    query += " ORDER BY c.classified_at DESC"

    return conn.execute(query, params).fetchall()


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


def claim_alert(conn: sqlite3.Connection, post_id: str, claimed_by: str) -> bool:
    """Assigns the post's latest alert to a PM's personal Board -- claimed_by is
    freeform (no auth system exists yet). Returns whether a row was actually
    updated (False if no alert exists for post_id).
    """
    alert_id = _latest_alert_id(conn, post_id)
    if alert_id is None:
        return False
    conn.execute(
        "UPDATE alerts SET claimed_by = ?, claimed_at = ? WHERE id = ?",
        (claimed_by, datetime.now(timezone.utc).isoformat(), alert_id),
    )
    conn.commit()
    return True


def release_alert(conn: sqlite3.Connection, post_id: str) -> bool:
    """Clears a claim, handing the post back to the team-wide Alerts pool.

    Returns whether a row was actually updated (False if no alert exists for post_id).
    """
    alert_id = _latest_alert_id(conn, post_id)
    if alert_id is None:
        return False
    conn.execute(
        "UPDATE alerts SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (alert_id,)
    )
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


def get_last_collected_at(conn: sqlite3.Connection) -> datetime | None:
    """When the most recent collection run actually happened -- None on a
    genuinely empty database. Lets run_collection() base its "recent" pass's
    `since` on the real last run instead of the configured poll interval, so
    a missed/late run (laptop asleep, cron skipped a tick) doesn't leave a
    silent gap of posts that were created but never captured.
    """
    row = conn.execute("SELECT MAX(collected_at) FROM snapshots").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def get_alerts_for_clustering(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str, str]]:
    """(category, model_implicated, severity, issue_summary, triggered_at, platform)
    for every alert -- platform comes from each post's most recent snapshot, needed
    so a cluster's exec brief can name which platforms it's actually spreading
    across (see radar/cluster.py's ClusterSummary.platforms).
    """
    return conn.execute(
        """
        SELECT a.category, c.model_implicated, a.severity, c.issue_summary, a.triggered_at,
               ls.platform
        FROM alerts a
        JOIN classifications c ON c.post_id = a.post_id
        JOIN (
            SELECT s.post_id, s.platform
            FROM snapshots s
            WHERE s.id = (
                SELECT s2.id FROM snapshots s2
                WHERE s2.post_id = s.post_id
                ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
            )
        ) ls ON ls.post_id = a.post_id
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


def _latest_alert_id(conn: sqlite3.Connection, post_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM alerts WHERE post_id = ? ORDER BY triggered_at DESC, id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    return row[0] if row is not None else None


def transition_incident(
    conn: sqlite3.Connection,
    post_id: str,
    new_status: str,
    note: str | None = None,
    coa: str | None = None,
) -> bool:
    """Moves the post's latest alert to a new incident_status, logging the
    transition to incident_events -- independent of qa_status (see the
    `incident_status` column comment in _SCHEMA). `coa` is the system-
    recommended Course of Action generated for this specific transition (the
    Kanban board), stored on the event row alongside any human `note` --
    callers also persist it as the alert's *current* COA via save_coa().

    Returns whether a row was actually updated (False if no alert exists).
    """
    row = conn.execute(
        "SELECT id, incident_status FROM alerts WHERE post_id = ? "
        "ORDER BY triggered_at DESC, id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    if row is None:
        return False
    alert_id, from_status = row
    conn.execute("UPDATE alerts SET incident_status = ? WHERE id = ?", (new_status, alert_id))
    conn.execute(
        "INSERT INTO incident_events (alert_id, from_status, to_status, note, coa, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (alert_id, from_status, new_status, note, coa, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return True


def get_incident_timeline(
    conn: sqlite3.Connection, post_id: str
) -> list[tuple[str, str, str | None, str | None, str]]:
    """(from_status, to_status, note, coa, created_at) for the post's latest
    alert, oldest first -- shown in the incident detail panel and folded into
    the post-incident report. `coa` is the Course of Action generated for
    that specific transition (Kanban board), if any.
    """
    return conn.execute(
        """
        SELECT e.from_status, e.to_status, e.note, e.coa, e.created_at
        FROM incident_events e
        WHERE e.alert_id = (
            SELECT a.id FROM alerts a WHERE a.post_id = ?
            ORDER BY a.triggered_at DESC, a.id DESC LIMIT 1
        )
        ORDER BY e.created_at ASC, e.id ASC
        """,
        (post_id,),
    ).fetchall()


def log_alert_action(conn: sqlite3.Connection, post_id: str, action_label: str, note: str | None = None) -> None:
    """Records that a human actually carried out a recommended action for
    this alert -- distinct from transition_incident()'s status-change log.
    Doesn't require an existing alert row (mirrors incident_events, which is
    likewise never foreign-keyed) so callers don't need a separate existence
    check before logging.
    """
    conn.execute(
        "INSERT INTO alert_actions (post_id, action_label, note, created_at) VALUES (?, ?, ?, ?)",
        (post_id, action_label, note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_alert_actions(conn: sqlite3.Connection, post_id: str) -> list[tuple[str, str | None, str]]:
    """(action_label, note, created_at) for this post, oldest first -- shown
    in the incident detail panel's "Actions taken" history, and used by
    api_alert_transition() to gate a move to 'resolved' on at least one
    logged action existing.
    """
    return conn.execute(
        "SELECT action_label, note, created_at FROM alert_actions "
        "WHERE post_id = ? ORDER BY created_at ASC, id ASC",
        (post_id,),
    ).fetchall()


def save_exec_brief(conn: sqlite3.Connection, post_id: str, brief: str) -> bool:
    """Persists a generated exec brief on the post's latest alert row --
    returns False if no alert exists for post_id (nothing to attach it to).
    """
    alert_id = _latest_alert_id(conn, post_id)
    if alert_id is None:
        return False
    conn.execute(
        "UPDATE alerts SET exec_brief = ?, exec_brief_generated_at = ? WHERE id = ?",
        (brief, datetime.now(timezone.utc).isoformat(), alert_id),
    )
    conn.commit()
    return True


def save_incident_report(conn: sqlite3.Connection, post_id: str, report_markdown: str) -> bool:
    alert_id = _latest_alert_id(conn, post_id)
    if alert_id is None:
        return False
    conn.execute(
        "UPDATE alerts SET incident_report = ?, incident_report_generated_at = ? WHERE id = ?",
        (report_markdown, datetime.now(timezone.utc).isoformat(), alert_id),
    )
    conn.commit()
    return True


def save_coa(conn: sqlite3.Connection, post_id: str, coa: str) -> bool:
    """Persists the current recommended Course of Action on the post's latest
    alert row -- regenerating (moving to a new Kanban column) overwrites it,
    same shape as save_exec_brief().
    """
    alert_id = _latest_alert_id(conn, post_id)
    if alert_id is None:
        return False
    conn.execute(
        "UPDATE alerts SET coa = ?, coa_generated_at = ? WHERE id = ?",
        (coa, datetime.now(timezone.utc).isoformat(), alert_id),
    )
    conn.commit()
    return True


def get_cluster_brief(conn: sqlite3.Connection, cluster_key: str) -> str | None:
    row = conn.execute(
        "SELECT brief FROM cluster_briefs WHERE cluster_key = ?", (cluster_key,)
    ).fetchone()
    return row[0] if row else None


def save_cluster_brief(conn: sqlite3.Connection, cluster_key: str, brief: str) -> None:
    conn.execute(
        """
        INSERT INTO cluster_briefs (cluster_key, brief, generated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(cluster_key) DO UPDATE SET
            brief = excluded.brief,
            generated_at = excluded.generated_at
        """,
        (cluster_key, brief, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
