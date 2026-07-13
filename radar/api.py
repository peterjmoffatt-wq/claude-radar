from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from radar.brief import (
    BriefFacts,
    generate_coa,
    generate_exec_brief,
    generate_incident_report,
    generate_technical_explanation,
)
from radar.cluster import ClusterSummary, get_clusters
from radar.collect import run_collection, source_availability
from radar.classify import run_classification
from radar.config import (
    MAX_WATCHLIST_ITEMS,
    category_requires_qa,
    client_for_matched_term,
    effective_terms,
    get_settings,
    load_classify_schedule_config,
    load_escalation_criteria,
    load_model_tiers,
    load_schedule_config,
    load_search_terms,
    protection_tier_for,
    save_classify_schedule_config,
    save_escalation_criteria,
    save_model_tiers,
    save_schedule_config,
    save_search_terms,
)
from radar.db import (
    AlertAlreadyClaimedError,
    claim_alert,
    count_advertisements,
    get_alert_actions,
    get_alerts,
    get_cluster_brief,
    get_connection,
    get_incident_timeline,
    get_last_classified_at,
    get_last_collected_at,
    get_latest_raw_text,
    get_snapshot_history,
    get_unscored_pain_points,
    init_db,
    log_alert_action,
    release_alert,
    save_cluster_brief,
    save_coa,
    save_exec_brief,
    save_incident_report,
    save_technical_explanation,
    transition_incident,
    write_alert,
)
from radar.leadtime import compute_lead_times, summarize_lead_times
from radar.score import compute_velocity, run_scoring

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
        "incident_status",
        "exec_brief",
        "exec_brief_generated_at",
        "incident_report",
        "incident_report_generated_at",
        "coa",
        "coa_generated_at",
        "technical_explanation",
        "technical_explanation_generated_at",
        "action_count",
        "resolved_at",
        "claimed_by",
        "claimed_at",
    )
    return dict(zip(keys, row))


def _facts_from_alert(alert: dict, model_tiers: dict) -> BriefFacts:
    return BriefFacts(
        subject=f"{alert['platform']} alert",
        category=alert["category"],
        severity=alert["severity"],
        issue_summary=alert["issue_summary"],
        platforms=[alert["platform"]],
        velocity=alert["velocity"],
        matched_term=alert.get("matched_term"),
        protection_tier=protection_tier_for(model_tiers, alert["model_implicated"]),
        client=alert.get("client"),
    )


def _facts_from_cluster(cluster: ClusterSummary) -> BriefFacts:
    category = cluster.cluster_key.split(":", 1)[0]
    return BriefFacts(
        subject=cluster.label,
        category=category,
        severity=cluster.max_severity,
        issue_summary=cluster.representative_issue_summary,
        platforms=cluster.platforms,
        member_count=cluster.alert_count,
        episode_count=cluster.episode_count,
        protection_tier=cluster.protection_tier,
    )


