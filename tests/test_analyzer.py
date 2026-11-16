from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import watchpo.runner as runner_mod
from watchpo.analyzer import AnalysisError, Analyzer, echo_blocks
from watchpo.config import ClaudeSettings, WatchedRepo, load_config
from watchpo.github import GitHub
from watchpo.models import ChangeItem, RunReport
from watchpo.profile import Codebase
from watchpo.report import ISSUE_LIMIT, issue_body, render_markdown
from watchpo.runner import RunOptions, run
from watchpo.state import load_state

from .conftest import FakeAnthropic, make_message, submit_block

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
ITEMS = [
    ChangeItem(id="release:v1.0.0", kind="release", repo="up/stream", title="v1.0.0",
               url="https://github.com/up/stream/releases/tag/v1.0.0", date=NOW, body="Big", tag="v1.0.0"),
    ChangeItem(id="pr#2", kind="pr", repo="up/stream", title="Reuse connections",
               url="https://github.com/up/stream/pull/2", date=NOW, body="Keep-alive", number=2),
]


def make_analyzer(fake: FakeAnthropic, gh_transport, product_repo: Path, **settings) -> Analyzer:
    return Analyzer(
        fake, ClaudeSettings(**settings), "PROFILE", GitHub("t", transport=gh_transport),
        Codebase(product_repo), log=lambda _: None,
    )


def test_tool_loop_then_submit(gh_transport, product_repo: Path) -> None:
    fake = FakeAnthropic([
        make_message([
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "tu_1", "name": "get_upstream_change", "input": {"id": "pr#2"}},
            {"type": "tool_use", "id": "tu_2", "name": "search_code", "input": {"pattern": "httpx"}},
            {"type": "tool_use", "id": "tu_3", "name": "read_file", "input": {"path": "../../etc/passwd"}},
        ]),
        make_message([submit_block()]),
    ])
    result, usage, served = make_analyzer(fake, gh_transport, product_repo).analyze(WatchedRepo(repo="up/stream"), ITEMS)

    assert result.suggestions[0].affected_files == ["app/http.py"]
    assert usage.requests == 2 and usage.cache_read_input_tokens == 2000
    assert served == "claude-opus-5-5"

    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "PROFILE" in first["system"][0]["text"]
    assert all(t.get("eager_input_streaming") for t in first["tools"])
    assert '<change id="pr#2"' in first["messages"][0]["content"]

    # Second request: assistant turn echoed as-is, then all three results in one user turn.
    msgs = fake.requests[1]["messages"]
    assert [b["type"] for b in msgs[1]["content"]] == ["thinking", "tool_use", "tool_use", "tool_use"]
    results = msgs[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2", "tu_3"]
    assert "src/client.rs" in results[0]["content"]
    assert "app/http.py:1:" in results[1]["content"]
    assert results[2]["is_error"] is True


def test_invalid_submission_is_returned_as_error(gh_transport, product_repo: Path) -> None:
    bad = submit_block(suggestions=[{"title": "missing fields"}])
    fake = FakeAnthropic([make_message([bad]), make_message([submit_block(suggestions=[])])])
    result, _, _ = make_analyzer(fake, gh_transport, product_repo).analyze(WatchedRepo(repo="up/stream"), ITEMS)
    assert result.suggestions == []
    err = fake.requests[1]["messages"][2]["content"][0]
    assert err["is_error"] and "Invalid submission" in err["content"]


def test_nudges_when_model_stops_without_submitting(gh_transport, product_repo: Path) -> None:
    fake = FakeAnthropic([
        make_message([{"type": "text", "text": "Done."}], stop_reason="end_turn"),
        make_message([submit_block()]),
    ])
    make_analyzer(fake, gh_transport, product_repo).analyze(WatchedRepo(repo="up/stream"), ITEMS)
    assert "submit_analysis" in fake.requests[1]["messages"][-1]["content"]


def test_refusal_raises(gh_transport, product_repo: Path) -> None:
    fake = FakeAnthropic([make_message([], stop_reason="refusal")])
    with pytest.raises(AnalysisError, match="declined"):
        make_analyzer(fake, gh_transport, product_repo).analyze(WatchedRepo(repo="up/stream"), ITEMS)


def test_turn_budget(gh_transport, product_repo: Path) -> None:
    loop = [{"type": "tool_use", "id": "tu", "name": "list_files", "input": {}}]
    fake = FakeAnthropic([make_message(loop) for _ in range(3)])
    with pytest.raises(AnalysisError, match="3 turns"):
        make_analyzer(fake, gh_transport, product_repo, max_turns=3).analyze(WatchedRepo(repo="up/stream"), ITEMS)


def test_fallbacks_off(gh_transport, product_repo: Path) -> None:
    fake = FakeAnthropic([make_message([submit_block()])])
    make_analyzer(fake, gh_transport, product_repo, fallbacks="off", explore_codebase=False).analyze(
        WatchedRepo(repo="up/stream"), ITEMS
    )
    req = fake.requests[0]
    assert "fallbacks" not in req and "betas" not in req
    assert {t["name"] for t in req["tools"]} == {"get_upstream_change", "submit_analysis"}


def test_echo_after_fallback_drops_pre_boundary_model_blocks() -> None:
    msg = make_message([
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "old", "name": "list_files", "input": {}},
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
         "trigger": {"type": "refusal"}},
        {"type": "thinking", "thinking": "", "signature": "b"},
        {"type": "tool_use", "id": "new", "name": "list_files", "input": {}},
    ], model="claude-opus-5")
    echoed, calls = echo_blocks(msg.content)
    assert [b["type"] for b in echoed] == ["text", "fallback", "thinking", "tool_use"]
    assert echoed[1]["from"] == {"model": "claude-opus-5-5"}
    assert [c.id for c in calls] == ["new"]


