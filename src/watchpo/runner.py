"""Orchestrates a watchpo run: fetch → analyze → report → checkpoint."""

from __future__ import annotations

import os
import re
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anthropic

from .analyzer import AnalysisError, Analyzer, ToolAnalyzer, make_client
from .branches import current_commit, git_location, has_edits
from .compatible import ChatAnalyzer, chat_client
from .config import Config, ConfigError, WatchedRepo
from .github import GitHub, GitHubError, resolve_token, utcnow
from .models import RepoReport, RunReport
from .profile import Codebase, build_profile
from .report import write_reports
from .state import State, load_state, save_state

NO_CREDS = "No usable Claude credentials. Set ANTHROPIC_API_KEY (or run `ant auth login`) and try again."
REL_RE = re.compile(r"^(\d+)\s*([hdw])$")


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """'7d', '48h', '2w', or an ISO date/datetime."""
    now = now or utcnow()
    v = value.strip().lower()
    if m := REL_RE.match(v):
        n, unit = int(m.group(1)), m.group(2)
        return now - {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"can't parse --since {value!r}; use e.g. 7d, 48h, 2w, or 2026-11-01") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class RunOptions:
    repos: list[str] | None = None  # limit to these
    since: datetime | None = None  # override the window start
    rescan: bool = False  # re-analyze changes already seen in earlier runs
    analyze: bool = True  # False = fetch only (no model calls)
    update_state: bool = True
    apply: bool = False
    skip_empty_report: bool = False


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def selected(cfg: Config, names: list[str] | None) -> list[WatchedRepo]:
    if not names:
        names = [w.repo for w in cfg.watch]
    out = []
    for n in names:
        w = cfg.find(n)
        if w is None:
            raise ValueError(f"{n} is not in watchpo.yml (add it with `watchpo add {n}`)")
        if w not in out:
            out.append(w)
    return out


def window_start(cfg: Config, state: State, w: WatchedRepo, opts: RunOptions, now: datetime) -> datetime:
    if opts.since:
        return opts.since
    last = state.get(w.repo).last_run
    return last if last else now - timedelta(days=cfg.settings.lookback_days)


def _discover(cfg: Config, opts: RunOptions, state: State, gh: GitHub, report: RunReport) -> list[tuple[WatchedRepo, RepoReport]]:
    batches = []
    for w in selected(cfg, opts.repos):
        since = window_start(cfg, state, w, opts, report.started_at)
        checkpoint = state.get(w.repo)
        rr = RepoReport(repo=w.repo, why=w.why, since=since)
        report.repos.append(rr)
        seen = set() if opts.rescan else set(checkpoint.seen)
        seen.update(i.id for i in checkpoint.pending)
        try:
            log(f"• {w.repo}: fetching {', '.join(w.track)} since {since.date().isoformat()}")
            fresh = gh.changes(w, since, cfg.settings, seen, limit=False)
        except GitHubError as e:
            rr.error = f"Upstream fetch failed: {e}"
            log(f"  ✗ {e}")
        else:
            checkpoint.pending.extend(fresh)
            checkpoint.last_run = report.started_at
        cap = cfg.settings.max_items_per_repo
        rr.items = checkpoint.pending[:cap]
        rr.deferred = len(checkpoint.pending) - len(rr.items)
        log(f"  {len(rr.items)} change(s) in batch, {rr.deferred} deferred")
        if rr.items:
            batches.append((w, rr))
    return batches


def _open_analyzer(cfg: Config, stack: ExitStack, gh: GitHub, report: RunReport, apply: bool) -> ToolAnalyzer:
    root = cfg.resolve(cfg.product.path)
    code = Codebase(root) if root.is_dir() else None
    if apply:
        if not cfg.ai.explore_codebase:
            raise ConfigError("Branch mode requires ai.explore_codebase.")
        report.base_commit = current_commit(root)
        _, prefix = git_location(root)
        report.product_subdir = prefix.as_posix()
    profile = build_profile(cfg.product, code)
    if cfg.ai.provider == "openai-compatible":
        client = stack.enter_context(chat_client(cfg.ai))
        return ChatAnalyzer(client, cfg.ai, profile, gh, code, log, base_commit=report.base_commit)
    client = make_client()
    if hasattr(client, "close"):
        stack.callback(client.close)
    return Analyzer(client, cfg.ai, profile, gh, code, log, base_commit=report.base_commit)


def run(cfg: Config, opts: RunOptions) -> RunReport:
    """Process one poll. CLI callers hold the state lock across this and delivery."""
    state_path = cfg.resolve(cfg.state_file)
    state = load_state(state_path, repair=opts.analyze and opts.update_state)
    report = RunReport(product=cfg.product.name, started_at=utcnow(), model=cfg.ai.model, provider=cfg.ai.provider)
    apply = opts.apply or cfg.workflow.mode == "branch"
    with ExitStack() as stack:
        gh = GitHub(resolve_token())
        stack.callback(gh.close)
        batches = _discover(cfg, opts, state, gh, report)
        if not opts.analyze:
            report.finished_at = utcnow()
            return report
        if opts.update_state:
            # Persist discovery before an interruption or unavailable AI can age items out of the window.
            save_state(state, state_path)
        if batches:
            analyzer = _open_analyzer(cfg, stack, gh, report, apply)
        for w, rr in batches:
            try:
                log(f"• {w.repo}: analyzing with {cfg.ai.model}")
                rr.result, rr.usage, rr.served_by = analyzer.analyze(w, rr.items)
            except TypeError as e:
                if "authentication" not in str(e):
                    raise
                raise ConfigError(NO_CREDS) from e
            except (AnalysisError, anthropic.APIError, GitHubError) as e:
                rr.error = f"{rr.error}. Analysis failed: {e}" if rr.error else str(e)
                if isinstance(e, AnalysisError) and e.usage:
                    rr.usage = e.usage
                log(f"  ✗ {e}")
                continue
            checkpoint = state.get(w.repo)
            del checkpoint.pending[:len(rr.items)]
            state.mark(w.repo, checkpoint.last_run, [i.id for i in rr.items])
    report.finished_at = utcnow()
    if not opts.skip_empty_report or any(r.items or r.error for r in report.repos):
        _, js = write_reports(report, cfg.resolve(cfg.report.dir))
        if apply and any(has_edits(rr) for rr in report.repos):
            state.apply_queue.append(os.path.relpath(js, cfg.root))
    if opts.update_state and batches:
        save_state(state, state_path)
    return report
