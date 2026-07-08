# Claude Radar

A public-signal early-warning tool. It watches public posts about Claude / the Claude API,
uses an LLM to decide which are real pain points, scores their engagement velocity, gates
sensitive categories behind human review, clusters them by root cause, and surfaces the
important clusters on a dashboard with a lead-time metric — how early we spotted an issue
versus when it went viral. Built as a portfolio/interview prototype.

**Status: Phase 9 of 9 (all phases scaffolded).** Collectors for Reddit, YouTube, Hacker News,
Stack Overflow, and GitHub Issues write a time series of post snapshots to SQLite; a
Claude-based classifier labels pain points; a scorer turns accelerating pain points into
alerts; a human QA gate reviews sensitive categories; a clustering step groups alerts by root
cause; a FastAPI + static dashboard surfaces all of it; a lead-time metric measures how early
the early-warning pass caught things; and a backtest CLI replays scored alerts against known
past incidents. An X/Twitter source exists behind a feature flag but ships inert (no free API
tier — see Phase 9 below). See "How it works" for the full per-phase breakdown.

## Guardrails

1. **Official APIs only. No scraping, no fake accounts, no logging into anyone's platform.**
   Everything uses sanctioned developer APIs with real, registered app credentials. If an API
   can't do something, that's a documented limitation, not something to route around.
2. **No personal dossiers.** We store the minimum: post ID, platform, timestamp, public
   metrics, text, and derived labels — never a per-user profile. `HASH_AUTHORS=true` (on by
   default) one-way hashes author handles before storage.
3. **Respect each platform's Terms of Service and rate limits.** Polite pacing and exponential
   backoff on every request.
4. **Secrets live in `.env`, never committed.** `.env.example` ships with blank values.
5. **Everything runs locally.** SQLite + a local process. No cloud, no third-party data
   sharing.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Then edit `.env`:
- Set `AUTHOR_HASH_PEPPER` to any random string (required whenever `HASH_AUTHORS=true`,
  which is the default).
- Fill in `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` once you have a Reddit app (below).
  Until then, `radar collect` skips Reddit with a warning instead of failing.
- Fill in `ANTHROPIC_API_KEY` once you want to classify collected posts. Until then, `radar
  classify` runs and exits cleanly with a warning instead of failing.
