"""watchpo command-line interface."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    CONFIG_NAMES,
    Config,
    ConfigError,
    WatchedRepo,
    load_config,
    normalize_repo,
    render_template,
    save_config,
)
from .github import GitHub, GitHubError, resolve_token
from .profile import Codebase, build_profile
from .report import issue_body, write_reports
from .runner import RunOptions, parse_since, run


def _load(args: argparse.Namespace) -> tuple[Config, Path]:
    return load_config(Path(args.config) if args.config else None)


def _detect_name(root: Path) -> str:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if url:
            return normalize_repo(url).split("/")[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return root.resolve().name


def _readme_blurb(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.is_file():
            paras = [b.strip() for b in p.read_text(errors="replace").split("\n\n")]
            for para in paras:
                if para and not para.startswith(("#", "!", "[", "<", "```", "=")):
                    return re.sub(r"\s+", " ", para)[:600]
    return ""


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    existing = [root / n for n in CONFIG_NAMES if (root / n).exists()]
    if existing and not args.force:
        print(f"{existing[0]} already exists (use --force to overwrite).", file=sys.stderr)
        return 1
    name = args.name or _detect_name(root)
    desc = args.description or _readme_blurb(root)
    path = root / "watchpo.yml"
    path.write_text(render_template(name, desc, args.watch or []))
    print(f"Created {path}")
    print("Next: edit product.description, add repos with `watchpo add owner/name`, then `watchpo run`.")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg, path = _load(args)
    repo = normalize_repo(args.repo)
    w = cfg.find(repo)
    fields = {}
    if args.why is not None:
        fields["why"] = args.why
    if args.track:
        fields["track"] = [t.strip() for t in args.track.split(",") if t.strip()]
    if args.paths:
        fields["paths"] = args.paths
    if w is None:
        w = WatchedRepo(repo=repo, **fields)
        if args.check:
            gh = GitHub(resolve_token())
            try:
                gh._get(f"/repos/{w.repo}")
            except GitHubError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
            finally:
                gh.close()
        cfg.watch.append(w)
        print(f"Watching {w.repo} ({', '.join(w.track)})")
    else:
        idx = cfg.watch.index(w)
        cfg.watch[idx] = WatchedRepo.model_validate({**w.model_dump(), **fields})
        print(f"Updated {w.repo}")
    save_config(cfg, path)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cfg, path = _load(args)
    w = cfg.find(args.repo)
    if w is None:
        print(f"{args.repo} is not watched.", file=sys.stderr)
        return 1
    cfg.watch.remove(w)
    save_config(cfg, path)
    print(f"Stopped watching {w.repo}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg, path = _load(args)
    from .state import load_state

    state = load_state(cfg.resolve(cfg.state_file))
    print(f"{cfg.product.name}  ({path})")
    if not cfg.watch:
        print("  (not watching any repos yet; `watchpo add owner/name`)")
    for w in cfg.watch:
        last = state.get(w.repo).last_run
        when = last.strftime("%Y-%m-%d %H:%M UTC") if last else "never"
        print(f"  {w.repo:<40} {','.join(w.track):<24} last run: {when}")
        if w.why:
            print(f"    why: {w.why}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    root = cfg.resolve(cfg.product.path)
    code = Codebase(root) if root.is_dir() else None
    text = build_profile(cfg.product, code)
    print(text)
    print(f"\n[{len(text):,} chars ≈ {len(text) // 4:,} tokens]", file=sys.stderr)
    return 0


def _opts(args: argparse.Namespace, **kw) -> RunOptions:
    return RunOptions(
        repos=args.repo or None,
        since=parse_since(args.since) if args.since else None,
        rescan=args.rescan,
        **kw,
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    report = run(cfg, _opts(args, analyze=False, update_state=False))
    for rr in report.repos:
        print(f"\n## {rr.repo} ({len(rr.items)} new since {rr.since.date().isoformat()})")
        if rr.error:
            print(f"  error: {rr.error}")
        for it in rr.items:
            print(f"  {it.date.date().isoformat()}  {it.id:<22} {it.title[:90]}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    report = run(cfg, _opts(args, update_state=not args.no_state))
    md, js = write_reports(report, cfg.resolve(cfg.report.dir))
    n_sugs = sum(len(r.result.suggestions) for r in report.repos if r.result)
    n_items = sum(len(r.items) for r in report.repos)
    errors = [r for r in report.repos if r.error]
    print(f"\nReport: {md}\n        {js}", file=sys.stderr)
    if args.stdout:
        print(md.read_text())

    if args.issue:
        if n_sugs or errors or (args.issue_always and n_items):
            repo = cfg.report.issue_repo or os.environ.get("GITHUB_REPOSITORY")
            if not repo:
                print("! --issue needs report.issue_repo in watchpo.yml or $GITHUB_REPOSITORY", file=sys.stderr)
                return 1
            title = f"watchpo: {n_sugs} suggestion(s) from {n_items} upstream change(s) ({report.started_at.date().isoformat()})"
            gh = GitHub(resolve_token())
            try:
                url = gh.create_issue(repo, title, issue_body(report), cfg.report.issue_labels)
                print(f"Issue: {url}", file=sys.stderr)
            except GitHubError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
            finally:
                gh.close()
        else:
            print("No suggestions; skipping issue (use --issue-always to open one anyway).", file=sys.stderr)

    attempted = [r for r in report.repos if r.items or r.error]
    if attempted and all(r.error for r in attempted):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watchpo",
        description="Track open-source repos and get Claude's suggestions for improving your product.",
    )
    p.add_argument("--version", action="version", version=f"watchpo {__version__}")
    p.add_argument("-c", "--config", help="path to watchpo.yml (default: search upward from cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create watchpo.yml for your product")
    s.add_argument("--dir", default=".", help="product directory (default: .)")
    s.add_argument("--name")
    s.add_argument("--description")
    s.add_argument("--watch", nargs="*", metavar="OWNER/NAME", help="repos to watch")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add", help="watch a repo (or update its settings)")
    s.add_argument("repo", help="owner/name or GitHub URL")
    s.add_argument("--why", help="why you watch it; steers the analysis")
    s.add_argument("--track", help="comma list of releases,pulls,commits (default: releases,pulls)")
    s.add_argument("--paths", nargs="*", help="only commits touching these paths")
    s.add_argument("--no-check", dest="check", action="store_false", help="don't verify the repo exists")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("remove", help="stop watching a repo")
    s.add_argument("repo")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("list", help="show watched repos")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("profile", help="print the product profile sent to Claude")
    s.set_defaults(fn=cmd_profile)

    for name, fn, help_ in (
        ("fetch", cmd_fetch, "list new upstream changes (no Claude calls, no state change)"),
        ("run", cmd_run, "analyze new upstream changes and write a report"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("-r", "--repo", action="append", help="only this repo (repeatable)")
        s.add_argument("--since", help="window start instead of the last checkpoint: 7d, 48h, 2w, or 2026-11-01")
        s.add_argument("--rescan", action="store_true", help="include changes already analyzed in earlier runs")
        if name == "run":
            s.add_argument("--issue", action="store_true", help="open a GitHub issue with the digest")
            s.add_argument("--issue-always", action="store_true", help="open an issue even with no suggestions")
            s.add_argument("--no-state", action="store_true", help="don't update checkpoints")
            s.add_argument("--stdout", action="store_true", help="also print the report")
        s.set_defaults(fn=fn)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
