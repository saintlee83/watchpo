"""Minimal GitHub REST client: fetch releases, merged PRs, commits, and details."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx2

from .config import Settings, WatchedRepo
from .models import ChangeItem

API = "https://api.github.com"
BODY_PREVIEW = 1200  # chars of PR/release body shown in the change list
DETAIL_BODY = 12000  # chars of body returned by the detail tool
DETAIL_PATCH_BUDGET = 24000  # total chars of patch returned by the detail tool
PATCH_PER_FILE = 4000


class GitHubError(Exception):
    pass


def resolve_token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if tok := os.environ.get(var):
            return tok
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx2.BaseTransport | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "watchpo",
        }
        self.authenticated = bool(token)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx2.Client(
            base_url=API, headers=headers, timeout=30.0, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    # -- low level -------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx2.Response:
        for attempt in range(4):
            try:
                r = self._http.get(url, params=params)
            except httpx2.TransportError as e:
                if attempt == 3:
                    raise GitHubError(f"GET {url}: {e}") from e
                time.sleep(2**attempt)
                continue
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                reset = int(r.headers.get("x-ratelimit-reset", "0"))
                wait = max(0, reset - int(time.time())) + 1
                if wait > 120:
                    hint = "" if self.authenticated else " Set GITHUB_TOKEN to raise the limit."
                    raise GitHubError(f"GitHub rate limit exhausted (resets in {wait}s).{hint}")
                print(f"  rate limited; waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            if r.status_code == 404:
                raise GitHubError(f"Not found: {url} (private repo or typo?)")
            if r.status_code >= 400:
                raise GitHubError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
            return r
        raise GitHubError(f"GET {url}: retries exhausted")

    def _paginate(self, url: str, params: dict[str, Any], max_pages: int = 10) -> Iterator[dict]:
        page_url: str | None = url
        page_params: dict[str, Any] | None = {**params, "per_page": 100}
        for _ in range(max_pages):
            if page_url is None:
                return
            r = self._get(page_url, page_params)
            yield from r.json()
            page_url = r.links.get("next", {}).get("url")
            page_params = None  # the next link already carries the query

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        r = self._http.post(
            f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels}
        )
        if r.status_code >= 400:
            raise GitHubError(f"creating issue in {repo} -> {r.status_code}: {r.text[:300]}")
        return r.json()["html_url"]

    # -- change discovery -----------------------------------------------

    def releases(self, repo: str, since: datetime) -> list[ChangeItem]:
        items = []
        for rel in self._paginate(f"/repos/{repo}/releases", {}, max_pages=3):
            if rel.get("draft"):
                continue
            published = rel.get("published_at") or rel.get("created_at")
            if not published:
                continue
            when = parse_dt(published)
            if when < since:
                # Releases come newest first; stop at the first old one.
                break
            items.append(
                ChangeItem(
                    id=f"release:{rel['tag_name']}",
                    kind="release",
                    repo=repo,
                    title=rel.get("name") or rel["tag_name"],
                    url=rel["html_url"],
                    author=(rel.get("author") or {}).get("login"),
                    date=when,
                    body=rel.get("body") or "",
                    labels=["prerelease"] if rel.get("prerelease") else [],
                    tag=rel["tag_name"],
                )
            )
        return items

    def merged_pulls(self, w: WatchedRepo, since: datetime) -> list[ChangeItem]:
        items = []
        params = {"state": "closed", "sort": "updated", "direction": "desc"}
        for pr in self._paginate(f"/repos/{w.repo}/pulls", params, max_pages=10):
            if parse_dt(pr["updated_at"]) < since:
                break  # sorted by updated desc; nothing older can have merged later
            merged = pr.get("merged_at")
            if not merged or parse_dt(merged) < since:
                continue
            labels = [lbl["name"] for lbl in pr.get("labels", [])]
            items.append(
                ChangeItem(
                    id=f"pr#{pr['number']}",
                    kind="pr",
                    repo=w.repo,
                    title=pr["title"],
                    url=pr["html_url"],
                    author=(pr.get("user") or {}).get("login"),
                    date=parse_dt(merged),
                    body=pr.get("body") or "",
                    labels=labels,
                    number=pr["number"],
                )
            )
        return items

    def commits(self, w: WatchedRepo, since: datetime) -> list[ChangeItem]:
        paths = w.paths or [None]
        found: dict[str, ChangeItem] = {}
        for path in paths:
            params: dict[str, Any] = {"since": since.isoformat()}
            if w.branch:
                params["sha"] = w.branch
            if path:
                params["path"] = path
            for c in self._paginate(f"/repos/{w.repo}/commits", params, max_pages=5):
                sha = c["sha"]
                msg = c["commit"]["message"]
                title, _, rest = msg.partition("\n")
                author = (c.get("author") or {}).get("login") or c["commit"]["author"].get("name")
                found[sha] = ChangeItem(
                    id=f"commit:{sha[:10]}",
                    kind="commit",
                    repo=w.repo,
                    title=title,
                    url=c["html_url"],
                    author=author,
                    date=parse_dt(c["commit"]["committer"]["date"]),
                    body=rest.strip(),
                    sha=sha,
                )
        return list(found.values())

    def changes(
        self, w: WatchedRepo, since: datetime, settings: Settings, seen: set[str]
    ) -> list[ChangeItem]:
        items: list[ChangeItem] = []
        if "releases" in w.track:
            items += self.releases(w.repo, since)
        if "pulls" in w.track:
            items += self.merged_pulls(w, since)
        if "commits" in w.track:
            items += self.commits(w, since)
        return filter_items(items, w, settings, seen)

    # -- details for the deep-dive tool ----------------------------------

    def detail(self, item: ChangeItem) -> str:
        if item.kind == "release":
            return f"# {item.title} ({item.tag})\n{item.url}\n\n{clip(item.body, DETAIL_BODY * 2)}"
        if item.kind == "pr":
            pr = self._get(f"/repos/{item.repo}/pulls/{item.number}").json()
            files = list(
                self._paginate(f"/repos/{item.repo}/pulls/{item.number}/files", {}, max_pages=3)
            )
            head = (
                f"# PR #{item.number}: {pr['title']}\n{item.url}\n"
                f"author: {item.author}  merged: {pr.get('merged_at')}  "
                f"+{pr.get('additions')} -{pr.get('deletions')} in {pr.get('changed_files')} files\n"
                f"labels: {', '.join(item.labels) or '-'}\n\n"
                f"## Description\n{clip(pr.get('body') or '(none)', DETAIL_BODY)}\n"
            )
            return head + render_files(files)
        c = self._get(f"/repos/{item.repo}/commits/{item.sha}").json()
        head = (
            f"# Commit {item.sha}\n{item.url}\nauthor: {item.author}\n\n"
            f"## Message\n{clip(c['commit']['message'], DETAIL_BODY)}\n"
        )
        return head + render_files(c.get("files", []))


def filter_items(
    items: list[ChangeItem], w: WatchedRepo, settings: Settings, seen: set[str]
) -> list[ChangeItem]:
    bots = {a.lower() for a in settings.exclude_authors}
    inc = {lbl.lower() for lbl in w.include_labels}
    exc = {lbl.lower() for lbl in w.exclude_labels}
    out = []
    for it in items:
        if it.id in seen:
            continue
        if it.author and it.author.lower() in bots:
            continue
        if it.kind == "pr":
            labels = {lbl.lower() for lbl in it.labels}
            if inc and not labels & inc:
                continue
            if labels & exc:
                continue
        out.append(it)
    # Releases first (they summarize many changes), then newest first.
    out.sort(key=lambda i: (i.kind != "release", -i.date.timestamp()))
    return out[: settings.max_items_per_repo]


def render_files(files: list[dict]) -> str:
    lines = [f"\n## Changed files ({len(files)})"]
    for f in files:
        lines.append(f"- {f['filename']} ({f.get('status')}, +{f.get('additions', 0)} -{f.get('deletions', 0)})")
    budget = DETAIL_PATCH_BUDGET
    patches = ["\n## Patches"]
    # Biggest changes are usually the interesting ones; show them first.
    ranked = sorted(files, key=lambda f: -(f.get("additions", 0) + f.get("deletions", 0)))
    omitted = 0
    for f in ranked:
        patch = f.get("patch")
        if not patch:
            continue
        if budget <= 0:
            omitted += 1
            continue
        chunk = clip(patch, min(PATCH_PER_FILE, budget))
        budget -= len(chunk)
        patches.append(f"### {f['filename']}\n```diff\n{chunk}\n```")
    if omitted:
        patches.append(f"({omitted} more file patches omitted for length)")
    return "\n".join(lines + patches)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
