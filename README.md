# Claude Radar

A public-signal early-warning tool. It watches public posts about Claude / the Claude API,
uses an LLM to decide which are real pain points, clusters them by root cause, and (in later
phases) surfaces the important clusters on a dashboard with a lead-time metric — how early we
spotted an issue versus when it went viral. Built as a portfolio/interview prototype.

**Status: Phase 2 of 9.** Phase 1 shipped the repo scaffold, the data model, and a working
Reddit collector that polls a tuned watchlist and writes a time series of post snapshots to
SQLite. Phase 2 adds a Claude-based classifier that labels each collected post as a pain
point (or not) and writes the result to SQLite. Scoring, the human QA gate, clustering, the
dashboard, and the other data sources are future phases — see "How it works" below for the
full roadmap.

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
  Until then, `radar collect` runs and exits cleanly with a warning instead of failing.
- Fill in `ANTHROPIC_API_KEY` once you want to classify collected posts. Until then, `radar
  classify` runs and exits cleanly with a warning instead of failing.

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

## Running the collector

```bash
radar collect
# or: python -m radar.collect
```

This loads `config/search_terms.yaml`, runs a `search_top` (most-engaged) and a
`search_recent` (newest) pass per term against the configured subreddits, and writes one row
per matched post to the `snapshots` table in `data/radar.db`. Re-running it later adds new
snapshot rows for the same posts — that accumulating time series is what later phases use to
compute engagement velocity.

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

## Tests

```bash
pytest
```

The whole suite runs against fixtures under `tests/fixtures/reddit/` and
`tests/fixtures/anthropic/` via `respx`-mocked HTTP — no live Reddit/Anthropic credentials or
network access required.

## How it works (current + planned)

- **Phase 1:** `Source` interface, `RawPost`/`Classification` data models, and a
  `RedditSource` collector writing snapshots to SQLite. See `radar/sources/reddit.py` and
  `radar/collect.py`.
- **Phase 2 (this phase):** Claude-based classifier (`is_pain_point`, `category`, `severity`,
  `model_implicated`, `issue_summary`), calling the Messages API directly via `httpx` (same
  pattern as the Reddit source, reusing `RateLimitedClient`) with a forced tool call for
  structured output. Writes one row per post to the `classifications` table. See
  `radar/classify.py`.
- **Phase 3:** Score + diff + velocity — suppress repeat alerts unless engagement is
  accelerating.
- **Phase 4:** Human QA gate for sensitive categories (`abuse`, `credential_theft`, `safety`)
  before anything could fire an external alert.
- **Phase 5:** Clustering into root-cause groups.
- **Phase 6:** FastAPI + static dashboard.
- **Phase 7:** `search_recent` early-warning pass + lead-time metric (already wired into the
  Phase 1 collector).
- **Phase 8:** Backtest CLI against known past incidents.
- **Phase 9:** YouTube source, then X/Twitter behind a feature flag.

## Data model

- `snapshots` — one row per `(post_id, poll_run_id, search_pass)`. This is the time series:
  post id, platform, hashed author, collected/created timestamps, public metrics, a computed
  `virality_score`, and the raw text (subject to a future retention-purge job). Never a
  per-user profile.
- `classifications` — one row per `post_id` (not a time series; a re-run replaces the prior
  row): `is_pain_point`, `category`, `model_implicated`, `severity`, `issue_summary`, which
  `classifier_model` produced it, and when.

## Project layout

```
radar/
├── models.py         # RawPost, Metrics, Classification + enums
├── config.py          # Settings (pydantic-settings) + search_terms.yaml loader
├── hashing.py          # author hashing
├── virality.py         # virality score formula
├── http_utils.py        # shared rate-limit/backoff HTTP helper
├── db.py                # SQLite schema + snapshot writes
├── sources/
│   ├── base.py           # Source protocol (search_top / search_recent)
│   └── reddit.py          # RedditSource
├── collect.py             # orchestration: python -m radar.collect
├── classify.py             # ClaudeClassifier + orchestration: python -m radar.classify
└── cli.py                  # `radar collect` / `radar classify`
```
