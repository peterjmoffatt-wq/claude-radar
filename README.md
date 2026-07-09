# Claude Radar

A public-signal early-warning tool. It watches public posts about Claude / the Claude API,
uses an LLM to decide which are real pain points, scores their engagement velocity, gates
sensitive categories behind human review, clusters them by root cause, and surfaces the
important clusters on a dashboard with a lead-time metric — how early we spotted an issue
versus when it went viral. Built as a portfolio/interview prototype.

**Status: Phase 9 of 9 (all phases scaffolded), plus a Mastodon source, a dashboard source
picker/live-collection trigger, and an incident-response layer (lifecycle, exec briefs,
post-incident reports, recurrence detection, dashboard-editable escalation criteria) added
afterward.** Collectors for Reddit, YouTube, Hacker News, Stack Overflow, GitHub Issues, and
Mastodon write a time series of post snapshots to SQLite; a Claude-based classifier labels
pain points; a scorer turns accelerating pain points into alerts; a human QA gate reviews
sensitive categories; a clustering step groups alerts by root cause and flags recurrence; a
FastAPI + static dashboard surfaces all of it (including an interactive, draggable
cross-platform footprint graph on the home tab, and a per-alert incident-lifecycle/exec-brief/
post-incident-report panel); a lead-time metric measures how early the early-warning pass
caught things; and a backtest CLI replays scored alerts against known past incidents. An
X/Twitter source exists behind a feature flag but ships inert (no free API tier — see Phase 9
below). See "How it works" for the full per-phase breakdown, and "Platforms not included" for
what a partner-API-funded version would add next.

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
- Fill in `MASTODON_INSTANCE_URL` + `MASTODON_ACCESS_TOKEN` once you have both (below) to
  collect from Mastodon.

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

### Creating a Mastodon access token

1. Pick a Mastodon instance (e.g. `https://mastodon.social`, or any instance you have an
   account on) and log in.
2. Go to **Settings → Development → New Application**. Give it any name; the only scope this
   project needs is `read:search` (or just `read`, which includes it).
3. Set `MASTODON_INSTANCE_URL` to the instance's base URL (e.g. `https://mastodon.social`) and
   `MASTODON_ACCESS_TOKEN` to the application's generated access token.
4. **This searches one instance's known/federated statuses, not "all of Mastodon"** — there is
   no global search across the fediverse; each instance only indexes posts it has seen. Pick an
   instance with broad federation (mastodon.social is a reasonable default) to maximize
   coverage. Confirmed live before wiring this up: unauthenticated account/hashtag search works
   on `mastodon.social`, but status (post) search returns an empty result set without a bearer
   token — that's why this needs a real token, unlike Hacker News or Stack Overflow.

## Platforms not included

