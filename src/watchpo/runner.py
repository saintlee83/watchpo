"""Orchestrates a watchpo run: fetch → analyze → report → checkpoint."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anthropic

from .analyzer import AnalysisError, Analyzer, make_client
from .config import Config, WatchedRepo
from .github import GitHub, GitHubError, resolve_token, utcnow
from .models import ChangeItem, RepoReport, RunReport
from .profile import Codebase, build_profile
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
    analyze: bool = True  # False = fetch only (no Claude calls)
    update_state: bool = True


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def selected(cfg: Config, names: list[str] | None) -> list[WatchedRepo]:
    if not names:
        return list(cfg.watch)
    out = []
    for n in names:
        w = cfg.find(n)
        if w is None:
            raise ValueError(f"{n} is not in watchpo.yml (add it with `watchpo add {n}`)")
        out.append(w)
    return out


def window_start(cfg: Config, state: State, w: WatchedRepo, opts: RunOptions, now: datetime) -> datetime:
    if opts.since:
        return opts.since
    last = state.get(w.repo).last_run
    return last if last else now - timedelta(days=cfg.settings.lookback_days)


def fetch_all(cfg: Config, state: State, gh: GitHub, opts: RunOptions, now: datetime) -> list[tuple[WatchedRepo, datetime, list[ChangeItem], str | None]]:
    out = []
    for w in selected(cfg, opts.repos):
        since = window_start(cfg, state, w, opts, now)
        seen = set() if opts.rescan else set(state.get(w.repo).seen)
        log(f"• {w.repo}: fetching {', '.join(w.track)} since {since.date().isoformat()}")
        try:
            items = gh.changes(w, since, cfg.settings, seen)
            log(f"  {len(items)} new change(s)")
            out.append((w, since, items, None))
        except GitHubError as e:
            log(f"  ✗ {e}")
            out.append((w, since, [], str(e)))
    return out


def run(cfg: Config, opts: RunOptions) -> RunReport:
    now = utcnow()
    state_path = cfg.resolve(cfg.state_file)
    state = load_state(state_path)
    token = resolve_token()
    if not token:
        log("! No GitHub token found (GITHUB_TOKEN/GH_TOKEN or `gh auth login`); using the 60 req/hour anonymous limit.")
    gh = GitHub(token)
    report = RunReport(product=cfg.product.name, started_at=now, model=cfg.claude.model)
    try:
        fetched = fetch_all(cfg, state, gh, opts, now)

        analyzer = None
        if opts.analyze and any(items for _, _, items, _ in fetched):
            code_root = cfg.resolve(cfg.product.path)
            code = Codebase(code_root) if code_root.is_dir() else None
            if code is None:
                log(f"! product.path {code_root} not found; analyzing from the description only")
            analyzer = Analyzer(make_client(), cfg.claude, build_profile(cfg.product, code), gh, code, log)

        for w, since, items, err in fetched:
            rr = RepoReport(repo=w.repo, why=w.why, since=since, items=items, error=err)
            report.repos.append(rr)
            if err:
                continue
            if not items:
                if opts.update_state:
                    state.mark(w.repo, now, [])  # advance the window
                continue
            if analyzer is None:  # fetch-only
                continue
            log(f"• {w.repo}: analyzing {len(items)} change(s) with {cfg.claude.model}")
            try:
                rr.result, rr.usage, rr.served_by = analyzer.analyze(w, items)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise SystemExit(f"Claude API auth failed: {e.message}\n{NO_CREDS}") from e
            except TypeError as e:
                if "authentication" not in str(e):
                    raise
                raise SystemExit(NO_CREDS) from e
            except AnalysisError as e:
                rr.error = str(e)
                if e.usage:
                    rr.usage = e.usage
                log(f"  ✗ {e}")
                continue
            except (anthropic.APIError, GitHubError) as e:
                rr.error = str(e)
                log(f"  ✗ {e}")
                continue
            if opts.update_state:
                state.mark(w.repo, now, [i.id for i in items])
                save_state(state, state_path)  # checkpoint after each repo
    finally:
        gh.close()
        if opts.update_state:
            save_state(state, state_path)
    report.finished_at = utcnow()
    return report