def test_end_to_end_run(monkeypatch, gh_transport, product_repo: Path) -> None:
    (product_repo / "watchpo.yml").write_text(
        "product: {name: Product, description: A web app}\n"
        "watch: [{repo: up/stream, why: same domain}]\n"
        "report: {issue_repo: me/product}\n"
    )
    fake = FakeAnthropic([make_message([submit_block()])])
    monkeypatch.setattr(runner_mod, "make_client", lambda: fake)
    monkeypatch.setattr(runner_mod, "GitHub", lambda token: GitHub(token, transport=gh_transport))
    monkeypatch.setattr(runner_mod, "resolve_token", lambda: "tok")
    cfg, _ = load_config(product_repo / "watchpo.yml")
    report = run(cfg, RunOptions(since=datetime(2026, 11, 2, tzinfo=timezone.utc)))
    rr = report.repos[0]
    assert rr.error is None and len(rr.result.suggestions) == 1
    state = load_state(product_repo / ".watchpo" / "state.json")
    # No exclude_labels here, so the docs-labelled PR #4 is analyzed too.
    assert set(state.get("up/stream").seen) == {"release:v1.0.0", "pr#2", "pr#4"}

    md = render_markdown(report)
    assert "| S1 | Enable HTTP keep-alive in the API client | up/stream | high | small | medium |" in md
    assert "[pr#2](https://github.com/up/stream/pull/2)" in md
    assert "**Files:** `app/http.py`" in md

    # Same window again: everything was already seen, so no Claude call at all.
    fake.responses.clear()
    report2 = run(cfg, RunOptions(since=datetime(2026, 11, 2, tzinfo=timezone.utc)))
    assert report2.repos[0].items == [] and len(fake.requests) == 1

    # --rescan brings them back.
    report3 = run(cfg, RunOptions(since=datetime(2026, 11, 2, tzinfo=timezone.utc), rescan=True, analyze=False))
    assert len(report3.repos[0].items) == 3


def test_issue_body_fits_limit() -> None:
    from watchpo.models import AnalysisResult, RepoReport, Suggestion

    big = Suggestion(title="t", category="dx", upstream_refs=["pr#2"], what_changed="x" * 5000,
                     why_it_matters="y", recommendation="z" * 20000, impact="low", effort="small", confidence="low")
    rr = RepoReport(repo="up/stream", since=NOW, items=ITEMS,
                    result=AnalysisResult(summary="s", suggestions=[big] * 6))
    run_report = RunReport(product="P", started_at=NOW, model="claude-opus-5-5", repos=[rr])
    body = issue_body(run_report)
    assert len(body) <= ISSUE_LIMIT
    assert "trimmed" in body