The dashboard's source picker also lists Discord, LinkedIn, TikTok, and Threads as disabled
"Coming soon" checkboxes — deliberately shown, not hidden, as the honest next step of this
story: none of them expose a public search API accessible without a partner/business
relationship (Discord has no cross-server public search API at all; LinkedIn, TikTok, and
Threads gate their APIs behind app review or partnership tiers this project doesn't have).
Building a fake collector against a scraped or unofficial endpoint for any of them would break
guardrail #1 above, so they stay as picker entries with no backing code — a real placeholder
for "what a funded version adds next," not a working feature.

**Bluesky was evaluated and deliberately left out entirely** (not even as "coming soon"). Its
public, unauthenticated API works fine for profile/account lookups, but the actual
`app.bsky.feed.searchPosts` endpoint returns `403 Forbidden` without an authenticated session —
confirmed live before ruling it out. The *only* way to search posts is to authenticate with an
account handle + a Bluesky "app password" (a scoped secondary credential, not your real
password, but still logging into a real account to do it). That sits closer to the "no logging
into anyone's platform" guardrail line than Reddit's app-only `client_credentials` grant or
Mastodon/GitHub/X's bearer-token model, none of which require *any* account to exist. Rather
than quietly building something that stretches that guardrail, it's excluded outright.

## Running the collector

```bash
radar collect
# or: python -m radar.collect
```

Loads `config/search_terms.yaml` and runs every **configured** source: Reddit
(`REDDIT_CLIENT_ID`/`SECRET`), YouTube (`YOUTUBE_API_KEY`), Hacker News
(`ENABLE_HACKERNEWS_SOURCE=true`), Stack Overflow (`ENABLE_STACKOVERFLOW_SOURCE=true`),
GitHub Issues (`GITHUB_TOKEN`), Mastodon (`MASTODON_INSTANCE_URL`/`MASTODON_ACCESS_TOKEN`), and
X (`ENABLE_X_SOURCE=true` + `X_BEARER_TOKEN`, inert without paid access — see Phase 9). Each
does a `search_top` (most-engaged) and a `search_recent` (newest) pass per term, writing one
row per matched post to the `snapshots` table in `data/radar.db`. A source with no
credentials/flag is skipped with a log line, not a hard failure; the whole command only no-ops
if *no* source is configured. Re-running it later adds new snapshot rows for the same posts —
that accumulating time series is what scoring uses to compute engagement velocity, and what the
lead-time metric reads.

`run_collection()` also accepts an optional `sources` filter (a subset of configured source
names) — `radar collect` doesn't use it (always polls everything configured), but the
dashboard's source picker does, via `POST /api/collect` (see "Serving the dashboard" below).

**Tuning the watchlist**: edit `config/search_terms.yaml` directly, or use the dashboard's
Settings tab (writes to the same file, so `radar collect` picks up dashboard edits too — see
below). Two kinds of entries:
- `terms` — generic watchlist phrases (up to `MAX_WATCHLIST_ITEMS` = 10), searched as-is.
- `clients` × `risk_patterns` — the **client-scoped targeted-attack detection** layer.
  `radar/config.py`'s `effective_terms()` crosses every watched client name with every risk
  pattern into a combined query (e.g. client `"McDonald's"` + pattern `"jailbreak"` →
  `"McDonald's jailbreak"`), so you can ask "is a specific enterprise client's Claude deployment
  being targeted" without hand-typing every combination. `risk_patterns` ships with a starting
  set (jailbreak, prompt injection, credential leak, api key leak, token theft, code execution
  exploit, system prompt leak, data exfiltration) — edit it to tune what "targeted" means for
  your threat model. Each list is capped at `MAX_WATCHLIST_ITEMS` independently, but the
  *effective* query count is `len(terms) + len(clients) * len(risk_patterns)`, which can get
  large fast — the dashboard's Settings tab shows a live "effective query preview" so you can
  see the real count before running a collection pass.
- Every `snapshots` row keeps `matched_term` — which exact term (generic or a client×pattern
  combo) surfaced that post — surfaced on the Watching/Alerts tables and the footprint graph's
  detail panel/tooltip, so a hit from `"McDonald's jailbreak"` reads as exactly that, not a
  generic "Claude" mention.

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
clears the effective threshold — a per-category override (`config/escalation_criteria.yaml`,
dashboard-editable under Settings) if one is set, else the per-platform override
(`VELOCITY_THRESHOLD_OVERRIDES`), else the global `VELOCITY_THRESHOLD` — but **only if it's
accelerating past its own last alert**, not just still above threshold, so a steady
(non-accelerating) pain point doesn't re-fire every run. Each alert is stamped
`qa_status='pending'` if its category's `requires_qa` is true in `config/escalation_criteria.yaml`
(`abuse`, `credential_theft`, `safety` by default) or `'not_required'` otherwise.

Independent of `qa_status`, every alert also carries its own `incident_status`
(`open` → `acknowledged` → `mitigating` → `resolved`, or `false_positive`) — see
"Incident lifecycle, exec briefs, and post-incident reports" below.

## Human QA review

```bash
radar review              # list alerts pending human review
radar review approve t3_abc123
radar review reject t3_abc123
```

This is the concrete form of the "gate before anything could fire an external alert" in a
local-only tool: `'pending'` alerts (sensitive categories) sit here until a human approves or
rejects them — via this CLI or the dashboard's own approve/reject buttons (same effect,
`radar/qa.py` backs both). Rejecting an alert also auto-closes its `incident_status` as
`false_positive` (see below) — a rejected alert isn't a real incident to keep working.

## Incident lifecycle, exec briefs, and post-incident reports

