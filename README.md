# watchpo

watchpo runs on a developer machine or a persistent server. It watches upstream
GitHub repositories, uses your chosen AI to compare their changes with your product,
and writes a ranked digest. In branch mode it also turns concrete recommendations
into commits on local Git branches.

Run `watchpo watch` in your product checkout for continuous tracking, or
`watchpo run` for a single pass. GitHub Actions can run the same CLI on a schedule.

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

Report: .watchpo/reports/2026-11-16-090000-<run-id>.md
```

## How it works

1. **Fetch.** For each watched repo, watchpo lists the new releases, merged PRs, and (optionally) commits through the GitHub API. It skips bot authors, applies your label filters, and drops anything already analyzed.
2. **Profile.** It builds a compact profile of your product: your description and focus areas, your README, agent docs (`CLAUDE.md`, `AGENTS.md`), dependency manifests, and the file tree.
3. **Analyze.** For each repo, the configured model gets the list of changes and a few tools:
   - `get_upstream_change`: the full PR description, changed files, and diff excerpts
   - `list_files`, `read_file`, `search_code`: **read-only** access to your codebase
   - `submit_analysis`: returns the structured suggestions

   The model opens the changes that look relevant, checks your code to see whether and where they apply, and then submits its suggestions.
4. **Report.** You get a Markdown digest (plus JSON) ranked by impact, effort, and confidence. You can also have it opened as a GitHub issue.
5. **Resume.** Discovery saves pending changes to disk. A change is marked analyzed after its report is written. When the per-repo batch is full, remaining changes are processed first on later polls.
6. **Apply.** In branch mode, watchpo validates exact text edits against the recorded Git commit and creates a local branch per upstream repo with applicable edits. The JSON report can be replayed after a restart.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/saintlee83/watchpo
# or run without installing:
uvx --from git+https://github.com/saintlee83/watchpo watchpo --help
```

Credentials:

- **AI:** for the default Anthropic provider, set `ANTHROPIC_API_KEY` or sign in with `ant auth login`. For a local or hosted Chat Completions provider, configure `ai` as shown below. Its API key is optional.
- **GitHub:** set `GITHUB_TOKEN` (or `GH_TOKEN`), or be signed in with `gh auth login`. Without a token you're limited to 60 requests/hour.

## Quick start

From your product's repo:

```sh
watchpo init                                   # writes watchpo.yml
$EDITOR watchpo.yml                            # describe your product, set focus areas
watchpo add fastapi/fastapi --why "Our web framework; adopt new features early"
watchpo add litestar-org/litestar --why "Competitor; borrow good ideas"

watchpo fetch                                  # preview the next batch
watchpo profile                                # see the shared product profile
watchpo watch --once                           # try one local poll
watchpo watch --apply                          # keep polling and create local review branches
```

## Continuous local workflow

`watchpo watch` polls immediately, sleeps for the configured interval after each
cycle, and reloads the config for the next poll. The default interval is 900 seconds.
Use `--interval 60` to override it or `--once` for an external scheduler. Idle polls
update the discovery checkpoint and skip model calls and reports. On a self-hosted
machine, run the same command under your service manager with a persistent checkout,
state file, and reports directory. Relative paths resolve from the config directory.

