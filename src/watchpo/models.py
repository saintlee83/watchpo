"""Data models shared across watchpo: upstream changes, suggestions, reports."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

ItemKind = Literal["release", "pr", "commit"]


class ChangeItem(BaseModel):
    """One upstream change (a release, a merged PR, or a commit)."""

    id: str  # stable id, e.g. "pr#1234", "release:v2.0.0", "commit:abc1234"
    kind: ItemKind
    repo: str
    title: str
    url: str
    author: str | None = None
    date: datetime
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    number: int | None = None  # PR number
    sha: str | None = None  # commit sha
    tag: str | None = None  # release tag


Category = Literal[
    "feature",
    "performance",
    "reliability",
    "security",
    "dx",
    "ux",
    "architecture",
    "dependency",
    "testing",
    "docs",
]
Level = Literal["high", "medium", "low"]
Effort = Literal["small", "medium", "large"]


class Edit(BaseModel):
    path: str
    before: str = Field(min_length=1)
    after: str


class Suggestion(BaseModel):
    title: str
    category: Category
    upstream_refs: list[str] = Field(
        description="Ids of the upstream items this comes from, e.g. ['pr#123']"
    )
    what_changed: str
    why_it_matters: str
    recommendation: str
    affected_files: list[str] = Field(default_factory=list)
    edits: list[Edit] = Field(default_factory=list)
    impact: Level
    effort: Effort
    confidence: Level


class Note(BaseModel):
    ref: str
    note: str


class AnalysisResult(BaseModel):
    """What Claude submits for one watched repo."""

    summary: str
    suggestions: list[Suggestion] = Field(default_factory=list)
    also_noted: list[Note] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.requests += other.requests


class RepoReport(BaseModel):
    repo: str
    why: str | None = None
    since: datetime
    items: list[ChangeItem] = Field(default_factory=list)
    result: AnalysisResult | None = None
    error: str | None = None
    deferred: int = 0
    served_by: str | None = None  # model that produced the final answer
    usage: Usage = Field(default_factory=Usage)


class RunReport(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    product: str
    started_at: datetime
    finished_at: datetime | None = None
    model: str
    provider: str = "anthropic"
    base_commit: str | None = None
    product_subdir: str | None = None
    repos: list[RepoReport] = Field(default_factory=list)
