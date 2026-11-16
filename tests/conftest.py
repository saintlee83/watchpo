from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic.types.beta import BetaMessage


def make_message(content: list[dict], stop_reason: str = "tool_use", model: str = "claude-opus-5-5") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000},
        }
    )


class FakeStream:
    def __init__(self, msg: BetaMessage):
        self._msg = msg

    def get_final_message(self) -> BetaMessage:
        return self._msg


class FakeAnthropic:
    """Replays scripted responses and records every request."""

    def __init__(self, responses: list[BetaMessage]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    @contextmanager
    def stream(self, **kwargs: Any):
        # Snapshot messages: the analyzer keeps appending to the same list.
        self.requests.append({**kwargs, "messages": json.loads(json.dumps(kwargs["messages"], default=str))})
        if not self.responses:
            raise AssertionError("no scripted response left")
        yield FakeStream(self.responses.pop(0))


def submit_block(tool_id: str = "tu_submit", suggestions: list[dict] | None = None) -> dict:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": "submit_analysis",
        "input": {
            "summary": "Upstream added connection keep-alive.",
            "suggestions": suggestions
            if suggestions is not None
            else [
                {
                    "title": "Enable HTTP keep-alive in the API client",
                    "category": "performance",
                    "upstream_refs": ["pr#2"],
                    "what_changed": "Upstream reuses connections.",
                    "why_it_matters": "Our client opens a new connection per request.",
                    "recommendation": "Reuse one `Client` in `app/http.py`.",
                    "affected_files": ["app/http.py"],
                    "impact": "high",
                    "effort": "small",
                    "confidence": "medium",
                }
            ],
            "also_noted": [{"ref": "release:v1.0.0", "note": "Major release."}],
        },
    }


@pytest.fixture
def product_repo(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    (root / "app").mkdir(parents=True)
    (root / "README.md").write_text("# Product\n\nA tiny web app that calls third-party APIs.\n")
    (root / "app" / "http.py").write_text("import httpx\n\ndef get(url):\n    return httpx.get(url)\n")
    (root / "pyproject.toml").write_text('[project]\nname = "product"\ndependencies = ["httpx"]\n')
    (root / ".env").write_text("API_KEY=supersecret\n")
    (root / "secrets.json").write_text('{"token": "supersecret"}\n')
    (root / "ignored.log").write_text("noise\n")
    (root / ".gitignore").write_text("*.log\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


# -- fake GitHub ----------------------------------------------------------

RELEASES = [
    {
        "tag_name": "v1.0.0",
        "name": "v1.0.0",
        "html_url": "https://github.com/up/stream/releases/tag/v1.0.0",
        "author": {"login": "maintainer"},
        "published_at": "2026-11-14T10:00:00Z",
        "body": "Big release.",
        "draft": False,
        "prerelease": False,
    },
    {
        "tag_name": "v0.9.0",
        "name": "v0.9.0",
        "html_url": "https://github.com/up/stream/releases/tag/v0.9.0",
        "author": {"login": "maintainer"},
        "published_at": "2026-10-01T10:00:00Z",
        "body": "Old release.",
        "draft": False,
        "prerelease": False,
    },
]


def _pr(n: int, merged: str | None, updated: str, author: str = "dev", labels: tuple[str, ...] = ()) -> dict:
    return {
        "number": n,
        "title": f"PR {n}",
        "html_url": f"https://github.com/up/stream/pull/{n}",
        "user": {"login": author},
        "merged_at": merged,
        "updated_at": updated,
        "body": f"Body of {n}",
        "labels": [{"name": lbl} for lbl in labels],
    }


PULLS_PAGE1 = [
    _pr(1, None, "2026-11-15T00:00:00Z"),  # closed, not merged
    _pr(2, "2026-11-14T00:00:00Z", "2026-11-14T00:00:00Z"),
    _pr(3, "2026-11-13T00:00:00Z", "2026-11-13T00:00:00Z", author="dependabot[bot]"),
]
PULLS_PAGE2 = [
    _pr(4, "2026-11-12T00:00:00Z", "2026-11-12T00:00:00Z", labels=("docs",)),
    _pr(5, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),  # older than window -> stop
]


def github_handler(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if path == "/repos/up/stream/releases":
        return httpx2.Response(200, json=RELEASES)
    if path == "/repos/up/stream/pulls":
        if request.url.params.get("page") == "2":
            return httpx2.Response(200, json=PULLS_PAGE2)
        link = '<https://api.github.com/repos/up/stream/pulls?state=closed&page=2>; rel="next"'
        return httpx2.Response(200, json=PULLS_PAGE1, headers={"link": link})
    if path == "/repos/up/stream/pulls/2":
        return httpx2.Response(200, json={**PULLS_PAGE1[1], "additions": 10, "deletions": 2, "changed_files": 1})
    if path == "/repos/up/stream/pulls/2/files":
        return httpx2.Response(
            200,
            json=[{"filename": "src/client.rs", "status": "modified", "additions": 10, "deletions": 2, "patch": "@@ -1 +1 @@\n-old\n+new"}],
        )
    if path == "/repos/me/product/issues" and request.method == "POST":
        return httpx2.Response(201, json={"html_url": "https://github.com/me/product/issues/1"})
    return httpx2.Response(404, json={"message": "Not Found"})


@pytest.fixture
def gh_transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(github_handler)