@app.get("/api/alerts")
def api_alerts(category: str | None = None, severity: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        rows = get_alerts(conn, category=category, severity=severity)
        search_config = load_search_terms()
        criteria = load_escalation_criteria()
    finally:
        conn.close()
    dicts = [_alert_dict(row) for row in rows]
    for d in dicts:
        d["client"] = client_for_matched_term(d.get("matched_term"), search_config)
        d["action_items"] = criteria.get(d["category"], {}).get("action_items", [])
    return dicts


@app.get("/api/clusters")
def api_clusters() -> list[dict]:
    conn = _connect()
    try:
        settings = get_settings()
        model_tiers = load_model_tiers()
        clusters = get_clusters(
            conn, recurrence_gap_hours=settings.recurrence_gap_hours, model_tiers=model_tiers
        )
        result = []
        for c in clusters:
            d = c.__dict__.copy()
            d["brief"] = get_cluster_brief(conn, c.cluster_key)
            result.append(d)
    finally:
        conn.close()
    return result


@app.get("/api/watching")
def api_watching() -> list[dict]:
    """Pain points that are classified but haven't (yet, or ever) crossed the
    velocity threshold into a real alert -- see get_unscored_pain_points.
    """
    conn = _connect()
    try:
        rows = get_unscored_pain_points(conn)
        search_config = load_search_terms()
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
    dicts = [dict(zip(keys, row)) for row in rows]
    for d in dicts:
        d["client"] = client_for_matched_term(d.get("matched_term"), search_config)
    return dicts


@app.post("/api/watching/{post_id}/promote")
def api_promote_to_alert(post_id: str) -> dict:
    """Manually sends a Watching-tier post (never crossed the velocity
    threshold) into the Alerts tab -- the footprint graph's "Send to Alerts"
    action for a post a human judges worth tracking even though the
    automated scoring never flagged it. Reuses run_scoring()'s exact
    write path (radar/score.py) so a manually-promoted alert behaves
    identically to an automatic one -- same qa_status gating, same schema.
    """
    conn = _connect()
    try:
        if get_alerts(conn, post_id=post_id):
            raise HTTPException(status_code=400, detail="This post is already an alert.")
        rows = get_unscored_pain_points(conn, post_id=post_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"No watching pain point found for {post_id}")
        _, category, severity = rows[0][:3]

        criteria = load_escalation_criteria()
        qa_status = "pending" if category_requires_qa(criteria, category) else "not_required"
        history = get_snapshot_history(conn, post_id)
        velocity = compute_velocity(history) or 0.0
        latest_score = history[-1][1] if history else 0.0

        write_alert(conn, post_id, latest_score, velocity, category, severity, qa_status)
        alert = _get_one_alert(conn, post_id)
    finally:
        conn.close()
    return alert


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
    # `is not None`, not truthiness: an explicit empty list ("I deselected
    # every source picker checkbox") must mean "collect nothing", not be
    # silently treated the same as `sources` being omitted entirely.
    requested = set(body.sources) if body.sources is not None else None
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


@app.post("/api/classify")
def api_classify() -> dict:
    """Triggers a real classification pass over whatever's currently
    unclassified (up to CLASSIFY_BATCH_LIMIT posts), then immediately scores
    the results so any newly-classified pain point that already crosses its
    velocity threshold turns into a real alert in the same request -- without
    this, nothing but the `radar score` CLI or a manual "send to Alerts"
    click ever calls run_scoring(), so classifying from the dashboard would
    otherwise never actually produce an alert. Runs synchronously in-request,
    same "fine at this scale" tradeoff as /api/collect above. Calls the paid
    Anthropic API -- unlike /api/collect, this isn't run implicitly by
    anything else in the UI.
    """
    settings = get_settings()
    result = run_classification(settings)
    scoring = run_scoring(settings)
    return {
        "posts_classified": result.posts_classified,
        "skipped": result.skipped,
        "alerts_written": scoring.alerts_written,
    }


def _search_terms_payload(config: dict) -> dict:
    effective = effective_terms(config)
    # Same cross-product count effective_terms() itself builds from (generic
    # terms + every client x risk_pattern pair) -- if that's more than
    # MAX_EFFECTIVE_TERMS, some cross-product terms were silently dropped and
    # the dashboard should be able to say so rather than just showing a
    # shorter-than-expected list with no explanation.
    total_possible = len(config.get("terms", [])) + len(config.get("clients", [])) * len(
        config.get("risk_patterns", [])
    )
    return {
        "terms": config.get("terms", []),
        "clients": config.get("clients", []),
        "risk_patterns": config.get("risk_patterns", []),
        "effective_terms": effective,
        "effective_terms_truncated": total_possible > len(effective),
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


IncidentStatus = Literal["open", "acknowledged", "mitigating", "resolved", "false_positive"]


class IncidentTransition(BaseModel):
    status: IncidentStatus
    note: str | None = None


# Landing in one of these columns is a real decision point worth a
# recommendation; "open" has nothing to recommend yet (nothing's happened),
# and "false_positive" has nothing left to act on (dismissed).
_COA_ELIGIBLE_STATUSES = {"acknowledged", "mitigating", "resolved"}


@app.post("/api/alerts/{post_id}/transition")
def api_alert_transition(post_id: str, body: IncidentTransition) -> dict:
    """Moves an alert through its incident lifecycle (the Kanban board's drag
    target) -- independent of qa_status (see transition_incident()'s
    docstring). Landing in an actionable column auto-generates a Course of
    Action, grounded in that category's escalation-criteria playbook.
    """
    conn = _connect()
    try:
        alert = _get_one_alert(conn, post_id)

        if body.status == alert["incident_status"]:
            # No-op: already in this column (a duplicate drop, a double-
            # submitted request). Returning early avoids both logging a
            # from==to junk timeline event and re-billing/regenerating the
            # Claude COA call below for nothing.
            return {"post_id": post_id, "incident_status": body.status, "coa": alert["coa"]}

        if body.status == "resolved" and not get_alert_actions(conn, post_id):
            raise HTTPException(
                status_code=400, detail="Log at least one action before resolving this alert."
            )

        coa = None
        if body.status in _COA_ELIGIBLE_STATUSES:
            settings = get_settings()
            criteria = load_escalation_criteria()
            playbook = criteria.get(alert["category"], {}).get("response_template", "")
            facts = _facts_from_alert(alert, load_model_tiers())
            coa = generate_coa(settings, facts, playbook, body.status)

        changed = transition_incident(conn, post_id, body.status, note=body.note, coa=coa)
        if changed and coa:
            save_coa(conn, post_id, coa)
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail=f"No alert found for {post_id}")
    return {"post_id": post_id, "incident_status": body.status, "coa": coa}


class ClaimRequest(BaseModel):
    claimed_by: str


@app.post("/api/alerts/{post_id}/claim")
def api_alert_claim(post_id: str, body: ClaimRequest) -> dict:
    """Assigns the alert to a PM's personal Board tab (radar/static/dashboard.js's
    Board view, which only shows claimed alerts). claimed_by is freeform text --
    no auth system exists.
    """
    conn = _connect()
    try:
        try:
            changed = claim_alert(conn, post_id, body.claimed_by)
        except AlertAlreadyClaimedError as exc:
            raise HTTPException(
                status_code=409, detail=f"Already claimed by {exc.claimed_by}."
            ) from exc
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail=f"No alert found for {post_id}")
    return {"post_id": post_id, "claimed_by": body.claimed_by}


