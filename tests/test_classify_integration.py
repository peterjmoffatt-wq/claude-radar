from __future__ import annotations

from datetime import datetime, timezone

import httpx
import respx

import radar.classify as classify_module
from radar.classify import API_URL
from radar.db import get_connection, init_db, write_snapshots
from radar.models import Metrics, Platform, RawPost


def _post(post_id: str, text: str = "Getting 529s constantly.") -> RawPost:
    return RawPost(
        id=post_id,
        platform=Platform.REDDIT,
        author="alice123",
        text=text,
        url=f"https://www.reddit.com/r/ClaudeAI/comments/{post_id}/",
        created_at=datetime(2023, 11, 1, tzinfo=timezone.utc),
        metrics=Metrics(likes=0, comments=10, score=50, shares=0),
        subreddit="ClaudeAI",
        matched_term="claude api",
    )


def _seed_snapshots(settings, posts, search_pass="top"):
    conn = get_connection(settings.database_path)
    init_db(conn)
    write_snapshots(conn, posts, poll_run_id="run-1", search_pass=search_pass, settings=settings)
    conn.close()


def _mock_claude(load_anthropic_fixture, fixture="classify_pain_point.json"):
    return respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture(fixture))
    )


@respx.mock
def test_run_classification_writes_expected_rows(settings_factory, load_anthropic_fixture):
    settings = settings_factory()
    _seed_snapshots(settings, [_post("t3_top1"), _post("t3_top2")])
    _mock_claude(load_anthropic_fixture)

    result = classify_module.run_classification(settings, sleep_fn=lambda s: None)

    assert result.skipped is False
    assert result.posts_classified == 2

    conn = get_connection(settings.database_path)
    rows = conn.execute("SELECT post_id, is_pain_point, category FROM classifications").fetchall()
    conn.close()

    assert {row[0] for row in rows} == {"t3_top1", "t3_top2"}
    assert all(row[1] == 1 for row in rows)
    assert all(row[2] == "product_bug" for row in rows)


@respx.mock
def test_dedupes_multiple_snapshots_of_same_post(settings_factory, load_anthropic_fixture):
    settings = settings_factory()
    # Same post collected in both the "top" and "recent" pass -- two snapshot rows,
    # one underlying post -- should only be classified once.
    _seed_snapshots(settings, [_post("t3_top1")], search_pass="top")
    _seed_snapshots(settings, [_post("t3_top1")], search_pass="recent")
    route = _mock_claude(load_anthropic_fixture)

    result = classify_module.run_classification(settings, sleep_fn=lambda s: None)

    assert route.call_count == 1
    assert result.posts_classified == 1


@respx.mock
def test_already_classified_posts_not_resent_on_second_run(settings_factory, load_anthropic_fixture):
    settings = settings_factory()
    _seed_snapshots(settings, [_post("t3_top1")])
    route = _mock_claude(load_anthropic_fixture)

    classify_module.run_classification(settings, sleep_fn=lambda s: None)
    second_result = classify_module.run_classification(settings, sleep_fn=lambda s: None)

    assert route.call_count == 1
    assert second_result.posts_classified == 0


@respx.mock
def test_one_bad_response_does_not_block_the_rest_of_the_batch(
    settings_factory, load_anthropic_fixture
):
    settings = settings_factory()
    _seed_snapshots(settings, [_post("t3_bad"), _post("t3_good")])
    respx.post(API_URL).mock(
        side_effect=[
            httpx.Response(200, json=load_anthropic_fixture("classify_no_tool_use.json")),
            httpx.Response(200, json=load_anthropic_fixture("classify_pain_point.json")),
        ]
    )

    result = classify_module.run_classification(settings, sleep_fn=lambda s: None)

    assert result.posts_classified == 1
    conn = get_connection(settings.database_path)
    row = conn.execute("SELECT post_id FROM classifications").fetchone()
    conn.close()
    assert row[0] in {"t3_bad", "t3_good"}  # whichever post the mock served the good response to


@respx.mock
def test_gives_up_after_max_classify_attempts_and_writes_sentinel(settings_factory, load_anthropic_fixture):
    settings = settings_factory()
    _seed_snapshots(settings, [_post("t3_bad")])
    # 3 consecutive bad responses (no tool_use block) -- one per run_classification() call.
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=load_anthropic_fixture("classify_no_tool_use.json"))
    )

    results = [
        classify_module.run_classification(settings, sleep_fn=lambda s: None)
        for _ in range(classify_module.MAX_CLASSIFY_ATTEMPTS)
    ]
    # Every attempt but the last writes nothing (still under the cap); the
    # final attempt crosses MAX_CLASSIFY_ATTEMPTS and writes the sentinel row.
    assert [r.posts_classified for r in results] == [0, 0, 1]

    # A sentinel row should now exist.
    conn = get_connection(settings.database_path)
    row = conn.execute(
        "SELECT is_pain_point, classifier_model FROM classifications WHERE post_id = 't3_bad'"
    ).fetchone()
    conn.close()
    assert row == (0, "failed")

    # A later run must not pick the now-classified post back up.
    classify_module.run_classification(settings, sleep_fn=lambda s: None)
    assert route.call_count == classify_module.MAX_CLASSIFY_ATTEMPTS


@respx.mock
def test_recovers_after_transient_failure_not_marked_failed(settings_factory, load_anthropic_fixture):
    settings = settings_factory()
    _seed_snapshots(settings, [_post("t3_flaky")])
    respx.post(API_URL).mock(
        side_effect=[
            httpx.Response(200, json=load_anthropic_fixture("classify_no_tool_use.json")),
            httpx.Response(200, json=load_anthropic_fixture("classify_pain_point.json")),
        ]
    )

    first = classify_module.run_classification(settings, sleep_fn=lambda s: None)
    assert first.posts_classified == 0
    second = classify_module.run_classification(settings, sleep_fn=lambda s: None)
    assert second.posts_classified == 1

    conn = get_connection(settings.database_path)
    row = conn.execute(
        "SELECT is_pain_point, classifier_model FROM classifications WHERE post_id = 't3_flaky'"
    ).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] != "failed"


@respx.mock
def test_run_classification_noop_when_credentials_missing(settings_factory):
    settings = settings_factory(anthropic_api_key="")

    result = classify_module.run_classification(settings, sleep_fn=lambda s: None)

    assert result.skipped is True
    assert result.posts_classified == 0
    assert not settings.database_path.exists()
