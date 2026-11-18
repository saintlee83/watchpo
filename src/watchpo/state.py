"""Per-repo checkpoints so each run only looks at what's new."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .models import ChangeItem

MAX_SEEN = 3000


class RepoState(BaseModel):
    last_run: datetime | None = None
    pending: list[ChangeItem] = Field(default_factory=list)
    seen: list[str] = Field(default_factory=list)


class State(BaseModel):
    version: int = 1
    apply_queue: list[str] = Field(default_factory=list)
    repos: dict[str, RepoState] = Field(default_factory=dict)

    def get(self, repo: str) -> RepoState:
        return self.repos.setdefault(repo.lower(), RepoState())

    def mark(self, repo: str, run_started: datetime | None, item_ids: list[str]) -> None:
        rs = self.get(repo)
        rs.last_run = run_started
        seen = list(dict.fromkeys([*rs.seen, *item_ids]))
        rs.seen = seen[-MAX_SEEN:]


def load_state(path: Path, *, repair: bool = True) -> State:
    if not path.exists():
        return State()
    try:
        return State.model_validate_json(path.read_text())
    except ValueError:
        # Corrupt state shouldn't block a run; start fresh but keep a backup.
        if repair:
            path.rename(path.with_suffix(".corrupt.json"))
        return State()


def save_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state.model_dump(mode="json"), indent=2) + "\n")


def atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".watchpo-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