Every alert also carries its own `incident_status` — `open` → `acknowledged` →
`mitigating` → `resolved` (or `false_positive`) — independent of `qa_status`: QA gates
whether the *classification* is legitimate, incident status tracks whether someone is
*actually working* it. Each transition is logged to `incident_events` (from/to/note/
timestamp), forming an audit trail. This, and everything below, is dashboard-only (no
CLI subcommands) — expand any row in the Alerts tab via **Details** to work an incident:
advance its status, generate a short **executive brief** (2-3 sentences, Claude-generated
via the same `httpx`+forced-nothing pattern the classifier uses for structured
classification, with a deterministic template fallback if `ANTHROPIC_API_KEY` is unset or
the call fails), and — once you're done — generate a **post-incident report**: a
Markdown document combining the hard facts, the full status timeline, a Claude-written
"what happened" narrative, and your own closing note on what should change, downloadable
as `.md` or copyable to the clipboard. The footprint graph's hub detail panel can
generate the same kind of brief for a whole root-cause cluster, not just one alert.

## Root-cause clusters

```bash
radar clusters
```

Groups all alerts by `(category, model_implicated)` — a deterministic, dependency-free
grouping computed at query time (no separate table to drift out of sync) — and prints each
cluster's alert count, worst severity, and a representative issue summary, plus (when it
applies) how many separate **episodes** it's recurred in: a quiet gap longer than
`RECURRENCE_GAP_HOURS` (default 48) between alerts starts a new episode, so "recurring ×3"
means this root cause has gone quiet and come back three separate times, not just alerted
three times in one burst. The dashboard's `/api/clusters` endpoint calls the same function
and surfaces the same fields (plus any cached exec brief) in the Home tab's cluster chart
and the footprint graph's hub detail panel.

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

Serves a FastAPI backend + static frontend at <http://127.0.0.1:8000> (local only). Pure
HTML/CSS/vanilla JS — no build step, no CDN, works fully offline. See `radar/api.py` and
`radar/static/`.

Navigation is a left sidebar (Home / Watching / Alerts / Settings), not top tabs.

**Home** is the cross-platform footprint graph plus the root-cause cluster chart and lead-time
stat. Nodes are pointer-draggable (drag to reposition, it stays pinned where you drop it) and
clicking any node opens a persistent detail panel (platform name, category, severity/velocity,
matched search term, summary, a real "View post" link) instead of relying on a hover tooltip —
deliberately so, since the raw `post_id` alone doesn't tell you which platform, issue, or
matched term you're looking at. **Filtering**: click a platform swatch in the legend to
hide/show its nodes (and any hub left with zero visible members), or click a category chip
above the graph to narrow to specific root causes — both read from the full fetched set, so
toggling is instant and doesn't re-fetch.

**Watching** and **Alerts** are the filterable, sortable tables — Platform/Category/Severity
filters on both (Alerts also filters by QA status), a Platform filter added to Alerts, and
click-to-sort column headers (Platform/Category/Severity/Velocity) with a ▲/▼ indicator; Alerts
keeps its inline approve/reject actions, plus an **Incident** status column and a **Details**
toggle per row that expands into the incident lifecycle/brief/report panel described above.
Both tables show **Matched term** so a client-scoped hit (e.g. `"McDonald's jailbreak"`) is
visibly distinct from a generic one.

**Settings** holds everything about *what* gets searched, separated from the "what did we
find" tabs above: the source picker (checkboxes for every real platform, pre-checked from
`GET /api/sources` if already configured in `.env`, plus disabled "Coming soon" entries for
Discord/LinkedIn/TikTok/Threads — see "Platforms not included") with a **Run collection**
button that `POST`s to `/api/collect` and triggers a real, live collection pass for just the
checked sources; three editable watchlists (Search terms, Watched clients, Risk patterns —
see "Tuning the watchlist" above) with a live "effective query preview" and a single **Save
watchlist** button that writes to `config/search_terms.yaml` (so CLI runs pick it up too, not
just dashboard clicks); and an **Escalation criteria** card — per category, whether it requires
human QA, an optional velocity-threshold override, and a first-response playbook — that writes
to `config/escalation_criteria.yaml`, so `radar score` (CLI or dashboard-triggered) picks up
edits the same way. In production, collection runs on a schedule (e.g. a cron/systemd timer
calling `radar collect`) rather than a manual click — this button is the same underlying
operation, triggered on demand for the dashboard/demo case.

