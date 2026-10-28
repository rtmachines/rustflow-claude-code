# Usage

[Documentation index](README.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

## Installation

The installable project is [`rustflow/pyproject.toml`](../pyproject.toml), one level below the repository root. Install with Python 3.11 or newer:

```bash
# From the repository root, preferably in a virtual environment:
python -m pip install ./rustflow
rustflow --version
rustflow --help
```

Alternatively, use `pipx install ./rustflow`, `uv tool install ./rustflow`, or install a built wheel with `python -m pip install /path/to/rustflow-0.1.0-py3-none-any.whl`. These are local installation methods; no registry release is assumed. See [development](development.md) for editable installs and wheel builds.

Install Git, Cargo, rustfmt, and Clippy for the target project. Configure a Git author because the workflow can make checkpoint commits. Install and log in to each provider CLI referenced by your configuration. Defaults route most roles to Claude Code and reviewer B to Codex. A single-provider setup requires changing all applicable role routes. `doctor` checks local CLI flags, model/effort compatibility where available, hook interpreters, project files, and optional tools; it does not prove a remote model request will succeed.

Optional integrations use rust-analyzer, cargo-nextest, cargo-deny, cargo-semver-checks, SQLx CLI, or `gh`. Inspect skipped steps: a green gate result does not establish that every optional integration ran.

### Windows and Linux

Package CI covers Ubuntu and Windows with Python 3.11 and 3.13. Hooks use an executable plus arguments, without requiring Git Bash. The default interpreter is `python3` on POSIX, and `py -3` on Windows if available, otherwise `python`.

```bash
rustflow init --python-cmd "python"
rustflow doctor --selftest
```

Use `init --local` for machine-specific hooks in `.claude/settings.local.json`. Copied gates/hooks/process helpers support Python 3.9+, but the installed CLI still requires 3.11+. On Windows, recognized npm `.cmd` shims are resolved to Node invocations, prompts travel on stdin, and process-tree termination has Windows-specific handling. This does not imply identical provider sandbox guarantees across platforms.

## Initialize a target repository

Run in the Rust repository, not Rustflow's Python checkout:

```bash
rustflow init
rustflow doctor
git diff
git add .
git commit -m "Configure Rustflow"
```

Review `rustflow.toml` first. Set `[project].language = "en"` to request English model-written prose (default `pl`); the full report's fixed headings/status text remain Polish. Commit installed hooks and skills so they are available in task worktrees and to teammates. For a workspace in a subdirectory, set `[gates].cargo_dir`.

Default initialization clones `https://github.com/rtmachines/rust-skills` with depth 1, then copies it without `.git` into both skill directories. This uses the upstream default branch, not a pinned content revision. A failed required download stops installation; some earlier files may already exist. Re-run after fixing connectivity or initialize offline:

```bash
rustflow init --no-rust-skills --no-ci
```

Re-running `init` refreshes managed scripts and instruction blocks while keeping customized non-managed templates, including configuration. `--force` overwrites those templates and refreshes downloaded skills. Inspect customizations first. `--no-rust-skills` skips downloading; it does not remove an existing copy.

### Installed files

| Path in target repository | Purpose / update policy |
| --- | --- |
| `rustflow.toml` | Full defaults; custom file kept unless forced |
| `.rustflow/gates.py`, `.rustflow/hooks.py`, `.rustflow/process.py` | Managed standalone scripts refreshed by `init` |
| `.rustflow/.gitignore` | Ignores `runs/`, `cache/`, `wt/`, `__pycache__/` |
| `.claude/settings.json` | Merged hooks, permissions, plugin settings; `--local` selects the local variant |
| `.claude/skills/`, `.agents/skills/` | Role skills, driver skill `rustflow`, knowledge and optional stack skills |
| `.claude/skills/rustflow-pr-review/SKILL.md`, `.claude/agents/rustflow-reviewer.md` | Managed interactive review integration |
| `REVIEW.md` | Customizable review rules and severity matrix |
| `AGENTS.md`, `CLAUDE.md` | Managed Rustflow blocks within existing files |
| `.config/nextest.toml` | Test timeout/report settings, kept if customized |
| `.github/workflows/rustflow-gates.yml` | Generated CI unless `--no-ci`; custom file kept |
| `.mcp.json` | Merged server registration with `--with-mcp` |

The installer adds `target/` and `.rust-review-results/` to the root ignore file. Invalid settings/MCP JSON is reported and skipped. See [`installer.py`](../rustflow/installer.py).

## Implement a task

Start from a clean tree. Triage is a local heuristic preview and never starts a provider, even when run triage is configured as `llm` or `hybrid`:

```bash
rustflow triage "Add an add(a: u64, b: u64) function with unit tests" --json
rustflow run "Add an add(a: u64, b: u64) function with unit tests"
rustflow status
rustflow report --full
```

Default isolation creates and checks out `rustflow/<run-id>` from current HEAD. `--base` selects the comparison reference; it does not change the starting commit to that reference. The orchestrator can checkpoint successful write phases. No automatic push, PR creation, or merge occurs.

For a UTF-8 task file use `rustflow run -f task.md --difficulty hard`; `rustflow run -` reads stdin. `--difficulty` skips workflow triage; `--no-plan` suppresses planning. `--isolation worktree` uses `.rustflow/wt/<run-id>/`, leaving the main checkout available. Clean worktrees may be removed after completion while the branch remains. `--isolation none` works in the current checkout without branch isolation.

`--allow-dirty` bypasses the clean-tree check. With branch/none isolation, existing edits remain in the run and may enter checkpoints when enabled. Worktree isolation starts from committed HEAD and does not copy uncommitted edits from the main checkout. `--accept-red-baseline` records existing diagnostic signatures and accepts only those known failures, including during strict final gates. New or unitemized failures still block; approval can therefore coexist with explicitly accepted baseline failures. See [troubleshooting](troubleshooting.md).

### Preview prompts

```bash
rustflow run -f task.md --dry-run
rustflow report --full
```

Dry-run uses dummy provider results to exercise prompt construction and write prompt/argv artifacts. It skips real gates and task branch creation. It does not validate credentials/model availability, create provider attempt records, or establish approval of the change.

### Enforce acceptance criteria with tests

Create this file outside the task worktree, or commit it before starting:

```toml
# criteria.toml
[[criteria]]
text = "Addition returns the expected result"
test = "tests::adds"

[[criteria]]
text = "Existing public API remains compatible"
```

```bash
rustflow run "Add an add function and a tests::adds unit test" --criteria criteria.toml
```

The named test must actually run and pass on the checked tree. Short names match suffixes: at least one matching test must pass and no observed matching test may fail. Prefer a fully qualified name to avoid collisions. Zero executed tests, ignored tests, or a different passing test do not satisfy it. Criteria without `test` are assessed by reviewers. Hard-task planning can also supply criteria. Missing required tests become blocking `T-<id>` findings and can end as `criteria_unmet`. The report includes mechanical evidence and reviewer assessments. See [`criteria.py`](../rustflow/criteria.py) and [`test_criteria.py`](../tests/test_criteria.py).

### Gate-affecting changes

Rustflow also checks changes that could make the gates weaker than the base commit. Each such change has a kind:

- `config`: `rustflow.toml`, `.cargo/config`, `.cargo/config.toml`, `.config/nextest.toml`, `clippy.toml`, `.clippy.toml`, `rustfmt.toml`, `.rustfmt.toml`, `rust-toolchain`, `rust-toolchain.toml`, `deny.toml`, `.gitattributes` (each name matches at any depth), and paths matched by `integrity_extra_paths`. Files of these names that `.gitignore` hides count as well (see [configuration](configuration.md#gate-integrity)).
- `cargo-lints`: `[lints]`, `[workspace.lints]`, or `[profile.*]` in a `Cargo.toml`. Adding a dependency is not a change.
- `silence`: an added `#[allow(`, `#![allow(`, `#[expect(`, `cfg_attr(…, allow/expect(`, or `#[ignore` attribute.
- `tests-removed`: a net decrease in test attributes across the diff. Moving a test is fine.
- `unknown`: the scan failed. This always blocks.

Acceptance is automatic; no CLI or MCP option overrides it:

- `config` and `silence` need a proof. Rustflow reruns the gates with the changed configuration restored to the base version and the added attributes stripped. Green accepts the change (`accepted_by: proof`). Red means the change hides a real failure and blocks it; reviewers cannot override that.
- `cargo-lints` and `tests-removed` are accepted only when both reviewers vote `accept` for the current version of that change (a per-change digest).

Every change that is not accepted becomes a blocking finding `G-<kind>-<path>`. Reviewers and the fixer cannot close it. When only these findings remain after a fix round, or at the final check, the run ends as `gate_changes_pending` (exit code 2, resumable). Its `next_actions` (`rustflow status <id> --json`) offers two recovery paths:

1. Revert the change by hand, then run `rustflow resume <id> --extra-rounds 1`. This reruns the gates and review without the fixer.
2. Commit the change yourself as a gate-policy change on the base branch, then start a new run.

Approved runs with accepted gate changes end at most as `approved_with_advisories`. The report has a section "## Zmiany wpływające na bramki" that lists the id, kind, path, status, `accepted_by`, proof, votes, and justification of each change.

## Review existing changes

```bash
# Source review, including uncommitted changes:
rustflow review --base main "Check compatibility and error paths"
rustflow report --full

# Permit fixes in the current checkout:
rustflow review --base main --fix
```

Review mode works in place, accepts a dirty tree, and does not create an isolated branch or implement a new task. Without `--fix`, formatting is checked without rewriting files and no fixer runs. Intermediate Cargo checks are not locked and can generate/update `Cargo.lock`; build tools can still produce artifacts. Final gates are strict. Choose an existing local ref such as `origin/main` if present. Rustflow does not fetch it. A nonliteral-`HEAD` base resolves to its merge base with HEAD, so the diff represents branch changes since divergence. The default base is HEAD; pass a base when reviewing already committed changes. Report-only blocking results finish as `changes_requested`.

## Long-running tasks and recovery

```bash
rustflow run -f task.md --detach
# Substitute the returned run_id:
rustflow wait <run-id> --timeout 540
rustflow status <run-id> --json
rustflow report <run-id> --full
rustflow cancel <run-id>
rustflow resume <run-id> --detach
```

Detached startup JSON is not task success. Repeat `wait` while it exits 75 (`running`). One active run per repository is guarded by `.rustflow/runs/ACTIVE.json`. Avoid edits and Git mutations in its checkout. Cancellation is resumable. Omitted selectors use the latest run; IDs, unique partial IDs, and absolute run-directory paths are supported by lookup.

`rustflow resume <run-id> --extra-rounds 2` continues a stopped fix loop on its own branch/worktree. Raise execution limits with `--max-agent-calls`, `--max-active-seconds`, or `--max-output-bytes`; saved counters are not reset. See [recovery](troubleshooting.md).

## Gates, lint adoption, and CI

```bash
rustflow gates --profile full
rustflow gates --profile edit --no-cache --json
python .rustflow/gates.py run --profile full --check
python .rustflow/gates.py ci
```

Normal local gates can format eligible Rust files and cache a passing result. The package command has no `--check` option; use the copied script for check-only execution. `ci` (or `rustflow gates --ci`) disables caching, checks formatting, and uses locked Cargo behavior. The script also supports `fingerprint` for a tree hash.

| Profile | Checks |
| --- | --- |
| `edit` | Formatting and, by default, `cargo check` |
| `stop` | Formatting, Clippy with warnings denied, SQLx when applicable, tests, matching extras |
| `full` | Stop checks plus doctests, cargo-deny and semver when applicable, matching extras, tree stability check |

Compilation failures can skip redundant tests. nextest is preferred when installed, with Cargo test fallback; doctests are a separate full step. Inspect skipped optional checks and configure required checks as described in [configuration](configuration.md).

`rustflow lints` measures candidate Rust/Clippy lints and reports rules with no current violations. Besides the general set, candidates cover REVIEW.md rules so the compiler enforces them instead of a reviewer: `undocumented_unsafe_blocks`, `let_underscore_must_use` and `unused_result_ok` (swallowed errors), `allow_attributes_without_reason`, and `indexing_slicing` (tests exempt via `allow-indexing-slicing-in-tests`). `rustflow init` points to this command. `--apply` writes adoptable rules to manifests and test exemptions to Clippy configuration, then rechecks Clippy. Review its file changes before committing. `--exclude print_stdout,missing_docs` removes selected candidates. Adoption is not a guarantee about future code.

`init --stack axum-sqlx` adds stack guidance and Ubuntu CI with PostgreSQL, migrations, and required SQLx checks. Standard generated CI uses Ubuntu and Windows. These workflows are separate from [Rustflow package CI](../../.github/workflows/test.yml). They require external Actions/tool downloads and were not executed by the local documentation quickstart.

## Claude Code hooks and interactive review

Initialization registers `PostToolUse` formatting/check feedback, a `Stop` gate, `PreToolUse` command/edit guards, and normally a `UserPromptSubmit` review router. Stop blocks at most three times by default, then emits a manual-review warning; this does not turn failed gates green. Orchestrated reviewers skip write-oriented interactive hooks; the driving session respects the active checkout.

Stop and the commit gate read the gate configuration from `HEAD` and check for unproven [gate-affecting changes](#gate-affecting-changes) in interactive sessions. Stop blocks on them, but that block is capped by `stop_max_blocks`, after which the session ends with a notice. Only the commit gate denies unconditionally; revert such a change, or ask the user to commit it. Orchestrator agents are not checked for gate-affecting changes by hooks, although their Stop gates still run with the base configuration; the orchestrator performs the check itself.

In a trusted Claude Code project, `/rustflow-pr-review [base]` invokes the installed interactive reviewer. This forked read-only interface is separate from the two-reviewer workflow. Hooks validate its Markdown report and can suggest a Trail of Bits audit for unsafe/FFI signals. `init --with-plugins` enables that optional plugin integration; an orchestrated plugin audit also needs auditor configuration. Folder trust and permissions affect hook activation. `doctor --selftest` replays local hook scenarios.

`rustflow comment <run-id> --pr 123 --dry-run` prepares `pr-comment.md` and prints the GitHub CLI command. Removing `--dry-run` publishes a PR comment through authenticated `gh`. Large comments are truncated with a pointer to the local report.

## MCP interface

```bash
rustflow init --with-mcp
rustflow mcp
```

`mcp` serves JSON-RPC over stdio, intended for launch by a client rather than interactive shell use. Initialization merges server registration and Claude permissions. The server uses `CLAUDE_PROJECT_DIR`, falling back to its working directory. Start it in the target repo; it accepts no CLI `--repo` or `--config` flags.

| Tool | Inputs and behavior |
| --- | --- |
| `rustflow_triage` | Required `task`; local difficulty, routing, historical estimate |
| `rustflow_start` | `mode` (`run` or `review`); task needed for run; detached start; `base`, `difficulty`, `max_rounds`, `criteria`, limits, `dry_run`; run-only `isolation`, `allow_dirty`, `accept_red_baseline`, `no_plan`; review-only `fix` |
| `rustflow_wait` | Optional `run`, `timeout_s` default 600, maximum 1800; progress and summary |
| `rustflow_status` | Optional `run`; summary and next actions |
| `rustflow_report` | Optional `run`, `format` = `brief` (default), `json`, or `full` |
| `rustflow_cancel` | Optional `run`; stop worker |
| `rustflow_resume` | Optional `run`, positive `extra_rounds` up to 5, raised execution limits; detached resume |
| `rustflow_gates` | `profile` default `full`; `check_only` default true |

Example `rustflow_start` arguments:

```json
{
  "mode": "run",
  "task": "Add an add(a: u64, b: u64) function with unit tests",
  "isolation": "worktree",
  "max_agent_calls": 20
}
```

Responses include text and `structuredContent`. `next_actions` supplies CLI actions and matching `{tool, arguments}` MCP calls, including actions requiring human decisions. Reports are bounded to about 40,000 characters with truncation notices; use CLI/local files for complete output. See [`mcp_server.py`](../rustflow/mcp_server.py). The repository declares no stable public Python API; CLI and MCP are the documented integration surfaces.

## CLI reference

Use `rustflow --help`, `rustflow --version`, or `rustflow <command> --help`. Put options after the subcommand. All except `mcp` accept `--repo PATH` and `--config PATH`; repo defaults to the current Git toplevel, or the supplied directory outside Git. Some handlers read repository-local configuration directly: `init`, `doctor --selftest`, `lints`, and `gates` do not apply alternate `--config` files. Gates inside a run read `[gates]` from the run's base commit, not from the checkout; commit gate settings before you start. See [configuration](configuration.md).

| Command | Specific arguments/options |
| --- | --- |
| `init` | `--force`, `--python-cmd COMMAND`, `--local`, `--no-ci`, `--no-rust-skills`, `--with-apollo`, `--with-plugins`, `--stack axum-sqlx`, `--with-mcp`, `--no-router` |
| `doctor` | `--selftest` (hook replay instead of ordinary diagnostics) |
| `triage [TASK ...]` | `-f/--file FILE`, `--difficulty easy\|medium\|hard`, `--json` |
| `run [TASK ...]` | Shared options below; `-f/--file FILE`, `--isolation branch\|worktree\|none`, `--allow-dirty`, `--accept-red-baseline`, `--no-plan` |
| `review [TASK ...]` | Shared options below; `-f/--file FILE`, `--fix`; omitted task uses review instruction |
| `resume [RUN]` | `--dry-run`, `--detach`, `--extra-rounds N`, three execution limit overrides |
| `status [RUN]` | `--json` |
| `wait [RUN]` | `--timeout SECONDS` (default 540), `--json` (no progress text) |
| `report [RUN]` | Mutually exclusive `--json` / `--full`; default brief |
| `cancel [RUN]` | Prints cancellation JSON |
| `comment [RUN]` | `--pr NUMBER_OR_URL`, `--dry-run` |
| `lints` | `--apply`, `--exclude COMMA_SEPARATED_NAMES`, `--json` |
| `mcp` | No command-specific options |
| `gates` | `--profile edit\|stop\|full` (default full), `--ci`, `--no-cache`, `--json` |

Shared options for `run`/`review`: `--detach`, `--difficulty easy|medium|hard`, `--base REF`, `--max-rounds N`, `--dry-run`, `--criteria TOML`, `--max-agent-calls N`, `--max-active-seconds N`, `--max-output-bytes N`. Limit overrides are positive integers. Task input comes from `-f FILE`, positional words, or stdin (`-` explicitly selects stdin). File input takes precedence.

`--with-apollo` downloads optional Apollo `rust-best-practices` guidance. `--no-router` omits router registration on fresh installs. `gates --ci` selects CI behavior instead of the local profile; CI is already uncached.

Workflow exits: 0 for approved outcomes, 2 for controlled stops/errors, 3 for unexpected crash, 130 for foreground interruption. Dry-run uses simulated success and does not prove approval. `wait` returns 75 while running, 0 for approved, 3 for crashed, otherwise 2. `status`/`report` return 0 after reading even a failed run; inspect status. Failed gates return 1; doctor returns 1 for required-check failures. Parser/configuration errors generally return 2. See the [status and recovery guide](troubleshooting.md).

## Run artifacts

History lives in the initiating repository even with worktree execution:

```text
.rustflow/
  runs/
    ACTIVE.json
    <run-id>/
      state.json
      report.md
      rustflow.log
      agents/       # prompts, argv, stdout/stderr, structured provider results
      attempts/     # JSON record per physical provider invocation
      gates/        # labeled gate JSON and process logs
  cache/            # gate cache, hook state, logs
  wt/               # task worktrees, when used
```

Other artifacts depend on mode: detached workers write console/progress files, audits can be archived in the run, and `comment` writes `pr-comment.md`. Reports cover branch/workdir, routing, gates, acceptance, findings across rounds, disputes, escalation, and usage. Brief/JSON summaries expose `next_actions`. Raw prompts/logs can contain project content; runtime artifacts are locally ignored by default. See [architecture](architecture.md) for logical calls versus physical attempts and usage accounting.
