from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from radar.cluster import get_clusters
from radar.config import get_settings
from radar.db import get_alerts, get_connection, init_db, resolve_alert
from radar.leadtime import compute_lead_times, summarize_lead_times

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Claude Radar")


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
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
