"""Loading, validating, and saving watchpo.yml."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

CONFIG_NAMES = ("watchpo.yml", "watchpo.yaml", ".watchpo.yml")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Track = Literal["releases", "pulls", "commits"]


class ConfigError(Exception):
    pass


class Product(BaseModel):
    name: str
    description: str = ""
    path: str = "."
    focus: list[str] = Field(default_factory=list)

    @field_validator("description", mode="before")
    @classmethod
    def _none_is_blank(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("focus", mode="before")
    @classmethod
    def _none_is_empty(cls, v: object) -> object:
        return [] if v is None else v


class WatchedRepo(BaseModel):
    repo: str
    why: str | None = None
    track: list[Track] = Field(default_factory=lambda: ["releases", "pulls"])
    branch: str | None = None  # for commits; default branch when unset
    paths: list[str] = Field(default_factory=list)  # for commits: only these paths
    include_labels: list[str] = Field(default_factory=list)  # for PRs
    exclude_labels: list[str] = Field(default_factory=list)  # for PRs

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        v = normalize_repo(v)
        if not REPO_RE.match(v):
            raise ValueError(f"expected 'owner/name', got {v!r}")
        return v


class Settings(BaseModel):
    lookback_days: int = 14
    max_items_per_repo: int = 50
    exclude_authors: list[str] = Field(
        default_factory=lambda: [
            "dependabot[bot]",
            "renovate[bot]",
            "github-actions[bot]",
            "pre-commit-ci[bot]",
        ]
    )


class ClaudeSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_turns: int = 30
    # Server-side refusal fallbacks: "default" lets the API pick a substitute
    # model if the request is declined by a safety classifier; "off" disables.
    fallbacks: Literal["default", "off"] = "default"
    # Let Claude read and search your codebase so suggestions name real files.
    explore_codebase: bool = True


class ReportSettings(BaseModel):
    dir: str = ".watchpo/reports"
    issue_repo: str | None = None  # defaults to $GITHUB_REPOSITORY
    issue_labels: list[str] = Field(default_factory=lambda: ["watchpo"])


class Config(BaseModel):
    product: Product
    watch: list[WatchedRepo] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    state_file: str = ".watchpo/state.json"

    @field_validator("watch", mode="before")
    @classmethod
    def _none_is_empty(cls, v: object) -> object:
        return [] if v is None else v

    # Not part of the file: where the config was loaded from.
    _root: Path = Path(".")

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else (self._root / p).resolve()

    def find(self, repo: str) -> WatchedRepo | None:
        repo = normalize_repo(repo).lower()
        return next((w for w in self.watch if w.repo.lower() == repo), None)


def normalize_repo(value: str) -> str:
    """Accept 'owner/name', GitHub URLs, and '.git' suffixes."""
    v = value.strip()
    v = re.sub(r"^(https?://)?(www\.)?github\.com/", "", v)
    v = re.sub(r"^git@github\.com:", "", v)
    v = v.removesuffix(".git").strip("/")
    parts = v.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else v


def find_config(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        for name in CONFIG_NAMES:
            if (d / name).is_file():
                return d / name
    return None


def load_config(path: Path | None = None) -> tuple[Config, Path]:
    cfg_path = path or find_config()
    if cfg_path is None:
        raise ConfigError("No watchpo.yml found. Run `watchpo init` first.")
    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{cfg_path}: invalid YAML: {e}") from e
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{cfg_path}: {e}") from e
    cfg._root = cfg_path.parent.resolve()
    return cfg, cfg_path


def save_config(cfg: Config, path: Path) -> None:
    data = cfg.model_dump(mode="json", exclude_defaults=False)
    # Keep the file compact: drop per-repo fields left at their defaults.
    default_watch = WatchedRepo(repo="a/b").model_dump(mode="json")
    data["watch"] = [
        {k: v for k, v in w.items() if k == "repo" or v != default_watch.get(k)}
        for w in data["watch"]
    ]
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


HEADER = "# watchpo config. Docs: https://github.com/saintlee83/watchpo#configuration\n"

TEMPLATE = """\
# watchpo config. Docs: https://github.com/saintlee83/watchpo#configuration

product:
  name: {name}
  # Tell Claude what your product is, who uses it, and what you care about.
  # The better this is, the more relevant the suggestions.
  description: |
    {description}
  path: .          # your product's codebase (Claude reads it to ground suggestions)
  focus: []        # optional, e.g. [performance, security, developer experience]

# Open-source repos to learn from: dependencies, competitors, or inspiration.
watch:
{watch}

settings:
  lookback_days: 14        # window for the first run (later runs pick up where they left off)
  max_items_per_repo: 50   # most recent N changes per repo are analyzed
  exclude_authors: ["dependabot[bot]", "renovate[bot]", "github-actions[bot]", "pre-commit-ci[bot]"]

claude:
  model: claude-opus-5-5
  effort: high             # low | medium | high | xhigh | max
  max_turns: 30            # tool-use turns per repo
  fallbacks: default       # retry on a substitute model if a request is declined; "off" to disable
  explore_codebase: true   # let Claude read/search your code (read-only, git-tracked files only)

report:
  dir: .watchpo/reports
  issue_repo: null         # owner/name to open digest issues in (defaults to $GITHUB_REPOSITORY)
  issue_labels: [watchpo]

state_file: .watchpo/state.json
"""

WATCH_EXAMPLE = """\
  # - repo: owner/name
  #   why: "Why you watch it; what you hope to learn"
  #   track: [releases, pulls]      # any of: releases, pulls, commits
  #   exclude_labels: [docs]"""


def render_template(name: str, description: str, repos: list[str]) -> str:
    if repos:
        watch = "\n".join(
            f"  - repo: {normalize_repo(r)}\n    why: null\n    track: [releases, pulls]"
            for r in repos
        )
    else:
        watch = WATCH_EXAMPLE
    desc = description.strip() or "TODO: one paragraph about your product."
    desc = desc.replace("\n", "\n    ")
    return TEMPLATE.format(name=json.dumps(name), description=desc, watch=watch)