For a tool-capable model on a local server, configure its Chat Completions endpoint.
Replace `your-installed-model` with the model identifier the server exposes.
Ollama supports the required endpoint and tool calls. [Ollama compatibility guide](https://docs.ollama.com/api/openai-compatibility)

```yaml
ai:
  provider: openai-compatible
  base_url: http://127.0.0.1:11434/v1
  model: your-installed-model
  # api_key_env: MODEL_API_KEY    # for endpoints that require authentication
workflow:
  interval_seconds: 900
  mode: branch                     # report or branch
```

In `report` mode, results are Markdown and JSON. In `branch` mode (or with
`--apply`), the model can also return exact replacements for existing, committed
text files. watchpo checks each edit against the recorded base and asks the model
to correct mismatches. Suggestions that need a design decision can remain in the
report without edits. Each branch starts at the checkout's HEAD captured for that
run, using a private Git index. Your current branch, staged changes, and working
files are preserved. Branches are local until you choose to integrate them.

```sh
git branch --list 'watchpo/*'
git show <branch-printed-by-watchpo>
git cherry-pick <branch-printed-by-watchpo>    # integrate the reviewed commit
```

`watchpo list` shows pending analysis and branch work. The next stateful poll
retries queued branches from their saved reports, including when there are no new
upstream changes. `watchpo apply` retries that queue immediately.
`watchpo apply path/to/report.json` replays a specific branch-mode report against
its original base. Both commands run locally without an AI request.
A state-file lock serializes local writers. Keep each product's state with its
checkout and keep reports referenced by its apply queue.

### Keep the watcher running on a server

For example, save this user service as `~/.config/systemd/user/watchpo.service` on
a Linux host. Set the checkout and executable paths to the ones on that host.
The optional environment file holds the credentials required by the selected provider.

```ini
[Unit]
Description=Track upstream changes for my product

[Service]
WorkingDirectory=/srv/my-product
ExecStart=%h/.local/bin/watchpo watch
EnvironmentFile=-%h/.config/watchpo/env
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now watchpo
journalctl --user -u watchpo -f
```

Set `workflow.mode: branch` in the project's config to generate review commits.
The service finishes the current cycle on SIGTERM and wakes immediately if idle.

## Commands

| Command | What it does |
|---|---|
| `watchpo init [--watch a/b c/d]` | Create `watchpo.yml`. The product name comes from the git remote and the description from your README. |
| `watchpo add REPO [--why …] [--track releases,pulls,commits] [--paths …]` | Watch a repo, or update its settings. Accepts `owner/name` or a GitHub URL. |
| `watchpo remove REPO` | Stop watching a repo. |
| `watchpo list` | Show watched repos, the last fetch, and pending work. |
| `watchpo profile` | Print the product profile sent to the selected model. |
| `watchpo fetch [-r REPO] [--since 7d]` | List new upstream changes. Previews the batch without model calls. |
| `watchpo run [-r REPO] [--since 7d] [--rescan] [--issue] [--stdout]` | Run a single cycle. |
| `watchpo watch [--once] [--interval SECONDS] [--apply]` | Poll continuously and optionally create local branches. |
| `watchpo apply [REPORT.json]` | Retry queued branch work or replay one saved report. |

`run` options:

- `--since 7d|48h|2w|2026-11-01`: start the window at this time instead of the last checkpoint. Changes that were already analyzed are still skipped.
- `--rescan`: analyze changes even if an earlier run already covered them.
- `--issue`: open a GitHub issue with the digest in `report.issue_repo` (or `$GITHUB_REPOSITORY`). Issues are opened for suggestions or errors. Add `--issue-always` to include empty digests. After a publication failure, `watchpo publish path/to/report.json` opens an issue from the saved result.
- `--no-state`: don't advance checkpoints. Use this when experimenting.

## Configuration

`watchpo.yml` (see [`examples/watchpo.yml`](examples/watchpo.yml) for a realistic one):

```yaml
product:
  name: acme-api
  description: |
    What the product is, who uses it, the stack, and what the team cares about.
    This is the single biggest lever on suggestion quality.
  path: .                      # local product checkout
  focus: [performance, security]

watch:
  - repo: fastapi/fastapi
    why: Our web framework     # steers the analysis
    track: [releases, pulls]   # any of: releases, pulls, commits
    exclude_labels: [docs]     # PR label filters
    include_labels: []
  - repo: some/monorepo
    track: [commits]
    paths: [packages/core]     # commits touching these paths only
    branch: main

settings:
  lookback_days: 14            # window for a repo's first run
  max_items_per_repo: 50       # per-poll batch; overflow is queued
  exclude_authors: ["dependabot[bot]", "renovate[bot]"]

ai:
  provider: anthropic
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

Existing configs using the `claude:` section are accepted as Anthropic configurations. Use `ai:` for new configs. `effort` and `fallbacks` apply to Anthropic.

### Cost

Each analyzed batch uses one model conversation. The report shows usage returned by the provider and a cost estimate for recognized Anthropic models. Self-hosted endpoint billing depends on your deployment. To spend less:

- Lower `effort`. `medium` works well for small repos or digest-style runs.
- Lower `max_items_per_repo`, or use `include_labels` and `exclude_labels` to cut noise from busy repos.
- Set `explore_codebase: false` to skip reading your code. Suggestions will be less specific.

The Anthropic adapter requests prompt caching so later repos in a run can reuse the product profile.

### Refusal fallbacks

For Anthropic with `fallbacks: default`, watchpo sends the server-side fallback beta (`server-side-fallback-2026-07-01`). If a safety classifier declines a request, the API retries it on a substitute model in the same call. This matters for watchpo because security-related upstream changes (CVE fixes, auth hardening) can occasionally trigger a false positive. If a fallback model produces a repo's analysis, the report says so. Set `fallbacks: off` to disable this.

## Run it on a schedule (GitHub Actions)

In your product's repo:

1. Supply credentials for your configured provider. The example uses an `ANTHROPIC_API_KEY` repository secret. For a compatible endpoint, pass the variable named by `ai.api_key_env` in the step environment.
2. Copy [`examples/workflow.yml`](examples/workflow.yml) to `.github/workflows/watchpo.yml`.

```yaml
- uses: actions/checkout@v4
- uses: saintlee83/watchpo@main
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    args: --issue --since 8d
```

Each week the example runs in report mode and uploads the digest as an artifact. It opens an issue when the digest has suggestions or errors. A self-hosted runner can reach a model on its own loopback interface. Use a persistent checkout for local review branches. State is kept in the Actions cache. GitHub evicts cache entries that go unused for 7 days, so `--since 8d` bounds the window either way.

The job needs `issues: write` permission to open the issue. The default `github.token` can read public upstream repos. To watch private upstream repos, pass a PAT as `github-token`.

## Security model

- **Upstream content is untrusted.** PR descriptions, commit messages, and diffs are written by third parties. The prompt marks them as data and tells the model not to follow instructions inside them.
- **The model's inspection tools are read-only.** It can read only git-tracked files (plus untracked files that aren't ignored) under `product.path`. Path traversal is rejected, binary and large files are skipped, and files that look like secrets (`.env*`, `*.pem`, `*.key`, `secrets.*`, `credentials*`, `*.tfvars`, SSH keys, and so on) are hidden from every tool. File reads skip symlinks. If Git cannot establish the file list, the model receives the configured product description.
- In branch mode, watchpo applies structured text replacements through Git. Generated code is never executed as part of branch creation. An issue is published only when requested.

## Development

```sh
uv sync
uv run pytest
uv run watchpo --help
```

The tests cover config parsing, the codebase sandbox, GitHub fetching (against a mocked API), the Anthropic tool loop, fallback echo handling, report rendering, and a local HTTP provider driving a real Git branch through a restart.

## License

Apache-2.0