@app.post("/api/alerts/{post_id}/release")
def api_alert_release(post_id: str) -> dict:
    """Clears a claim, handing the alert back to the team-wide Alerts pool."""
    conn = _connect()
    try:
        changed = release_alert(conn, post_id)
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail=f"No alert found for {post_id}")
    return {"post_id": post_id, "claimed_by": None}


class AlertActionRequest(BaseModel):
    action_item: str
    note: str | None = None


@app.post("/api/alerts/{post_id}/actions")
def api_alert_log_action(post_id: str, body: AlertActionRequest) -> dict:
    """Records that a human carried out one of the category's recommended
    action items for this alert -- gates api_alert_transition()'s move to
    'resolved'. When the category has a configured action_items checklist,
    `action_item` must be one of those entries, so the log can't drift from
    what the board displays; a category with no checklist configured (an
    empty action_items list) has nothing to validate against and accepts a
    freeform description instead.
    """
    conn = _connect()
    try:
        alert = _get_one_alert(conn, post_id)
        criteria = load_escalation_criteria()
        valid_items = criteria.get(alert["category"], {}).get("action_items", [])
        if valid_items and body.action_item not in valid_items:
            raise HTTPException(
                status_code=400, detail="Not a recognized action item for this alert's category."
            )
        log_alert_action(conn, post_id, body.action_item, note=body.note)
    finally:
        conn.close()
    return {"post_id": post_id, "action_label": body.action_item, "note": body.note}


