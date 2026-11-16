"""Per-repo checkpoints so each run only looks at what's new."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

MAX_SEEN = 3000


class RepoState(BaseModel):
    last_run: datetime | None = None
    seen: list[str] = Field(default_factory=list)


class State(BaseModel):
    version: int = 1
    repos: dict[str, RepoState] = Field(default_factory=dict)

    def get(self, repo: str) -> RepoState:
        return self.repos.setdefault(repo.lower(), RepoState())

    def mark(self, repo: str, run_started: datetime, item_ids: list[str]) -> None:
        rs = self.get(repo)
        rs.last_run = run_started
        seen = list(dict.fromkeys([*rs.seen, *item_ids]))
        rs.seen = seen[-MAX_SEEN:]


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        return State.model_validate_json(path.read_text())
    except ValueError:
        # Corrupt state shouldn't block a run; start fresh but keep a backup.
        path.rename(path.with_suffix(".corrupt.json"))
        return State()


def save_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state.model_dump(mode="json"), indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(data + "\n")
    os.replace(tmp, path)
