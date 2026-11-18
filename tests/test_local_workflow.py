from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from filelock import FileLock

import watchpo.cli as cli
import watchpo.runner as runner
from watchpo.branches import BranchError, branch_name
from watchpo.config import AISettings, load_config
from watchpo.github import GitHub
from watchpo.models import RunReport
from watchpo.profile import Codebase
from watchpo.state import load_state

from .conftest import FakeAnthropic, make_message, submit_block


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Tests", "-c", "user.email=tests@localhost", *args], cwd=root, text=True,
    ).strip()


@pytest.fixture
def project(product_repo, monkeypatch, gh_transport):
    monkeypatch.chdir(product_repo)
    monkeypatch.setattr(runner, "GitHub", lambda token: GitHub(token, transport=gh_transport))
    monkeypatch.setattr(runner, "resolve_token", lambda: None)
    monkeypatch.setattr(runner, "utcnow", lambda: datetime(2026, 11, 16, tzinfo=timezone.utc))
    cfg = product_repo / "watchpo.yml"
    cfg.write_text("product: {name: demo}\nwatch: [{repo: up/stream}]\n")
    return product_repo


def empty_message():
    block = submit_block(suggestions=[])
    block["input"]["also_noted"] = []
    return make_message([block])


def test_overflow_and_report_failure(project, monkeypatch):
    with (project / "watchpo.yml").open("a") as stream:
        stream.write("settings: {max_items_per_repo: 1}\n")
    fake = FakeAnthropic([empty_message() for _ in range(4)])
    monkeypatch.setattr(runner, "make_client", lambda: fake)
    state = project / ".watchpo/state.json"
    with monkeypatch.context() as m:
        def fail(*args):
            raise OSError("full disk")
        m.setattr(runner, "write_reports", fail)
        assert cli.main(["watch", "--once"]) == 1
        checkpoint = load_state(state).get("up/stream")
        assert checkpoint.seen == [] and len(checkpoint.pending) == 3
    assert cli.main(["watch", "--once"]) == 0
    first = load_state(state).get("up/stream")
    assert first.seen == ["release:v1.0.0"]
    assert [item.id for item in first.pending] == ["pr#2", "pr#4"]
    assert cli.main(["fetch"]) == 0
    assert load_state(state).get("up/stream") == first
    for _ in range(3):
        assert cli.main(["watch", "--once"]) == 0
    assert load_state(state).get("up/stream").seen == ["release:v1.0.0", "pr#2", "pr#4"]
    assert len(fake.requests) == 4
    assert len(list((project / ".watchpo/reports").glob("*.json"))) == 3


def test_local_endpoint_branch_recovery(project, monkeypatch):
    git(project, "commit", "-qm", "base")
    base = git(project, "rev-parse", "HEAD")
    (project / "README.md").write_text("a staged local change")
    git(project, "add", "README.md")
    (project / "README.md").write_text("another unstaged change")

    calls = []
    edit = {"path": "app/http.py", "before": "httpx.get(url)", "after": "httpx.get(url, timeout=10)"}
    submit = submit_block()["input"]
    submit["suggestions"][0]["edits"] = [edit]
    tool_sequence = [
        ("read_file", {"path": "app/http.py"}),
        ("submit_analysis", submit),
    ]

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), body))
            name, args = tool_sequence[len(calls) - 1]
            payload = {"model": "local-model", "usage": {"prompt_tokens": 24, "completion_tokens": 10},
                       "choices": [{"finish_reason": "tool_calls", "message": {
                           "role": "assistant", "content": None, "tool_calls": [{"type": "function",
                               "id": "tool-1", "function": {"name": name, "arguments": json.dumps(args)}}],
                       }}]}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOCAL_MODEL_TOKEN", "test-token")
    monkeypatch.setattr(runner, "make_client", lambda: pytest.fail("unexpected Anthropic call"))
    with (project / "watchpo.yml").open("a") as stream:
        stream.write(
            "ai:\n  provider: openai-compatible\n  model: local-model\n"
            f"  base_url: http://127.0.0.1:{server.server_port}/v1\n  api_key_env: LOCAL_MODEL_TOKEN\n"
            "workflow: {mode: branch, interval_seconds: 30}\nreport: {dir: output/reports}\n"
        )
    try:
        with monkeypatch.context() as m:
            def busy(*args):
                raise BranchError("repository temporarily unavailable")
            m.setattr(cli, "apply_report", busy)
            assert cli.main(["watch", "--once"]) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert len(calls) == 2
    assert calls[0][:2] == ("/v1/chat/completions", "Bearer test-token")
    second = calls[1][2]
    assert second["messages"][-1]["role"] == "tool"
    assert "return httpx.get(url)" in second["messages"][-1]["content"]
    assert "input_schema" not in second["tools"][0]["function"]
    state = project / ".watchpo/state.json"
    saved = load_state(state)
    assert len(saved.apply_queue) == 1
    source = project / saved.apply_queue[0]
    report = RunReport.model_validate_json(source.read_bytes())
    assert report.base_commit == base
    assert report.repos[0].usage.requests == 2

    # The endpoint is offline while the restarted watcher picks up the saved edits.
    assert cli.main(["watch", "--once"]) == 0
    assert load_state(state).apply_queue == []
    branch = branch_name(report, report.repos[0])
    commit = git(project, "rev-parse", branch)
    assert git(project, "rev-parse", branch + "^") == base
    assert edit["after"] in git(project, "show", branch + ":app/http.py")
    assert git(project, "rev-parse", "HEAD") == base
    assert git(project, "show", ":README.md") == "a staged local change"
    assert (project / "README.md").read_text() == "another unstaged change"
    assert cli.main(["apply", str(source)]) == 0
    assert git(project, "rev-parse", branch) == commit


