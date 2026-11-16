"""Render run results as Markdown/JSON and optionally publish as a GitHub issue."""

from __future__ import annotations

from pathlib import Path

from .analyzer import estimate_cost
from .models import ChangeItem, RepoReport, RunReport, Suggestion, Usage

ISSUE_LIMIT = 65_000  # GitHub caps issue bodies at 65,536 chars
RANK = {"high": 0, "medium": 1, "low": 2, "small": 0, "large": 2}


def rank(s: Suggestion) -> tuple[int, int, int]:
    return (RANK[s.impact], RANK.get(s.effort, 1), RANK[s.confidence])


def ordered(rr: RepoReport) -> list[Suggestion]:
    return sorted(rr.result.suggestions, key=rank) if rr.result else []


def total_usage(run: RunReport) -> Usage:
    u = Usage()
    for rr in run.repos:
        u.add(rr.usage)
    return u


def _refs(refs: list[str], items: dict[str, ChangeItem]) -> str:
    out = []
    for r in refs:
        it = items.get(r)
        out.append(f"[{r}]({it.url})" if it else f"`{r}`")
    return ", ".join(out) or "-"


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(run: RunReport, compact: bool = False) -> str:
    date = run.started_at.date().isoformat()
    n_items = sum(len(r.items) for r in run.repos)
    all_sugs = [(rr, s) for rr in run.repos for s in ordered(rr)]
    u = total_usage(run)
    cost = estimate_cost(run.model, u)

    lines = [f"# watchpo digest: {run.product} ({date})", ""]
    stats = [
        _n(len(run.repos), "repo") + " watched",
        _n(n_items, "new upstream change"),
        _n(len(all_sugs), "suggestion"),
        f"model `{run.model}`",
    ]
    if u.requests:
        stats.append(f"{u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens:,} in / {u.output_tokens:,} out tokens")
    if cost is not None and u.requests:
        stats.append(f"≈${cost:.2f}")
    lines += [" · ".join(stats), ""]

    # Global numbering so the summary table and the sections line up.
    numbers: dict[int, str] = {}
    if all_sugs:
        top = sorted(all_sugs, key=lambda p: rank(p[1]))
        for i, (_, s) in enumerate(top, 1):
            numbers[id(s)] = f"S{i}"
        lines += [
            "## Top suggestions",
            "",
            "| # | Suggestion | From | Impact | Effort | Confidence |",
            "|---|---|---|---|---|---|",
        ]
        for rr, s in top:
            lines.append(
                f"| {numbers[id(s)]} | {_cell(s.title)} | {rr.repo} | {s.impact} | {s.effort} | {s.confidence} |"
            )
        lines.append("")
    elif n_items:
        lines += ["No actionable suggestions this time. See the per-repo summaries below.", ""]

    for rr in run.repos:
        items = {i.id: i for i in rr.items}
        lines += [f"## [{rr.repo}](https://github.com/{rr.repo})", ""]
        meta = f"{_n(len(rr.items), 'new change')} since {rr.since.date().isoformat()}"
        if rr.why:
            meta = f"Why we watch it: {rr.why} · " + meta
        lines += [f"_{meta}_", ""]
        if rr.error:
            lines += [f"> ⚠️ Analysis failed: {rr.error}", ""]
        if not rr.items:
            lines += ["Nothing new.", ""]
            continue
        if rr.result is None:
            continue
        lines += [rr.result.summary.strip(), ""]
        if rr.served_by and rr.served_by != run.model:
            lines += [f"_Served by fallback model `{rr.served_by}`._", ""]
        for s in ordered(rr):
            lines += [
                f"### {numbers.get(id(s), '•')}. {s.title}",
                "",
                f"`{s.category}` · impact **{s.impact}** · effort {s.effort} · confidence {s.confidence} · from {_refs(s.upstream_refs, items)}",
                "",
            ]
            if compact:
                lines += [s.why_it_matters.strip(), ""]
                continue
            lines += [
                f"**What changed upstream.** {s.what_changed.strip()}",
                "",
                f"**Why it matters.** {s.why_it_matters.strip()}",
                "",
                "**Recommendation.**",
                "",
                s.recommendation.strip(),
                "",
            ]
            if s.affected_files:
                lines += ["**Files:** " + ", ".join(f"`{f}`" for f in s.affected_files), ""]
        if rr.result.also_noted:
            lines += [f"<details><summary>Also noted ({len(rr.result.also_noted)})</summary>", ""]
            for n in rr.result.also_noted:
                lines.append(f"- {_refs([n.ref], items)}: {n.note.strip()}")
            lines += ["", "</details>", ""]
        if not compact:
            lines += [f"<details><summary>{_n(len(rr.items), 'change')} analyzed</summary>", ""]
            for it in rr.items:
                lines.append(f"- [{it.id}]({it.url}) {_cell(it.title)} ({it.date.date().isoformat()})")
            lines += ["", "</details>", ""]

    lines += ["---", "_Generated by [watchpo](https://github.com/saintlee83/watchpo)._", ""]
    return "\n".join(lines)


def issue_body(run: RunReport) -> str:
    body = render_markdown(run)
    if len(body) <= ISSUE_LIMIT:
        return body
    body = render_markdown(run, compact=True)
    if len(body) <= ISSUE_LIMIT:
        return body + "\n_Details trimmed to fit GitHub's issue size limit; see the full report artifact._\n"
    return body[: ISSUE_LIMIT - 200] + "\n\n…[truncated to fit GitHub's issue size limit; see the full report artifact]\n"


def write_reports(run: RunReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.strftime("%Y-%m-%d-%H%M")
    md = out_dir / f"{stamp}.md"
    js = out_dir / f"{stamp}.json"
    md.write_text(render_markdown(run))
    js.write_text(run.model_dump_json(indent=2))
    (out_dir / "latest.md").write_text(md.read_text())
    return md, js
