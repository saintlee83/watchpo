# watchpo

An AI watchdog for GitHub repos that tracks upstream changes and suggests improvements to your own codebase.

You give watchpo a list of open-source repos you learn from: dependencies, competitors, projects in the same space. It pulls what changed since the last run (releases, merged PRs, commits). Then Claude reads those changes next to **your** code and reports the few worth acting on: what changed upstream, why it matters for your product, and what to change, naming the files.

Illustrative run:

```
$ watchpo run
• fastapi/fastapi: fetching releases, pulls since 2026-11-09
  23 new change(s)
• fastapi/fastapi: analyzing 23 change(s) with claude-opus-5-5
  → upstream pr#14211: Use orjson for response serialization when available
  → search_code /JSONResponse/
  → read_file app/api/responses.py:1
  ✓ 2 suggestion(s) after 6 turn(s)

Report: .watchpo/reports/2026-11-16-0900.md
```

## How it works

1. **Fetch.** For each watched repo, watchpo lists the new releases, merged PRs, and (optionally) commits through the GitHub API. It skips bot authors, applies your label filters, and drops anything already analyzed.
2. **Profile.** It builds a compact profile of your product: your description and focus areas, your README, agent docs (`CLAUDE.md`, `AGENTS.md`), dependency manifests, and the file tree.
3. **Analyze.** For each repo, Claude gets the list of changes and a few tools:
   - `get_upstream_change`: the full PR description, changed files, and diff excerpts
   - `list_files`, `read_file`, `search_code`: **read-only** access to your codebase
   - `submit_analysis`: returns the structured suggestions

   Claude opens the changes that look relevant, checks your code to see whether and where they apply, and then submits its suggestions.
4. **Report.** You get a Markdown digest (plus JSON) ranked by impact, effort, and confidence. You can also have it opened as a GitHub issue.
5. **Checkpoint.** State is saved in `.watchpo/state.json`, so the next run only looks at new changes.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/saintlee83/watchpo
# or run without installing:
uvx --from git+https://github.com/saintlee83/watchpo watchpo --help
```

Credentials:

- **Claude:** set `ANTHROPIC_API_KEY`, or sign in with `ant auth login`.
- **GitHub:** set `GITHUB_TOKEN` (or `GH_TOKEN`), or be signed in with `gh auth login`. Without a token you're limited to 60 requests/hour.

## Quick start

From your product's repo:

```sh
watchpo init                                   # writes watchpo.yml
$EDITOR watchpo.yml                            # describe your product, set focus areas
watchpo add fastapi/fastapi --why "Our web framework; adopt new features early"
watchpo add litestar-org/litestar --why "Competitor; borrow good ideas"

watchpo fetch                                  # preview what's new (no Claude calls)
watchpo profile                                # see exactly what Claude is told about your product
watchpo run                                    # analyze and write the report
```

## Commands

| Command | What it does |
|---|---|
| `watchpo init [--watch a/b c/d]` | Create `watchpo.yml`. The product name comes from the git remote and the description from your README. |
| `watchpo add REPO [--why …] [--track releases,pulls,commits] [--paths …]` | Watch a repo, or update its settings. Accepts `owner/name` or a GitHub URL. |
| `watchpo remove REPO` | Stop watching a repo. |
| `watchpo list` | Show watched repos and when each was last analyzed. |
| `watchpo profile` | Print the product profile sent to Claude. |
| `watchpo fetch [-r REPO] [--since 7d]` | List new upstream changes. Makes no Claude calls and doesn't change state. |
| `watchpo run [-r REPO] [--since 7d] [--rescan] [--issue] [--stdout]` | Full pipeline. |

`run` options:

- `--since 7d|48h|2w|2026-11-01`: start the window at this time instead of the last checkpoint. Changes that were already analyzed are still skipped.
- `--rescan`: analyze changes even if an earlier run already covered them.
- `--issue`: open a GitHub issue with the digest in `report.issue_repo` (or `$GITHUB_REPOSITORY`). This only happens when there are suggestions; add `--issue-always` to open one regardless.
- `--no-state`: don't advance checkpoints. Use this when experimenting.

## Configuration

`watchpo.yml` (see [`examples/watchpo.yml`](examples/watchpo.yml) for a realistic one):

```yaml
product:
  name: acme-api
  description: |
    What the product is, who uses it, the stack, and what the team cares about.
    This is the single biggest lever on suggestion quality.
  path: .                      # codebase Claude may read (read-only)
  focus: [performance, security]