`POST /api/collect` runs synchronously in-request, which is fine at this data volume but is a
tradeoff a production version would background instead. **A live click against several search
terms/sources can genuinely take a couple of minutes** (each request is politely paced ~1.2s
apart, and a source that errors retries with backoff before moving on) — the button shows
"Running…", gives up waiting client-side after 45s with an honest "still running on the server"
message rather than hanging with no feedback, and the collection keeps going regardless (the
abort only stops the browser from waiting, not the server-side work) — for a snappy live demo,
check only 1-2 already-configured sources before clicking. If a source errors out mid-run
(timeout, exhausted retries), it's isolated and reported in `sources_failed` — the run still
completes and keeps whatever the other sources wrote; found this via live testing against
Hacker News's real API, not hypothetically.

## Tests

```bash
pytest
```

The whole suite runs against fixtures under
`tests/fixtures/{reddit,anthropic,youtube,x,hackernews,stackoverflow,github,mastodon}/` via
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
- **Post-Phase-9:** `MastodonSource` (`radar/sources/mastodon.py`, one configured instance's
  `/api/v2/search`, bearer-token auth — see "Creating a Mastodon access token" above), plus the
  dashboard's source picker (`GET /api/sources`, `POST /api/collect` in `radar/api.py`) and the
  Home-tab footprint graph becoming draggable/click-for-detail (`radar/static/dashboard.js`).
  Bluesky was evaluated and excluded; Discord/LinkedIn/TikTok/Threads are cosmetic-only picker
  entries — see "Platforms not included."
- **Dashboard round 2:** left-sidebar navigation with a dedicated Settings tab; the client ×
  risk-pattern watchlist (`effective_terms()` in `radar/config.py`, `GET`/`PUT
  /api/search-terms` in `radar/api.py`, persisted to `config/search_terms.yaml`) plus
  `matched_term` surfaced end-to-end (`radar/db.py`'s `get_alerts()`/`get_unscored_pain_points()`
  → the Watching/Alerts tables and the footprint graph's detail panel/tooltip); footprint graph
  filtering by platform (click a legend swatch) and category (chip row above the graph);
  sortable Watching/Alerts columns.

## Data model

- `snapshots` — one row per `(post_id, poll_run_id, search_pass)`. This is the time series:
  post id, platform, hashed author, collected/created timestamps, public metrics, a computed
  `virality_score`, and the raw text (subject to a future retention-purge job). Never a
  per-user profile.
- `classifications` — one row per `post_id` (not a time series; a re-run replaces the prior
  row): `is_pain_point`, `category`, `model_implicated`, `severity`, `issue_summary`, which
  `classifier_model` produced it, and when.
- `alerts` — one row per *alert event* (a post can re-alert if it accelerates again):
  `post_id`, `triggered_at`, `virality_score`, `velocity`, `category`, `severity`,
  `qa_status` (`pending` / `approved` / `rejected` / `not_required`), `incident_status`
  (`open` / `acknowledged` / `mitigating` / `resolved` / `false_positive`), and a cached
  `exec_brief`/`incident_report` once generated.
- `incident_events` — append-only timeline of `incident_status` transitions
  (`alert_id`, `from_status`, `to_status`, `note`, `created_at`) — the audit trail behind
  the Alerts tab's Details panel and the post-incident report.
- `cluster_briefs` — one cached exec brief per root-cause cluster (`cluster_key`), since
  clusters themselves aren't otherwise persisted (`get_clusters()` computes them fresh).

## Project layout

```
radar/
├── models.py              # RawPost, Metrics, Classification + enums
├── config.py               # Settings (pydantic-settings) + search_terms.yaml / known_incidents.yaml /
│                            # escalation_criteria.yaml loaders
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
│   ├── mastodon.py                     # MastodonSource (one instance, bearer-token auth)
│   └── x.py                              # XSource (feature-flagged, inert without a paid token)
├── collect.py                             # orchestration across all configured sources: `radar collect`
├── classify.py                     # ClaudeClassifier + orchestration: `radar classify`
├── brief.py                          # exec-brief / post-incident-report generation (same Claude-call shape as classify.py)
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
