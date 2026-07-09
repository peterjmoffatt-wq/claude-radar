from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from radar.cluster import get_clusters
from radar.collect import run_collection, source_availability
from radar.config import (
    MAX_WATCHLIST_ITEMS,
    effective_terms,
    get_settings,
    load_search_terms,
    save_search_terms,
)
from radar.db import (
    count_advertisements,
    get_alerts,
    get_connection,
    get_unscored_pain_points,
    init_db,
    resolve_alert,
)
from radar.leadtime import compute_lead_times, summarize_lead_times

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Claude Radar")

# Tracks which resolved db paths have already had init_db() run in this
# process -- init_db() re-executes the full schema script + migration check +
# a schema_meta write, which is wasted work on every single request. Keyed by
# path (not a single bool) so tests using different tmp_path databases within
# the same pytest process each still get initialized on first use.
_initialized_paths: set[Path] = set()


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    conn = get_connection(settings.database_path)
    path = Path(settings.database_path).resolve()
    if path not in _initialized_paths:
        init_db(conn)
        _initialized_paths.add(path)
    return conn


def _alert_dict(row: tuple) -> dict:
    keys = (
        "post_id",
        "category",
        "severity",
        "velocity",
        "virality_score",
        "qa_status",
        "triggered_at",
        "issue_summary",
        "model_implicated",
        "url",
        "platform",
        "matched_term",
        "author",
        "likes",
        "comments",
        "score",
        "shares",
        "created_at",
    )
    return dict(zip(keys, row))


@app.get("/api/alerts")
def api_alerts(
    status: str | None = None, category: str | None = None, severity: str | None = None
) -> list[dict]:
    conn = _connect()
    try:
        rows = get_alerts(conn, status=status, category=category, severity=severity)
    finally:
        conn.close()
    return [_alert_dict(row) for row in rows]


@app.get("/api/clusters")
def api_clusters() -> list[dict]:
    conn = _connect()
    try:
        clusters = get_clusters(conn)
    finally:
        conn.close()
    return [c.__dict__ for c in clusters]


@app.get("/api/watching")
def api_watching() -> list[dict]:
    """Pain points that are classified but haven't (yet, or ever) crossed the
    velocity threshold into a real alert -- see get_unscored_pain_points.
    """
    conn = _connect()
    try:
        rows = get_unscored_pain_points(conn)
    finally:
        conn.close()
    keys = (
        "post_id",
        "category",
        "severity",
        "issue_summary",
        "model_implicated",
        "url",
        "platform",
        "matched_term",
        "author",
        "likes",
        "comments",
        "score",
        "shares",
        "created_at",
    )
    return [dict(zip(keys, row)) for row in rows]


@app.get("/api/lead-time")
def api_lead_time() -> dict:
    conn = _connect()
    try:
        entries = compute_lead_times(conn)
    finally:
        conn.close()
    summary = summarize_lead_times(entries)
    # Sorted positive lead times, for the dashboard's distribution sparkline.
    summary["lead_times_seconds"] = sorted(
        e.lead_time_seconds
        for e in entries
        if e.lead_time_seconds is not None and e.lead_time_seconds > 0
    )
    return summary


@app.get("/api/stats")
def api_stats() -> dict:
    """Signal-quality stats for the Home stat tiles -- currently just the
    promotional/competitor-poaching spam count, kept separate from clusters
    and lead-time since it's not itself a chart.
    """
    conn = _connect()
    try:
        ads_filtered = count_advertisements(conn)
    finally:
        conn.close()
    return {"ads_filtered": ads_filtered}


@app.get("/api/sources")
def api_sources() -> dict[str, bool]:
    """Which real platforms are currently configured -- drives the dashboard's
    source-picker checkbox defaults.
    """
    return source_availability(get_settings())


class CollectRequest(BaseModel):
    sources: list[str] | None = None


@app.post("/api/collect")
def api_collect(body: CollectRequest) -> dict:
    """Triggers a real collection pass for the requested sources (or every
    configured source, if `sources` is omitted) and reports what happened.
    Runs synchronously in-request -- fine at this data volume/demo scale; a
    production version would background this instead.
    """
    settings = get_settings()
    requested = set(body.sources) if body.sources else None
    result = run_collection(settings, sources=requested)

    available = source_availability(settings)
    skipped_unconfigured = (
        sorted(name for name in requested if not available.get(name, False))
        if requested is not None
        else []
    )

    return {
        "snapshots_written": result.snapshots_written,
        "sources_run": result.sources_run,
        "sources_skipped_unconfigured": skipped_unconfigured,
        "sources_failed": result.sources_failed,
    }


def _search_terms_payload(config: dict) -> dict:
    return {
        "terms": config.get("terms", []),
        "clients": config.get("clients", []),
        "risk_patterns": config.get("risk_patterns", []),
        "effective_terms": effective_terms(config),
        "max_items": MAX_WATCHLIST_ITEMS,
    }


@app.get("/api/search-terms")
def api_get_search_terms() -> dict:
    return _search_terms_payload(load_search_terms())


class SearchTermsUpdate(BaseModel):
    terms: list[str]
    clients: list[str]
    risk_patterns: list[str]


@app.put("/api/search-terms")
def api_update_search_terms(body: SearchTermsUpdate) -> dict:
    """Persists the watchlist to config/search_terms.yaml -- also changes
    what `radar collect` uses from the CLI, not just dashboard clicks.
    """
    cleaned: dict[str, list[str]] = {}
    for field_name, raw_items in (
        ("terms", body.terms),
        ("clients", body.clients),
        ("risk_patterns", body.risk_patterns),
    ):
        items = [item.strip() for item in raw_items if item.strip()]
        if len(items) > MAX_WATCHLIST_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} cannot exceed {MAX_WATCHLIST_ITEMS} entries",
            )
        cleaned[field_name] = items

    save_search_terms(cleaned)
    return _search_terms_payload(load_search_terms())


class ReviewDecision(BaseModel):
    decision: Literal["approved", "rejected"]


@app.post("/api/alerts/{post_id}/review")
def api_review(post_id: str, body: ReviewDecision) -> dict:
    conn = _connect()
    try:
        changed = resolve_alert(conn, post_id, body.decision)
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail=f"No pending alert found for {post_id}")
    return {"post_id": post_id, "qa_status": body.decision}


# Mounted last so the /api/* routes above take precedence over this catch-all.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
