from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from watchpo.config import Config, Settings, WatchedRepo, load_config, normalize_repo, render_template
from watchpo.github import GitHub, filter_items
from watchpo.models import ChangeItem
from watchpo.profile import Codebase, build_profile
from watchpo.runner import parse_since
from watchpo.state import State, load_state, save_state

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,want",
    [
        ("vercel/next.js", "vercel/next.js"),
        ("https://github.com/vercel/next.js", "vercel/next.js"),
        ("https://github.com/vercel/next.js/tree/canary/packages", "vercel/next.js"),
        ("git@github.com:astral-sh/uv.git", "astral-sh/uv"),
    ],
)
def test_normalize_repo(raw: str, want: str) -> None:
    assert normalize_repo(raw) == want


def test_invalid_repo_rejected() -> None:
    with pytest.raises(ValueError):
        WatchedRepo(repo="not-a-repo")


def test_template_loads(tmp_path: Path) -> None:
    path = tmp_path / "watchpo.yml"
    path.write_text(render_template("my app", "Line one.\nLine two.", []))
    cfg, _ = load_config(path)
    assert cfg.product.name == "my app"
    assert cfg.watch == []  # commented-out example parses as an empty list
    assert "Line two." in cfg.product.description
    path.write_text(render_template("x", "", ["https://github.com/a/b"]))
    cfg, _ = load_config(path)
    assert [w.repo for w in cfg.watch] == ["a/b"]
    assert cfg.root == tmp_path.resolve()


def test_parse_since() -> None:
    assert parse_since("7d", NOW) == datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert parse_since("48h", NOW) == datetime(2026, 11, 14, tzinfo=timezone.utc)
    assert parse_since("2w", NOW) == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert parse_since("2026-11-01", NOW) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_since("last tuesday", NOW)


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "s" / "state.json"
    st = State()
    st.mark("Up/Stream", NOW, ["pr#1", "pr#2"])
    st.mark("up/stream", NOW, ["pr#2", "pr#3"])
    save_state(st, path)
    back = load_state(path)
    assert back.get("up/stream").seen == ["pr#1", "pr#2", "pr#3"]
    assert back.get("up/stream").last_run == NOW
    path.write_text("{not json")
    assert load_state(path).repos == {}  # corrupt state doesn't crash
    assert path.with_suffix(".corrupt.json").exists()


# -- codebase sandbox -----------------------------------------------------


def test_codebase_hides_secrets_and_ignored(product_repo: Path) -> None:
    code = Codebase(product_repo)
    assert "app/http.py" in code.files
    assert ".env" not in code.files
    assert "secrets.json" not in code.files
    assert "ignored.log" not in code.files
    assert "supersecret" not in code.search("supersecret")
    assert code.read_file(".env").startswith("Error")


def test_codebase_blocks_traversal(product_repo: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("private")
    code = Codebase(product_repo)
    for p in ("../outside.txt", "/etc/passwd", str(tmp_path / "outside.txt"), "app/../../outside.txt"):
        assert code.read_file(p).startswith("Error"), p


def test_codebase_read_and_search(product_repo: Path) -> None:
    code = Codebase(product_repo)
    out = code.read_file("./app/http.py", start_line=3, max_lines=1)
    assert "3  def get(url):" in out and "more lines" in out
    assert "app/http.py:4:" in code.search(r"httpx\.get")
    assert code.search("(").startswith("Error")
    assert code.list_files("*.py") == "app/http.py"


def test_profile_includes_readme_and_manifests(product_repo: Path) -> None:
    cfg = Config.model_validate({"product": {"name": "P", "description": "desc", "focus": ["perf"]}})
    text = build_profile(cfg.product, Codebase(product_repo))
    assert "A tiny web app" in text
    assert "pyproject.toml" in text and 'dependencies = ["httpx"]' in text
    assert "Focus areas" in text
    assert "supersecret" not in text


# -- GitHub fetching ------------------------------------------------------


def test_fetch_changes(gh_transport) -> None:
    gh = GitHub("tok", transport=gh_transport)
    w = WatchedRepo(repo="up/stream", exclude_labels=["docs"])
    since = datetime(2026, 11, 2, tzinfo=timezone.utc)
    items = gh.changes(w, since, Settings(), seen=set())
    # release v1.0.0 in window; PR 1 unmerged, PR 3 bot, PR 4 docs-label, PR 5 too old.
    assert [i.id for i in items] == ["release:v1.0.0", "pr#2"]
    items = gh.changes(w, since, Settings(), seen={"pr#2"})
    assert [i.id for i in items] == ["release:v1.0.0"]
    detail = gh.detail(next(i for i in gh.merged_pulls(w, since) if i.id == "pr#2"))
    assert "src/client.rs" in detail and "+new" in detail


def test_filter_include_labels_and_cap() -> None:
    def pr(n: int, labels: list[str]) -> ChangeItem:
        return ChangeItem(id=f"pr#{n}", kind="pr", repo="a/b", title="t", url="u", date=NOW, labels=labels)

    items = [pr(1, ["perf"]), pr(2, []), pr(3, ["Perf", "x"])]
    w = WatchedRepo(repo="a/b", include_labels=["perf"])
    assert [i.id for i in filter_items(items, w, Settings(), set())] == ["pr#1", "pr#3"]
    assert len(filter_items(items, WatchedRepo(repo="a/b"), Settings(max_items_per_repo=2), set())) == 2


def test_saved_config_is_compact(tmp_path: Path) -> None:
    from watchpo.config import save_config

    path = tmp_path / "watchpo.yml"
    cfg = Config.model_validate({"product": {"name": "P"}, "watch": [{"repo": "a/b", "why": "x"}]})
    save_config(cfg, path)
    data = yaml.safe_load(path.read_text())
    assert data["watch"] == [{"repo": "a/b", "why": "x"}]
    assert load_config(path)[0].watch[0].track == ["releases", "pulls"]