def test_bad_edits_and_refs_are_correctable(project, monkeypatch):
    git(project, "commit", "-qm", "base")
    missing = submit_block()
    missing["input"]["suggestions"][0]["upstream_refs"] = ["pr#999"]
    invalid = submit_block()
    invalid["input"]["suggestions"][0]["edits"] = [
        {"path": "app/http.py", "before": "missing line", "after": "new line"}
    ]
    fake = FakeAnthropic([make_message([missing]), make_message([invalid]), empty_message()])
    monkeypatch.setattr(runner, "make_client", lambda: fake)
    assert cli.main(["run", "--apply"]) == 0
    assert "Unknown upstream references" in fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert "exact match" in fake.requests[2]["messages"][-1]["content"][0]["content"]
    assert git(project, "branch", "--list", "watchpo/*") == ""


def test_poll_interval_reload_and_overlap(project, monkeypatch):
    cfg = project / "watchpo.yml"
    cfg.write_text("product: {name: idle}\nworkflow: {interval_seconds: 8}\n")
    delays = []

    def wait(event, seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise KeyboardInterrupt
        cfg.write_text("product: {name: idle}\nworkflow: {interval_seconds: 12}\n")

    monkeypatch.setattr(cli.threading.Event, "wait", wait)
    assert cli.main(["watch"]) == 130
    assert delays == [8, 12]
    assert not (project / ".watchpo/reports").exists()
    with FileLock(str(project / ".watchpo/state.json.lock")):
        assert cli.main(["watch", "--once"]) == 1
        assert cli.main(["run"]) == 1


def test_config_and_visible_files(project, tmp_path):
    cfg = project / "watchpo.yml"
    cfg.write_text("product: {name: legacy}\nclaude: {fallbacks: off, model: custom}\n")
    assert load_config(cfg)[0].ai.model == "custom"
    assert load_config(cfg)[0].ai.fallbacks == "off"
    with pytest.raises(ValueError, match="ai.model"):
        AISettings(provider="openai-compatible")
    (project / "alias.txt").symlink_to(project / "secrets.json")
    assert Codebase(project).read_text("alias.txt") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("local only")
    assert Codebase(plain).files == []


def test_fetch_failure_does_not_advance_discovery(project, monkeypatch):
    from watchpo.github import GitHubError

    with (project / "watchpo.yml").open("a") as stream:
        stream.write("settings: {max_items_per_repo: 1}\n")
    fake = FakeAnthropic([empty_message(), empty_message()])
    monkeypatch.setattr(runner, "make_client", lambda: fake)
    assert cli.main(["run"]) == 0
    source = project / ".watchpo/state.json"
    previous = load_state(source).get("up/stream")
    with monkeypatch.context() as m:
        def offline(*args, **kw):
            raise GitHubError("offline")
        m.setattr(GitHub, "changes", offline)
        m.setattr(runner, "utcnow", lambda: datetime(2026, 11, 18, tzinfo=timezone.utc))
        assert cli.main(["fetch"]) == 1
        assert load_state(source).get("up/stream") == previous
        assert cli.main(["watch", "--once"]) == 1
    current = load_state(source).get("up/stream")
    assert current.last_run == previous.last_run
    assert current.seen == ["release:v1.0.0", "pr#2"]
    assert len(current.pending) == 1


def test_custom_action_outputs_and_publish_saved_report(project, monkeypatch, gh_transport):
    with (project / "watchpo.yml").open("a") as stream:
        stream.write("state_file: local/s.json\nreport: {dir: custom/reports, issue_repo: me/product}\n")
    monkeypatch.setattr(runner, "make_client", lambda: FakeAnthropic([empty_message()]))
    output = project / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cli.main(["run"]) == 0
    files = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert Path(files["report_md"]).parent == project / "custom/reports"
    assert (project / "local/s.json").is_file()
    monkeypatch.setattr(cli, "GitHub", lambda token: GitHub(token, transport=gh_transport))
    monkeypatch.setattr(cli, "resolve_token", lambda: None)
    assert cli.main(["publish", files["report_json"]]) == 0