@app.get("/api/alerts/{post_id}/actions")
def api_alert_actions(post_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = get_alert_actions(conn, post_id)
    finally:
        conn.close()
    return [
        {"action_label": action_label, "note": note, "created_at": created_at}
        for action_label, note, created_at in rows
    ]


@app.get("/api/alerts/{post_id}/timeline")
def api_alert_timeline(post_id: str) -> list[dict]:
    conn = _connect()
    try:
        events = get_incident_timeline(conn, post_id)
    finally:
        conn.close()
    return [
        {
            "from_status": from_status,
            "to_status": to_status,
            "note": note,
            "coa": coa,
            "created_at": created_at,
        }
        for from_status, to_status, note, coa, created_at in events
    ]


def _get_one_alert(conn, post_id: str) -> dict:
    rows = get_alerts(conn, post_id=post_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No alert found for {post_id}")
    alert = _alert_dict(rows[0])
    alert["client"] = client_for_matched_term(alert.get("matched_term"), load_search_terms())
    criteria = load_escalation_criteria()
    alert["action_items"] = criteria.get(alert["category"], {}).get("action_items", [])
    return alert


@app.post("/api/alerts/{post_id}/brief")
def api_alert_brief(post_id: str) -> dict:
    """Generates (or regenerates) this alert's exec brief and persists it, so
    it isn't silently re-billed/regenerated every time the panel is opened.
    """
    conn = _connect()
    try:
        alert = _get_one_alert(conn, post_id)
        brief = generate_exec_brief(get_settings(), _facts_from_alert(alert, load_model_tiers()))
        save_exec_brief(conn, post_id, brief)
    finally:
        conn.close()
    return {"post_id": post_id, "brief": brief}


@app.post("/api/alerts/{post_id}/explain")
def api_alert_technical_explanation(post_id: str) -> dict:
    """Generates (or regenerates) a longer, technical explanation of the
    underlying issue, grounded in the post's full original text -- not the
    120-char issue_summary the Alerts/Board tables show, which is meant to
    be scannable, not explanatory. Persisted the same way the exec brief is,
    so it isn't silently re-billed every time the panel is opened.
    """
    conn = _connect()
    try:
        alert = _get_one_alert(conn, post_id)
        raw_text = get_latest_raw_text(conn, post_id)
        explanation = generate_technical_explanation(
            get_settings(), _facts_from_alert(alert, load_model_tiers()), raw_text
        )
        save_technical_explanation(conn, post_id, explanation)
    finally:
        conn.close()
    return {"post_id": post_id, "technical_explanation": explanation}


@app.post("/api/clusters/{cluster_key}/brief")
def api_cluster_brief(cluster_key: str) -> dict:
    conn = _connect()
    try:
        settings = get_settings()
        clusters = get_clusters(
            conn, recurrence_gap_hours=settings.recurrence_gap_hours, model_tiers=load_model_tiers()
        )
        cluster = next((c for c in clusters if c.cluster_key == cluster_key), None)
        if cluster is None:
            raise HTTPException(status_code=404, detail=f"No cluster found for {cluster_key}")
        brief = generate_exec_brief(settings, _facts_from_cluster(cluster))
        save_cluster_brief(conn, cluster_key, brief)
    finally:
        conn.close()
    return {"cluster_key": cluster_key, "brief": brief}


class IncidentReportRequest(BaseModel):
    closing_note: str


@app.post("/api/alerts/{post_id}/report")
def api_alert_report(post_id: str, body: IncidentReportRequest) -> dict:
    conn = _connect()
    try:
        alert = _get_one_alert(conn, post_id)
        timeline = get_incident_timeline(conn, post_id)
        report_markdown = generate_incident_report(
            get_settings(), _facts_from_alert(alert, load_model_tiers()), timeline, body.closing_note
        )
        save_incident_report(conn, post_id, report_markdown)
    finally:
        conn.close()
    return {"post_id": post_id, "report_markdown": report_markdown}


class CategoryCriteria(BaseModel):
    requires_qa: bool
    velocity_threshold: float | None
    response_template: str


class EscalationCriteriaUpdate(BaseModel):
    categories: dict[str, CategoryCriteria]


@app.get("/api/escalation-criteria")
def api_get_escalation_criteria() -> dict:
    return {"categories": load_escalation_criteria()}


@app.put("/api/escalation-criteria")
def api_update_escalation_criteria(body: EscalationCriteriaUpdate) -> dict:
    """Persists per-category QA/velocity/playbook criteria to
    config/escalation_criteria.yaml -- also changes what `radar score` uses,
    not just dashboard views.
    """
    updates = {category: fields.model_dump() for category, fields in body.categories.items()}
    updated = save_escalation_criteria(updates)
    return {"categories": updated}


class ModelTierCriteria(BaseModel):
    protection_tier: Literal["flagship", "standard", "legacy"]
    velocity_threshold: float | None


class ModelTiersUpdate(BaseModel):
    models: dict[str, ModelTierCriteria]


@app.get("/api/model-tiers")
def api_get_model_tiers() -> dict:
    return {"models": load_model_tiers()}


@app.put("/api/model-tiers")
def api_update_model_tiers(body: ModelTiersUpdate) -> dict:
    """Persists per-model protection tier/velocity override to
    config/model_tiers.yaml -- also changes what `radar score` uses, not just
    dashboard views.
    """
    updates = {model: fields.model_dump() for model, fields in body.models.items()}
    updated = save_model_tiers(updates)
    return {"models": updated}


class ScheduleUpdate(BaseModel):
    enabled: bool
    interval_seconds: int


@app.get("/api/schedule")
def api_get_schedule() -> dict:
    conn = _connect()
    try:
        last_collected_at = get_last_collected_at(conn)
    finally:
        conn.close()
    return {
        **load_schedule_config(),
        "last_collected_at": last_collected_at.isoformat() if last_collected_at else None,
    }


@app.put("/api/schedule")
def api_update_schedule(body: ScheduleUpdate) -> dict:
    """Persists radar serve's background-scheduler on/off + interval to
    config/schedule.yaml -- radar/scheduler.py's loop re-reads this file every
    tick, so this takes effect within CHECK_INTERVAL_SECONDS, no restart.
    """
    return save_schedule_config({"enabled": body.enabled, "interval_seconds": body.interval_seconds})


class ClassifyScheduleUpdate(BaseModel):
    enabled: bool
    interval_seconds: int


@app.get("/api/classify-schedule")
def api_get_classify_schedule() -> dict:
    conn = _connect()
    try:
        last_classified_at = get_last_classified_at(conn)
    finally:
        conn.close()
    return {
        **load_classify_schedule_config(),
        "last_classified_at": last_classified_at.isoformat() if last_classified_at else None,
    }


@app.put("/api/classify-schedule")
def api_update_classify_schedule(body: ClassifyScheduleUpdate) -> dict:
    """Same shape as /api/schedule above, for the classify scheduler --
    independent config/classify_schedule.yaml, independent interval."""
    return save_classify_schedule_config({"enabled": body.enabled, "interval_seconds": body.interval_seconds})


# Mounted last so the /api/* routes above take precedence over this catch-all.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    from radar.scheduler import start_scheduler_thread

    start_scheduler_thread(get_settings())
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
