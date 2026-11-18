"""Build review commits with a private Git index, including on dirty checkouts."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .models import AnalysisResult, RepoReport, RunReport
from .profile import MAX_FILE_BYTES, SKIP_DIRS, is_sensitive


class BranchError(Exception):
    pass


def _git(root: Path, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        proc = subprocess.run(
            ["git", "--literal-pathspecs", "-c", "core.hooksPath=" + os.devnull,
             "-c", "user.name=watchpo", "-c", "user.email=watchpo@localhost",
             "-c", "commit.gpgSign=false", *args],
            cwd=root, input=data, capture_output=True, env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BranchError(f"Unable to run git in {root}: {e}") from e
    if proc.returncode:
        raise BranchError(proc.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return proc.stdout


def current_commit(root: Path) -> str:
    return _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()


def has_edits(repo: RepoReport) -> bool:
    return bool(repo.result and any(s.edits for s in repo.result.suggestions))


def branch_name(report: RunReport, repo: RepoReport) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", repo.repo).strip("-")[:64]
    key = hashlib.sha256(repo.repo.encode()).hexdigest()[:8]
    return f"watchpo/{slug}-{key}-{report.started_at:%Y%m%d}-{report.run_id.hex[:12]}"


def git_location(root: Path) -> tuple[Path, Path]:
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    return top, root.resolve().relative_to(top.resolve())


def plan_edits(top: Path, prefix: Path, base: str, result: AnalysisResult) -> dict[str, tuple[str, bytes]]:
    contents: dict[str, tuple[str, bytes]] = {}
    for suggestion in result.suggestions:
        for edit in suggestion.edits:
            rel = PurePosixPath(edit.path)
            if (rel.is_absolute() or rel.as_posix() != edit.path or ".." in rel.parts
                    or set(rel.parts) & SKIP_DIRS or is_sensitive(edit.path)
                    or edit.path not in suggestion.affected_files):
                raise BranchError(f"Unsupported edit path: {edit.path}")
            path = (prefix / rel).as_posix()
            if path in contents:
                mode, data = contents[path]
            else:
                entry = _git(top, "ls-tree", "-z", base, "--", path)
                meta, _, filename = entry.rstrip(b"\0").partition(b"\t")
                fields = meta.split()
                if (filename != os.fsencode(path) or len(fields) != 3
                        or fields[0] not in (b"100644", b"100755")):
                    raise BranchError(f"{edit.path} must be a committed regular file.")
                mode = fields[0].decode()
                blob = fields[2].decode()
                if int(_git(top, "cat-file", "-s", blob)) > MAX_FILE_BYTES:
                    raise BranchError(f"{edit.path} is too large to edit.")
                data = _git(top, "cat-file", "blob", blob)
                if b"\0" in data:
                    raise BranchError(f"{edit.path} is binary.")
            before, after = edit.before.encode(), edit.after.encode()
            if b"\r\n" in data and b"\r\n" not in before + after:
                before, after = before.replace(b"\n", b"\r\n"), after.replace(b"\n", b"\r\n")
            if data.count(before) != 1 or before == after or b"\0" in after:
                raise BranchError(f"{edit.path}: edit needs one exact match and a changed replacement.")
            contents[path] = (mode, data.replace(before, after, 1))
    return contents


def apply_report(root: Path, report: RunReport) -> list[str]:
    """Install each repo's edits as one local branch. Replaying a report is idempotent."""
    repos = [rr for rr in report.repos if has_edits(rr)]
    if not repos:
        return []
    base = report.base_commit
    if not base or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", base):
        raise BranchError("The report needs a recorded Git base commit.")
    top, prefix = git_location(root)
    if report.product_subdir != prefix.as_posix():
        raise BranchError("The configured product directory differs from the one recorded in this report.")
    branches = []
    for rr in repos:
        name = branch_name(report, rr)
        with tempfile.TemporaryDirectory(prefix="watchpo-index-") as temp:
            index = Path(temp) / "index"
            _git(top, "read-tree", base, index=index)
            contents = plan_edits(top, prefix, base, rr.result)
            for path, (mode, data) in contents.items():
                blob = _git(top, "hash-object", "-w", "--stdin", data=data).decode().strip()
                _git(top, "update-index", "--add", "--cacheinfo", mode, blob, path, index=index)
            tree = _git(top, "write-tree", index=index).decode().strip()
            ref = "refs/heads/" + name
            if _git(top, "for-each-ref", "--format=%(refname)", ref).decode().strip() == ref:
                if (_git(top, "rev-parse", ref + "^{tree}").decode().strip() != tree
                        or _git(top, "rev-parse", ref + "^").decode().strip() != base):
                    raise BranchError(f"Local branch {name} already exists with different contents.")
            else:
                titles = "\n".join("- " + s.title for s in rr.result.suggestions if s.edits)
                message = f"watchpo: apply suggestions from {rr.repo}\n\n{titles}\n"
                commit = _git(top, "commit-tree", tree, "-p", base, data=message.encode()).decode().strip()
                _git(top, "update-ref", ref, commit, "0" * len(base))
        branches.append(name)
    return branches
