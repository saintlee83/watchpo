"""Describe the user's product to Claude, and give it safe read-only code access."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections import Counter
from functools import cached_property
from pathlib import Path, PurePosixPath

from .config import Product

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
    ".next", ".turbo", ".cache", "coverage", ".mypy_cache", ".pytest_cache", ".watchpo",
    "vendor", ".idea", ".vscode",
}
# Never shown to Claude, even if tracked: upstream PR text is untrusted and could
# try to coax secrets into a public issue.
SENSITIVE = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    "secrets.*", "*.secret", "*.secrets", "credentials", "credentials.*",
    "service-account*.json", "*.tfstate", "*.tfstate.*", "*.tfvars", "*.kdbx",
]
MANIFESTS = [
    "package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py",
    "setup.cfg", "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "Package.swift", "pubspec.yaml", "mix.exs",
    "deno.json", "bun.lockb", "Dockerfile", "docker-compose.yml",
]
GUIDES = ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "ARCHITECTURE.md", "docs/architecture.md"]
MAX_FILE_BYTES = 1_000_000
README_CHARS = 10_000
DOC_CHARS = 6_000
TREE_PATHS = 600


def is_sensitive(rel: str) -> bool:
    name = PurePosixPath(rel).name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE)


class Codebase:
    """Read-only view of git-tracked (or non-ignored) text files under root."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @cached_property
    def files(self) -> list[str]:
        files = self._git_files()
        return sorted(f for f in files or [] if not is_sensitive(f))

    @cached_property
    def _file_set(self) -> set[str]:
        return set(self.files)

    def _git_files(self) -> list[str] | None:
        try:
            out = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=self.root, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        paths = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
        return [p for p in paths if not (set(PurePosixPath(p).parts) & SKIP_DIRS)]

    def _safe_path(self, rel: str) -> Path | None:
        rel = rel.strip()
        if rel.startswith("./"):
            rel = rel[2:]
        if rel not in self._file_set:
            return None
        p = (self.root / rel).resolve()
        if p != self.root / rel or not p.is_file():
            return None
        return p

    def read_text(self, rel: str) -> str | None:
        try:
            p = self._safe_path(rel)
            if p is None or p.stat().st_size > MAX_FILE_BYTES:
                return None
            with p.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except (OSError, RuntimeError):
            # A long-lived checkout can change between listing and opening a file.
            return None
        if len(data) > MAX_FILE_BYTES or b"\0" in data[:8192]:
            return None
        return data.decode("utf-8", "replace")

    # -- tools ------------------------------------------------------------

    def list_files(self, glob: str | None = None, limit: int = 300) -> str:
        files = self.files
        if glob:
            files = [f for f in files if fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(PurePosixPath(f).name, glob)]
        shown = files[:limit]
        more = f"\n…and {len(files) - limit} more (narrow the glob)" if len(files) > limit else ""
        return ("\n".join(shown) or "(no matching files)") + more

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> str:
        text = self.read_text(path)
        if text is None:
            if path in self._file_set:
                return f"Error: {path} is binary or too large to read."
            return f"Error: {path} is not a readable tracked file. Use list_files to find paths."
        lines = text.splitlines()
        start = max(1, start_line)
        end = min(len(lines), start + max(1, min(max_lines, 1000)) - 1)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        tail = f"\n…({len(lines) - end} more lines; call again with start_line={end + 1})" if end < len(lines) else ""
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}{tail}"

    def search(self, pattern: str, glob: str | None = None, limit: int = 80) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Error: invalid regex: {e}"
        hits: list[str] = []
        files_hit = 0
        for rel in self.files:
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(PurePosixPath(rel).name, glob)):
                continue
            text = self.read_text(rel)
            if not text:
                continue
            matched = False
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    matched = True
                    if len(hits) < limit:
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
            files_hit += matched
        if not hits:
            return "(no matches)"
        more = f"\n…more matches in {files_hit} files total (narrow the pattern or glob)" if len(hits) >= limit else ""
        return "\n".join(hits) + more


def build_profile(product: Product, code: Codebase | None) -> str:
    """A compact, stable description of the product for the system prompt."""
    parts = [f"# Product: {product.name}"]
    parts.append(product.description.strip() or "(no description provided)")
    if product.focus:
        parts.append("Focus areas the team cares about most: " + ", ".join(product.focus))
    if code is None or not code.files:
        parts.append("\n(No local codebase available; reason from the description only.)")
        return "\n\n".join(parts)

    files = code.files
    readme = next((f for f in files if f.lower() in ("readme.md", "readme.rst", "readme.txt", "readme")), None)
    if readme and (text := code.read_text(readme)):
        parts.append(f"## {readme}\n{_clip(text, README_CHARS)}")
    for g in GUIDES:
        if g in code._file_set and (text := code.read_text(g)):
            parts.append(f"## {g}\n{_clip(text, DOC_CHARS)}")

    manifests = [f for f in files if PurePosixPath(f).name in MANIFESTS and f.count("/") <= 2]
    for m in manifests[:12]:
        if text := code.read_text(m):
            parts.append(f"## {m}\n```\n{_clip(text, DOC_CHARS)}\n```")

    top = Counter(f.split("/", 1)[0] if "/" in f else "." for f in files)
    exts = Counter(PurePosixPath(f).suffix or PurePosixPath(f).name for f in files)
    summary = (
        f"## Repository layout\n{len(files)} files. "
        f"Top-level: {', '.join(f'{d}/ ({n})' if d != '.' else f'root ({n})' for d, n in top.most_common(25))}. "
        f"Common types: {', '.join(f'{e} ({n})' for e, n in exts.most_common(12))}."
    )
    tree = "\n".join(files[:TREE_PATHS])
    if len(files) > TREE_PATHS:
        tree += f"\n…and {len(files) - TREE_PATHS} more files (use the list_files tool)"
    parts.append(f"{summary}\n\n```\n{tree}\n```")
    return "\n\n".join(parts)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated {len(text) - limit} chars]"