- Fill in `YOUTUBE_API_KEY` once you have one (below) to also collect from YouTube.
- `ENABLE_X_SOURCE` / `X_BEARER_TOKEN` are an inert feature flag — see Phase 9 below.
- Set `ENABLE_HACKERNEWS_SOURCE=true` to collect from Hacker News — no key needed at all
  (Algolia's HN Search API is free and keyless). Off by default purely so enabling it is a
  deliberate choice, not because it costs anything.
- Set `ENABLE_STACKOVERFLOW_SOURCE=true` to collect from Stack Overflow — also no key
  required (300 req/day shared-IP quota); optionally set `STACKOVERFLOW_API_KEY` (below) for
  a higher quota.
- Fill in `GITHUB_TOKEN` once you have one (below) to collect from GitHub Issues.

### Creating a Reddit API app

1. Go to <https://www.reddit.com/prefs/apps> (log in with any Reddit account).
2. Click "create app" (or "create another app").
3. Choose type **script**.
4. Name it anything (e.g. `claude-radar`); redirect URI can be `http://localhost:8080`
   (unused by this project — script apps don't need it, but Reddit's form requires a value).
5. After creating it, the string under the app name is `REDDIT_CLIENT_ID`; the "secret" field
   is `REDDIT_CLIENT_SECRET`.
6. Set `REDDIT_USER_AGENT` to something identifying, per Reddit's API rules, e.g.
   `claude-radar/0.1 by u/yourusername`.

This project uses the OAuth2 **`client_credentials`** grant — app-only, read-only access to
public search/listing endpoints. It never authenticates as a specific Reddit account.

### Creating a YouTube API key

1. Create (or reuse) a project in the [Google Cloud Console](https://console.cloud.google.com/),
   enable the **YouTube Data API v3**, then create an **API key** credential.
2. Set `YOUTUBE_API_KEY` to that key. No OAuth, no account login — public search/video
   endpoints only.
3. `search.list` calls are quota-expensive (100 units each, of a 10,000/day default quota) —
   keep `config/search_terms.yaml`'s term list reasonably small.

### Getting a Stack Overflow API key (optional)

Not required — Stack Exchange's API works unauthenticated at a 300-request/day shared-IP
quota. If you want the higher 10,000/day quota, register an app at
<https://stackapps.com/apps/oauth/register> and set `STACKOVERFLOW_API_KEY` to the resulting
key (no OAuth flow needed for read-only public search).

### Creating a GitHub personal access token

1. Go to <https://github.com/settings/tokens> → **Generate new token (classic)**.
2. No scopes are required — this only reads public issue search results. An unscoped
   "read-only" token is enough (scopes matter for accessing private repos, which this never
   does).
3. Set `GITHUB_TOKEN` to the generated token. Unauthenticated GitHub search is capped at 10
   requests/minute, too low to be usable across several search terms — a token (even with no
   scopes) raises this to a workable rate.

## Running the collector

```bash
radar collect
# or: python -m radar.collect
```

Loads `config/search_terms.yaml` and runs every **configured** source: Reddit
(`REDDIT_CLIENT_ID`/`SECRET`), YouTube (`YOUTUBE_API_KEY`), Hacker News
(`ENABLE_HACKERNEWS_SOURCE=true`), Stack Overflow (`ENABLE_STACKOVERFLOW_SOURCE=true`),
GitHub Issues (`GITHUB_TOKEN`), and X (`ENABLE_X_SOURCE=true` + `X_BEARER_TOKEN`, inert
without paid access — see Phase 9). Each does a `search_top` (most-engaged) and a
`search_recent` (newest) pass per term, writing one row per matched post to the `snapshots`
table in `data/radar.db`. A source with no credentials/flag is skipped with a log line, not a
hard failure; the whole command only no-ops if *no* source is configured. Re-running it later
adds new snapshot rows for the same posts — that accumulating time series is what scoring uses
to compute engagement velocity, and what the lead-time metric reads.

Tuning the watchlist: edit `config/search_terms.yaml` (subreddits + search terms) and re-run.

## Running the classifier

```bash
radar classify
# or: python -m radar.classify
```

Reads up to `CLASSIFY_BATCH_LIMIT` (default 100) posts from `snapshots` that don't yet have a
row in `classifications` (one classification per post, using its most recent snapshot), sends
each to Claude (`CLASSIFIER_MODEL`) with a forced tool call to get a structured
`is_pain_point` / `category` / `model_implicated` / `severity` / `issue_summary` result, and
writes it to the `classifications` table. A single post that fails to classify (API error or
an unusable response) is logged and skipped without failing the rest of the batch. Unlike
`radar collect`, **this calls the paid Anthropic API** — run it deliberately, not on a tight
poll loop.

## Scoring alerts

```bash
radar score
```

For every post classified as a pain point, computes **velocity** (virality-score change per
hour between its two most recent snapshots) and writes a row to the `alerts` table if velocity
clears `VELOCITY_THRESHOLD` — but **only if it's accelerating past its own last alert**, not
just still above threshold, so a steady (non-accelerating) pain point doesn't re-fire every
run. Each alert is stamped `qa_status='pending'` if its category is in `HUMAN_QA_CATEGORIES`
(`abuse`, `credential_theft`, `safety` by default) or `'not_required'` otherwise.

## Human QA review

```bash
radar review              # list alerts pending human review
radar review approve t3_abc123
radar review reject t3_abc123
```

This is the concrete form of the "gate before anything could fire an external alert" in a
local-only tool: `'pending'` alerts (sensitive categories) sit here until a human approves or
rejects them — via this CLI or the dashboard's own approve/reject buttons (same effect,
`radar/qa.py` backs both).

## Root-cause clusters

```bash
radar clusters
```

Groups all alerts by `(category, model_implicated)` — a deterministic, dependency-free
grouping computed at query time (no separate table to drift out of sync) — and prints each
cluster's alert count, worst severity, and a representative issue summary. The dashboard's
`/api/clusters` endpoint calls the same function.

## Lead-time metric

```bash
radar leadtime
```

For each post, compares the first time it was caught by the `recent` (early-warning) pass
against the first time it was prominent enough to appear in the `top` (most-engaged) pass —
our proxy for "went viral" absent external ground truth. A positive lead time means the
early-warning pass caught it first. Prints the median/mean lead time and how many posts were
caught early; `/api/lead-time` serves the same data (plus the full distribution) to the
dashboard.

## Backtesting

```bash
radar backtest
```

Replays scored alerts against `config/known_incidents.yaml` (a human-curated list of real
incident time windows — ships with only a placeholder entry; **fill in real incidents once
you have weeks of `radar collect` + `radar score` history**, since this repo has none yet).
For each incident, reports a hit (with lead time — how long before the incident window an
alert already fired) or a miss, plus an aggregate hit rate.

## Serving the dashboard

```bash
radar serve
```

Serves a FastAPI backend + static frontend at <http://127.0.0.1:8000> (local only): a
filterable alerts table (status/category/severity) with inline approve/reject actions, a
root-cause cluster chart, and the lead-time stat + distribution. Pure HTML/CSS/vanilla JS —
no build step, no CDN, works fully offline. See `radar/api.py` and `radar/static/`.

## Tests

```bash
pytest
```

The whole suite runs against fixtures under
`tests/fixtures/{reddit,anthropic,youtube,x,hackernews,stackoverflow,github}/` via
`respx`-mocked HTTP, plus FastAPI's `TestClient` for the dashboard API — no live credentials or
network access required for any of it.

## How it works (per phase)

- **Phase 1:** `Source` interface, `RawPost`/`Classification` data models, and a
  `RedditSource` collector writing snapshots to SQLite. `radar/sources/reddit.py`,
  `radar/collect.py`.
- **Phase 2:** Claude-based classifier (`is_pain_point`, `category`, `severity`,
  `model_implicated`, `issue_summary`), calling the Messages API directly via `httpx` (same
  pattern as the Reddit source, reusing `RateLimitedClient`) with a forced tool call for
  structured output. `radar/classify.py`.
- **Phase 3:** Score + diff + velocity — `radar/score.py`. Suppresses repeat alerts unless
  engagement is accelerating past the post's own last alert.
- **Phase 4:** Human QA gate for sensitive categories (`abuse`, `credential_theft`, `safety`)
  — `radar/qa.py` + `radar review`. Pending sensitive alerts are held for a human decision
  before they'd ever be surfaced as "released."
- **Phase 5:** Clustering into root-cause groups — `radar/cluster.py`. Deterministic grouping
  by `(category, model_implicated)`, computed at query time.
- **Phase 6:** FastAPI + interactive static dashboard — `radar/api.py`, `radar/static/`.
- **Phase 7:** Lead-time metric — `radar/leadtime.py`, using the `search_recent`/`search_top`
  passes already wired into the Phase 1 collector.
- **Phase 8:** Backtest CLI against known past incidents — `radar/backtest.py`,
  `config/known_incidents.yaml`.
- **Phase 9:** `YouTubeSource` (`radar/sources/youtube.py`, YouTube Data API v3, API-key auth),
  `HackerNewsSource` (`radar/sources/hackernews.py`, Algolia HN Search API, free and keyless),
  `StackOverflowSource` (`radar/sources/stackoverflow.py`, Stack Exchange API, key optional),
  and `GitHubSource` (`radar/sources/github.py`, REST issue search, personal access token) are
  all fully wired into `radar collect`. `XSource` (`radar/sources/x.py`, X API v2 recent
  search) exists behind the `ENABLE_X_SOURCE` feature flag and is fully unit-tested via mocked
  HTTP, but modern X API has no free tier for search — it ships inert (no bearer token
  configured) rather than assuming paid access. Reddit access itself has become harder to get
  since Reddit gates new script-app creation behind a "valid moderation use case" review — see
  the Reddit setup section above; the other sources exist in part because Reddit access isn't
  guaranteed.

## Data model

- `snapshots` — one row per `(post_id, poll_run_id, search_pass)`. This is the time series:
  post id, platform, hashed author, collected/created timestamps, public metrics, a computed
  `virality_score`, and the raw text (subject to a future retention-purge job). Never a
  per-user profile.
- `classifications` — one row per `post_id` (not a time series; a re-run replaces the prior
  row): `is_pain_point`, `category`, `model_implicated`, `severity`, `issue_summary`, which
  `classifier_model` produced it, and when.
- `alerts` — one row per *alert event* (a post can re-alert if it accelerates again):
  `post_id`, `triggered_at`, `virality_score`, `velocity`, `category`, `severity`, and
  `qa_status` (`pending` / `approved` / `rejected` / `not_required`).

## Project layout

```
radar/
├── models.py              # RawPost, Metrics, Classification + enums
├── config.py               # Settings (pydantic-settings) + search_terms.yaml / known_incidents.yaml loaders
├── hashing.py               # author hashing
├── virality.py              # virality score formula
├── http_utils.py             # shared rate-limit/backoff HTTP helper (handles both relative-
│                             # seconds and absolute-epoch rate-limit-reset header semantics)
├── db.py                     # SQLite schema + all queries/writes
├── sources/
│   ├── base.py                # Source protocol (search_top / search_recent)
│   ├── reddit.py               # RedditSource
│   ├── youtube.py               # YouTubeSource
│   ├── hackernews.py             # HackerNewsSource (Algolia HN Search API)
│   ├── stackoverflow.py            # StackOverflowSource (Stack Exchange API)
│   ├── github.py                     # GitHubSource (Issues search only, not Discussions)
│   └── x.py                            # XSource (feature-flagged, inert without a paid token)
├── collect.py                             # orchestration across all configured sources: `radar collect`
├── classify.py                     # ClaudeClassifier + orchestration: `radar classify`
├── score.py                         # velocity scoring + alert suppression: `radar score`
├── qa.py                              # human QA gate: `radar review`
├── cluster.py                          # root-cause clustering: `radar clusters`
├── leadtime.py                          # lead-time metric: `radar leadtime`
├── backtest.py                           # backtest CLI: `radar backtest`
├── api.py                                 # FastAPI app + `radar serve`
├── static/                                 # dashboard frontend (plain HTML/CSS/JS)
│   ├── index.html
│   ├── dashboard.css
│   └── dashboard.js
└── cli.py                                   # `radar <command>` entry point
```