watch:
  - repo: fastapi/fastapi
    why: Our web framework     # steers what Claude looks for
    track: [releases, pulls]   # any of: releases, pulls, commits
    exclude_labels: [docs]     # PR label filters
    include_labels: []
  - repo: some/monorepo
    track: [commits]
    paths: [packages/core]     # commits touching these paths only
    branch: main

settings:
  lookback_days: 14            # window for a repo's first run
  max_items_per_repo: 50       # newest N changes (releases always first)
  exclude_authors: ["dependabot[bot]", "renovate[bot]"]

claude:
  model: claude-opus-5-5
  effort: high                 # low | medium | high | xhigh | max
  max_turns: 30                # tool-use turns per repo
  fallbacks: default           # see below
  explore_codebase: true       # false = reason from the profile only (cheaper)

report:
  dir: .watchpo/reports
  issue_repo: acme/acme-api
  issue_labels: [watchpo]

state_file: .watchpo/state.json
```

`watchpo add` and `watchpo remove` rewrite the file, which drops YAML comments. If you want to keep comments, edit the file by hand.

### Cost

Each watched repo with new changes costs one Claude conversation. The report header shows the token totals and an approximate dollar cost. To spend less:

- Lower `effort`. `medium` works well for small repos or digest-style runs.
- Lower `max_items_per_repo`, or use `include_labels` and `exclude_labels` to cut noise from busy repos.
- Set `explore_codebase: false` to skip reading your code. Suggestions will be less specific.

Your product profile sits in a cached system prompt, so the second and later repos in a run reuse it at the cached-input rate.

### Refusal fallbacks

With `fallbacks: default`, watchpo sends the server-side fallback beta (`server-side-fallback-2026-07-01`). If a safety classifier declines a request, the API retries it on a substitute model in the same call. This matters for watchpo because security-related upstream changes (CVE fixes, auth hardening) can occasionally trigger a false positive. If a fallback model produces a repo's analysis, the report says so. Set `fallbacks: off` to disable this.

## Run it on a schedule (GitHub Actions)

In your product's repo:

1. Add a repository secret named `ANTHROPIC_API_KEY`.
2. Copy [`examples/workflow.yml`](examples/workflow.yml) to `.github/workflows/watchpo.yml`.

```yaml
- uses: actions/checkout@v4
- uses: saintlee83/watchpo@main
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    args: --issue --since 8d
```

Each week the workflow opens an issue with the digest and uploads the full report as an artifact. State is kept in the Actions cache. GitHub evicts cache entries that go unused for 7 days, so `--since 8d` bounds the window either way.

The job needs `issues: write` permission to open the issue. The default `github.token` can read public upstream repos. To watch private upstream repos, pass a PAT as `github-token`.

## Security model

- **Upstream content is untrusted.** PR descriptions, commit messages, and diffs are written by third parties. The prompt marks them as data and tells Claude not to follow instructions inside them.
- **Claude's codebase access is read-only and sandboxed.** Claude can read only git-tracked files (plus untracked files that aren't ignored) under `product.path`. Path traversal is rejected, binary and large files are skipped, and files that look like secrets (`.env*`, `*.pem`, `*.key`, `secrets.*`, `credentials*`, `*.tfvars`, SSH keys, and so on) are hidden from every tool. These rules matter if digests go to a public issue tracker.
- Claude has no write tools. watchpo's only writes are its own report, its state file, and the optional issue.

## Development

```sh
uv sync
uv run pytest
uv run watchpo --help
```

The tests cover config parsing, the codebase sandbox, GitHub fetching (against a mocked API), the Claude tool loop (with a scripted fake client built on the SDK's real response types), fallback echo handling, and report rendering.

## License

Apache-2.0
