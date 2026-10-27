# Configuration reference

Rustflow reads `rustflow.toml` from the target repository root. Missing keys inherit the packaged [default configuration](../rustflow/templates/rustflow.toml). This page describes the defaults in this checkout; model names and CLI flags are passed to your installed provider CLIs, whose support must be checked in your environment.

For commands and examples, see [Usage](usage.md). For execution and persistence, see [Architecture](architecture.md). For failed runs, see [Troubleshooting](troubleshooting.md).

## Loading and precedence

The orchestrator loads packaged defaults, then recursively merges the project's TOML tables. Scalar values and arrays replace their defaults; arrays are never appended. For example, `models.claude = ["opus"]` replaces the entire Claude allowlist, so every configured Claude role must then use `opus`.

For handlers using `Config.load` (`run`, `review`, `resume`, `triage`, and ordinary `doctor`), `--config PATH` selects that file instead of the repository's `rustflow.toml`; relative paths are relative to the invoking process's working directory. An explicitly requested missing file is an error. Without `--config`, a missing project file uses defaults. Invalid TOML or validated routing options fail configuration loading. Other commands also accept the common parser option but do not apply it to their repository-local settings; see the [CLI reference](usage.md#cli-reference).

Command options override the corresponding defaults where provided: `--base`, `--difficulty`, `--isolation`, `--max-rounds`, `--no-plan`, `--max-agent-calls`, `--max-active-seconds`, and `--max-output-bytes`. The public review interfaces use `isolation = "none"` regardless of `[project].isolation` and never run the planner; `--isolation` is a run-only CLI option.

**Gate configuration has a separate loader.** Standalone gates and hooks read `[gates]` and `[process].max_output_bytes` from `rustflow.toml` in their detected project root. The orchestrator also reloads gates from its working checkout. Consequently, an alternate `--config` file controls orchestration but does not replace the checkout's `[gates]` settings. The orchestrator does apply its effective output limit to its own gate runs. Put gate settings in the checkout's `rustflow.toml`, including when using worktree isolation. Invalid TOML makes the standalone loader warn and use gate defaults; the Stop hook surfaces this once per session. This differs from the orchestrator's configuration validation.

Configuration validation covers model and effort allowlists, the documented routing enums, review severity/confidence options, and positive execution limits. It is not a complete schema validator: unknown keys are retained, and many gate/provider settings are consumed without enum or type validation. A misspelled key can therefore have no effect. Use `rustflow doctor` after editing configuration. See [the loader](../rustflow/config.py) and [gate defaults and loader](../rustflow/gates.py) for exact behavior.

## Minimal project override

This file keeps the default routing, requests English model-written prose, and uses a worktree:

```toml
[project]
language = "en"
isolation = "worktree"

[budget]
max_agent_calls = 30
max_active_seconds = 5400
max_total_usd = 40.0
```

The packaged default language is **Polish** (`"pl"`). The setting affects requested agent prose; [`report.py`](../rustflow/report.py) still renders fixed report headings and status descriptions in Polish even with `"en"`. The language of this documentation does not change runtime defaults.

## Project settings

| Key in `[project]` | Default | Behavior |
| --- | --- | --- |
| `base` | `"HEAD"` | Git reference resolved to a commit at preflight for snapshots and diffs. In review mode, a non-`HEAD` base uses its merge base with `HEAD`. This selects the comparison base; isolation branches still start from the current checkout's `HEAD`. |
| `language` | `"pl"` | Language requested for model-written prose; use `"en"` for English agent responses. Fixed full-report labels remain Polish. |
| `isolation` | `"branch"` | `branch`: switch to `rustflow/<run-id>` in the current checkout. `worktree`: create that branch in `.rustflow/wt/<run-id>`. `none`: work in place. |
| `checkpoint_commits` | `true` | Enable orchestrator checkpoint commits. Always disabled with `isolation = "none"`. |
| `baseline_gates` | `true` | Run strict full gates before implementation; skipped in review mode. A red baseline stops preflight unless `--accept-red-baseline` is supplied. |
| `python` | `""` | Interpreter command embedded in prompts and allowed-tool rules. Empty means `python3` on Unix; on Windows, `py -3` when available, otherwise `python`. This does not switch the Python process already running Rustflow. |

`branch` and `worktree` normally require a clean checkout. `--allow-dirty` bypasses that check; review mode accepts an existing diff. Worktrees use the main checkout's `target/` directory through `CARGO_TARGET_DIR`.

## Models, efforts, and role routing

An agent specification has exactly three nonempty colon-separated parts:

```text
provider:model:effort
```

Providers are `claude` and `codex`. Rustflow trims whitespace around each part and validates every configured difficulty route, including routes unused by the current task. Models must occur in `[models]` for their provider; `"*"` permits any model name. Efforts must occur in `[efforts]`; there is no effort wildcard. An allowlisted name is only accepted by Rustflow's validator; it does not establish provider availability or account access.

| Table key | Default array |
| --- | --- |
| `models.claude` | `["haiku", "sonnet", "opus", "fable"]` |
| `models.codex` | `["gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"]` |
| `efforts.claude` | `["low", "medium", "high", "xhigh", "max"]` |
| `efforts.codex` | `["low", "medium", "high", "xhigh", "max"]` |

Each of the following role tables contains `easy`, `medium`, and `hard` routes:

| Role table | `easy` | `medium` | `hard` | `timeout_min` | `max_budget_usd` |
| --- | --- | --- | --- | --- | --- |
| `[roles.planner]` | `claude:sonnet:high` | `claude:opus:high` | `claude:opus:xhigh` | `20` | `3.0` |
| `[roles.implementer]` | `claude:sonnet:medium` | `claude:opus:high` | `claude:opus:xhigh` | `60` | `15.0` |
| `[roles.reviewer_a]` | `claude:sonnet:high` | `claude:opus:high` | `claude:opus:xhigh` | `30` | `5.0` |
| `[roles.reviewer_b]` | `codex:gpt-6.1-sol:medium` | `codex:gpt-6-astra:high` | `codex:gpt-6-astra:xhigh` | `30` | unset |
| `[roles.fixer]` | `claude:sonnet:high` | `claude:opus:high` | `claude:opus:xhigh` | `45` | `10.0` |

Timeouts bound individual provider attempts, subject to the remaining run deadline. `max_budget_usd` is sent as Claude's per-call `--max-budget-usd`; it has no effect for Codex. Changing `reviewer_b` to Claude does not automatically give it a per-call dollar cap: add that key explicitly.

Additional role options:

| Key | Default | Behavior |
| --- | --- | --- |
| `roles.planner.when` | `"hard"` | `hard`, `always`, or `never`; also subject to `--no-plan` and review mode. A failed planner is logged and implementation continues without its plan. |
| `roles.reviewer_a.lens` | `"correctness"` | Reviewer emphasis; allowed values are `correctness`, `regressions`, `design`, and `audit`. |
| `roles.reviewer_b.lens` | `"regressions"` | Same choices. `design` emphasizes idioms, APIs, evidenced performance concerns, and documentation. |
| `roles.fixer.escalate_on_repeat` | `true` | Raise the fixer's difficulty by one level, capped at `hard`, when blocking findings persist from an earlier round. |

Unsafe, FFI, or atomic risk signals in the diff force review routes to `hard`, independently of the initial triage difficulty.

Example of replacing one provider's routes while keeping the rest:

```toml
[roles.reviewer_b]
easy = "codex:gpt-6.1-sol:high"
medium = "codex:gpt-6.1-sol:xhigh"
hard = "codex:gpt-6-astra:xhigh"
lens = "design"
```

### Optional auditor

`[roles.auditor]` uses one `spec`, rather than difficulty routes.

| Key | Default | Behavior |
| --- | --- | --- |
| `mode` | `"llm"` | `llm` adds a third read-only reviewer in round one when risk signals require escalation. `tob` runs the Trail of Bits plugin once after escalation. |
| `spec` | `""` | Empty disables the automatic LLM auditor; escalation remains visible in the report. A nonempty value uses the same `provider:model:effort` syntax and allowlists. `tob` requires a nonempty Claude spec. |
| `timeout_min` | `40` | Auditor attempt timeout. |
| `max_budget_usd` | `10.0` | Claude per-call cap. |
| `tob_args` | `"threat model REMOTE, use sonnet, high severity only"` | Text passed to `/rust-review:rust-review` before Rustflow appends the risky-file scope. |
| `tob_allowed_tools` | Explicit plugin tool list in the [template](../rustflow/templates/rustflow.toml) | Overrides normal write-role allowed tools for the plugin session. Includes delegation and report-writing tools plus selected shell commands. |

`tob` requires plugin installation, available through `rustflow init --with-plugins`. It runs a write-capable Claude session so the plugin can write its result directory, but instructs it to leave source and Git state alone. Rustflow stops with `audit_modified_tree` if the source snapshot changes. The plugin's report is attached for human review; it is not parsed into the ordinary finding aggregation protocol. See [Architecture](architecture.md) for escalation behavior.

The default `tob_allowed_tools` array is:

```toml
tob_allowed_tools = [
  "Agent", "Read", "Write", "Edit", "Glob", "Grep", "TaskCreate", "TaskUpdate", "TaskList",
  "Bash(rg *)", "Bash(grep *)", "Bash(find *)", "Bash(test *)", "Bash(ls *)",
  "Bash(wc *)", "Bash(mkdir *)", "Bash(date *)", "Bash(python3 *)", "Bash(python *)",
  "Bash(uv *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
  "Bash(git rev-parse *)", "Bash(cargo metadata *)", "Bash(cargo tree *)",
]
```

## Difficulty triage

| Key in `[triage]` | Default | Behavior |
| --- | --- | --- |
| `mode` | `"heuristic"` | `heuristic`: local score only. `llm`: use a successful model classification. `hybrid`: choose the harder of local and model classifications. Failed model triage retains the heuristic result. |
| `model` | `"claude:haiku:low"` | Model for `llm` and `hybrid`, validated against allowlists when those modes are selected. |
| `easy_max` | `1` | Score at or below this threshold is `easy`. |
| `medium_max` | `4` | Scores above `easy_max` and at or below this value are `medium`; higher scores are `hard`. |

Heuristic signals include unsafe/FFI, concurrency, lifetimes/macros, architecture changes, performance, persistence, and error handling, in Polish and English. Prompt length, named Rust files, requirement bullets, and workspace size add signals. Review mode also considers diff size and risk signals. See [triage.py](../rustflow/triage.py) for weights and matching rules.

`rustflow triage` is a local preview: it does not call a provider even when configured for `llm` or `hybrid`. `run` and `review` perform model triage when enabled. `--difficulty easy|medium|hard` bypasses automatic triage during a run.

Model triage uses the generic 30-minute role timeout and has no packaged per-call dollar cap. The generic role-option lookup also accepts `[roles.triage] timeout_min` and `max_budget_usd` when an explicit override is needed; the latter applies only to Claude. Triage still consumes the shared run budgets.

## Review and repair loop

| Key in `[loop]` | Default | Behavior |
| --- | --- | --- |
| `max_review_rounds` | `3` | Maximum review rounds, including the first review; must be at least one. A remaining blocker at the limit ends with `max_rounds`. |
| `max_gate_fix_attempts` | `2` | Maximum fixer attempts for a gate failure before stopping. |
| `block_on` | `["P0", "P1"]` | Finding severities that can block. Allowed entries: `P0`, `P1`, `P2`, `P3`. Other severities remain advisory. Mechanical gate/acceptance failures are handled separately. |
| `rereview` | `"both"` | `both` reruns both reviewers; `first` runs only `reviewer_a` from round two onward. |
| `allow_reject` | `true` | Allow the fixer to reject a finding with a reason through the resolution/dispute protocol. |
| `min_confidence_single` | `"medium"` | Single-reviewer findings below this confidence become advisory. Choices: `low`, `medium`, `high`. |

Setting `block_on = ["P0", "P1", "P2"]` makes ordinary P2 findings eligible to block, while confidence, anchoring, and dispute rules still apply. Review without `--fix` reports changes requested after the first round instead of entering the fix loop. The loop can also stop for lack of progress before reaching the round limit.

### Review context and advisory tools

| Key in `[review]` | Default | Behavior |
| --- | --- | --- |
| `inline_diff_max_kb` | `80` | Maximum UTF-8 diff size attached directly to review prompts; larger diffs must be read through Git tools. Also caps the rereview delta. Units are 1024 bytes. |
| `rereview_full_diff_max_kb` | `16` | From round two, omit a larger full diff when the delta fits inline. `0` omits any nonempty full diff under that condition. If the delta itself is too large, the ordinary full-diff limit still applies. |
| `advisory_clippy` | `["clippy::pedantic"]` | Extra Clippy lint groups, filtered to changed lines and attached as advisory context. `[]` disables these extra lints. |
| `advisory_every_round` | `false` (implicit) | Repeat advisory collection after round one. This supported key is absent from the packaged template. |
| `advisory` | One disabled rust-analyzer entry | Array of command definitions below. Replacing it replaces the entire array. |

The default advisory entry is:

```toml
[[review.advisory]]
name = "rust-analyzer diagnostics"
cmd = ["rust-analyzer", "diagnostics", "."]
enabled = false
timeout_s = 600
```

Each enabled entry executes its `cmd` argument array from the repository working checkout. `name` defaults to `"tool"` when formatting output, and `timeout_s` defaults to `600`. Output lines mentioning a changed file are attached, up to 40 lines per command; custom advisory output is matched by filename, not parsed into line-level diagnostics. Missing executables are logged and skipped. Advisory findings and nonzero command exits do not directly block, but incomplete process output or an execution limit can stop the run. Commands are executed directly, without a shell; use argument arrays rather than shell pipelines.

## Execution budgets and output limits

| Key | Default | Behavior |
| --- | --- | --- |
| `budget.max_agent_calls` | `40` | Total physical provider attempts, including retries, JSON repair, triage, and audit. Positive integer. |
| `budget.max_active_seconds` | `7200` | Active wall time across the initial invocation and all resumes. Positive finite number. Includes gates, provider calls, and retry waits; parallel agents share the same elapsed time. Time while the run is stopped is excluded. |
| `budget.max_total_usd` | `60.0` | Stop admitting further logical agent calls when the recorded Claude cost has reached this threshold. `0` disables this dollar check. Codex token usage is not converted to dollars. |
| `process.max_output_bytes` | `16777216` (16 MiB) | Combined raw stdout and stderr limit per process. Positive integer. An overflow produces incomplete output and stops processing that result. Codex's last-message artifact is also bounded. |

The dollar threshold is checked before a logical call, not reserved atomically across concurrent calls or every internal retry/repair. It can be exceeded by an admitted call or parallel calls. Claude per-role caps provide another bound; costs from a timed-out Claude call with no usable cost report are estimated using its role cap. This is a Claude accounting control, not a provider-wide billing guarantee. Cost reports distinguish measured, estimated, and missing amounts.

Execution limits are saved with the run. On resume, existing saved limits remain authoritative; use `--max-agent-calls`, `--max-active-seconds`, or `--max-output-bytes` to raise them. They cannot be lowered on resume, and changing TOML alone does not reset the spent allowance. Started attempts still count after a crash. Recovery adds one estimated heartbeat second for an interrupted active session. See [Usage](usage.md) for resume examples.

## Provider settings

Both providers accept `bin` and `extra_args`. `bin` selects an executable name or path, resolved without a shell; `.py` wrappers and Windows npm shims receive compatibility handling. `extra_args` is an argument array appended to the generated command. It is an escape hatch to your installed CLI; Rustflow does not validate the semantics of these extra flags.

Authentication belongs to the provider CLI and its environment. Rustflow does not provide TOML API-key fields.

### Claude Code: `[providers.claude]`

| Key | Default | Effect |
| --- | --- | --- |
| `bin` | `"claude"` | Provider executable. |
| `permission_mode_write` | `"acceptEdits"` | `--permission-mode` for write-capable calls. |
| `permission_mode_read` | `"dontAsk"` | `--permission-mode` for read-only calls. |
| `allowed_tools_write` | See lists below | Tools preapproved with `--allowedTools` for write roles. |
| `allowed_tools_read` | See lists below | Tools preapproved for read roles. |
| `disallowed_tools_read` | `["Edit", "Write", "NotebookEdit", "Agent"]` | Explicit `--disallowedTools` for read roles. |
| `tools_read` | `["Read", "Glob", "Grep", "LSP", "Bash"]` | Built-in definitions exposed with `--tools`. Empty omits the flag. |
| `tools_write` | `["Read", "Edit", "Write", "Glob", "Grep", "LSP", "Bash"]` | Same for write roles. Custom allowed-tool calls such as ToB bypass this restriction. Windows adds PowerShell. |
| `readonly_shell` | `true` | Add selected read-only shell rules to ordinary write-role permissions: `ls`, `wc`, `cat`, `head`, `tail`, `grep`, `rg`, `sort`, `uniq`, `cut`, `diff`, `tree`, and `pwd`, plus RTK forms. |
| `fallback_model` | `""` | Empty omits `--fallback-model`; otherwise pass the configured fallback model(s), validated against the Claude model allowlist. Whitespace is removed. Observed reroutes can produce a `model_mismatch` warning. |
| `repair_structured_output` | `true` | One extra turn in the same Claude session after a qualifying structured-output failure. Uses the same tools and schema, with a timeout capped at 10 minutes. Counts as another physical attempt. |
| `skill_invocation` | `"slash"` | `slash` invokes the role as `/skill`; `inline` embeds its `SKILL.md` body. |
| `extra_skills_via` | `"system"` | `system` attaches always-on skills using `--append-system-prompt-file`; `inline` includes them in the task prompt. |
| `exclude_dynamic_system_prompt` | `true` | Pass `--exclude-dynamic-system-prompt-sections` to keep per-run context out of the dynamic system sections. Requires CLI support for that flag. |
| `rtk_compat` | `true` | Add `Bash(rtk …)` twins to Bash permission rules for compatibility with RTK command rewriting. |
| `strict_mcp` | `true` | Pass `--strict-mcp-config`, excluding project MCP servers from orchestrated Claude calls. |
| `setting_sources` | `""` | Empty omits `--setting-sources`. Otherwise a comma-separated subset of `user,project,local`, with whitespace removed. `"project,local"` excludes user settings/hooks; authentication configured only in user settings may then be unavailable. |
| `extra_args` | `[]` | Additional CLI arguments. |

The exact default preapproval lists are:

```toml
allowed_tools_write = [
  "Read", "Edit", "Write", "Glob", "Grep", "LSP",
  "Bash(cargo *)", "Bash(git diff *)", "Bash(git status *)",
  "Bash(git log *)", "Bash(git show *)",
  "Bash(python .rustflow/gates.py *)", "Bash(python3 .rustflow/gates.py *)",
  "Bash(py -3 .rustflow/gates.py *)",
]
allowed_tools_read = [
  "Read", "Glob", "Grep", "LSP",
  "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git blame *)",
  "Bash(cargo tree *)", "Bash(cargo metadata *)",
]
```

Write calls also receive a rule for the configured Python gates command, its PowerShell counterpart, and `PowerShell(cargo *)`. Tool visibility (`tools_*`) and preapproval (`allowed_tools_*`) serve different purposes; increasing one does not automatically change the other. Hooks additionally enforce role-specific restrictions.

### Codex: `[providers.codex]`

| Key | Default | Effect |
| --- | --- | --- |
| `bin` | `"codex"` | Provider executable. |
| `network` | `false` | For write calls, request `sandbox_workspace_write.network_access=true`. Does not change read-only calls. |
| `add_dirs` | `[]` | Additional writable directories for write calls, passed as `--add-dir`; `~` is expanded. The shared `CARGO_TARGET_DIR` is added automatically for worktree runs. |
| `skill_invocation` | `"mention"` | `mention` invokes the role as `$skill`; `inline` embeds the role skill body. |
| `extra_args` | `[]` | Additional CLI arguments. |

Codex uses `exec`, `workspace-write` for write roles and `read-only` for read roles. The configured model and effort become `-m` and `model_reasoning_effort`. It emits JSON events plus a last-message artifact; Rustflow records token usage when provided, but has no Codex dollar-cap flag.

## Always-on skills

Role skills (`rustflow-plan`, `rustflow-implement`, `rustflow-review`, `rustflow-fix`) are separate from the additional skills configured here.

| Key in `[skills]` | Default | Applies to |
| --- | --- | --- |
| `all` | `["auto"]` | Every mapped role below. |
| `planner` | `[]` | Planner. |
| `implementer` | `[]` | Implementer. |
| `reviewer` | `[]` | `reviewer_a`, `reviewer_b`, the LLM auditor, and the installed interactive reviewer. |
| `fixer` | `[]` | Fixer. |
| `max_kb` | `64` | Combined size of selected extra `SKILL.md` files per role/provider, in 1024-byte units. Exceeding it is an error. |

Names resolve under `.claude/skills/<name>/SKILL.md` for Claude and `.agents/skills/<name>/SKILL.md` for Codex. `all` and the role group are concatenated, then duplicate names are removed. `auto` selects installed `rustflow-stack-*` skills plus `rust-skills` and optional `rust-best-practices`; it does not select arbitrary installed skills or duplicate the role skill. An explicitly named missing skill is an error; an absent auto-discovered skill is simply absent. Preflight checks role/provider combinations across all difficulty routes.

Only `SKILL.md` is attached. Reference files remain on disk, with their location included for agents to open as needed. Claude defaults to a per-role system-prompt file, Codex always receives extras inline, and `init` embeds interactive reviewer preloads in its generated definition. Rerun `init` to regenerate that definition after changing its preload configuration. Triage and the `tob_audit` role have no extra-skill group mapping.

```toml
[skills]
all = ["auto", "my-rust-conventions"]
reviewer = ["review-checklist"]
max_kb = 96
```

Both provider skill directories must contain explicitly requested skills when those providers appear in the configured routes. See [skills.py](../rustflow/skills.py) for resolution and bundling.

## Deterministic gates

Gates are shared by hooks, orchestration, and CI. Their configuration comes from `rustflow.toml` as described under [Loading and precedence](#loading-and-precedence), but the source of that file depends on the caller: orchestrator runs read `[gates]` from the run's base commit (also on resume), and interactive hooks read it from `HEAD`. Editing `[gates]` in the working tree does not change the gates of a running task or the Stop hook until the change is committed.

| Key in `[gates]` | Default | Behavior |
| --- | --- | --- |
| `cargo_dir` | `"."` (implicit) | Cargo workspace directory relative to project root. Supported by the runner even though absent from the template. |
| `features` | `["--all-features"]` | Cargo argument array added to check, Clippy, tests, doctests, and SQLx preparation. Use `[]` or selected features if all features are incompatible. |
| `tests` | `"auto"` | `auto` selects nextest if installed, otherwise `cargo test`. `nextest` requires nextest execution; `test` uses Cargo's built-in runner; `off` skips the test step. |
| `doc_tests` | `true` | Separate `cargo test --workspace --doc` during full gates. Binary-only workspaces skip when Cargo reports no library targets. |
| `deny` | `"auto"` | `auto`, `always`, or `off`; details below. |
| `clippy_args` | `["-D", "warnings"]` | Arguments after Clippy's `--`. Replacing with `[]` removes the default deny-warnings policy. |
| `edit_check` | `true` | Run `cargo check` in edit profile and after relevant interactive edits. |
| `edit_fmt` | `"file"` | Post-edit hook: `file` formats the edited Rust file, `all` uses configured gate formatting scope, `off` skips post-edit formatting. Does not disable formatting in ordinary gate profiles. |
| `fmt_fix_scope` | `"changed"` | `changed`, `touched`, or `all`; details below. |
| `stop_max_blocks` | `3` | Consecutive failures the Stop hook blocks before allowing the session to end with a red-gates notice. Does not turn a failed orchestrator gate into a pass. |
| `tree_stable` | `true` | Full gates fail if non-runtime source files change during execution. An unlocked run may generate/update `Cargo.lock`; strict mode treats changes as failures. |
| `log_lines` | `60` | Tail length retained for otherwise unparsed failed-step output. Full logs are separate artifacts. |
| `semver` | `"auto"` | `auto`, `always`, or `off`; details below. |
| `sqlx` | `"auto"` | `auto`, `require`, or `off`; details below. |
| `extra` | `[]` (implicit) | Additional gate command definitions. |
| `integrity_extra_paths` | `[]` (implicit) | List of globs. Matching changed files are treated as gate configuration (kind `config`) and need a proof; see below. |

There is no general gate-timeout TOML key. Most built-in gate commands use a one-hour timeout, subject to the run's remaining deadline; individual helpers have shorter fixed timeouts. Custom commands accept `timeout_s`.

### Gate integrity

Changes to gate configuration can weaken the gates themselves, so the diff between the base and the current tree is scanned. Files named in the built-in list (`rustflow.toml`, `.cargo/config` and `.cargo/config.toml` at any depth, `.config/nextest.toml`, `clippy.toml`, `.clippy.toml`, `rustfmt.toml`, `.rustfmt.toml`, `rust-toolchain`, `rust-toolchain.toml`, `deny.toml`) and paths matching `integrity_extra_paths` are classified as `config`. Each such change needs a proof: the gates are re-run in a temporary worktree with the changed configuration restored to the base version. A red proof blocks the change. Projects without the key keep the default `[]`.

```toml
[gates]
integrity_extra_paths = ["ci/*.sh"]
```

The changes are described in [usage](usage.md#gate-affecting-changes).

### Profiles and strict mode

| Profile | Steps |
| --- | --- |
| `edit` | Formatting and, when `edit_check` is enabled, `cargo check`. |
| `stop` | Formatting, Clippy, SQLx, tests, then matching extra gates. |
| `full` | Stop steps plus doctests, cargo-deny, semver checks, then matching extra gates. |

The post-edit hook has its own file-specific formatting/check path. Extra gates are evaluated for `stop` and `full`; the current `edit` profile returns before extras. Compile errors in Clippy short-circuit subsequent checks to avoid duplicate compiler output.

Ordinary gates may format files. `--check` makes formatting check-only and adds `--locked` where supported if `Cargo.lock` exists. `--ci` enables strict mode, runs eligible cargo-deny regardless of manifest changes, and ignores custom gate `when_env` skipping. Orchestrator baseline and final checks are strict. A passing cached gate does not replace the final strict verification.

### Formatting scope

`fmt_fix_scope = "changed"` formats changed/new `.rs` files relative to `RUSTFLOW_BASE` or `HEAD`, including untracked files. It skips existing files whose base revision was already unformatted, avoiding a whole-file reformat in a focused change. `touched` formats every changed Rust file in full, including previously unformatted files. `all` runs `cargo fmt --all`.

These are write-mode scopes. Check-only formatting still checks the whole workspace and can report untouched formatting debt. The post-edit `file` path formats through stdin so `mod` declarations do not cause child modules to be rewritten; it respects the base-formatting exclusion for `changed`.

### Optional tool gates

* **cargo-deny:** Requires `deny.toml` in `cargo_dir`. `auto` runs when manifests/lockfiles changed relative to `RUSTFLOW_BASE` or in the working tree; CI runs it regardless of those changes. `always` bypasses the change check. Both modes still skip if configuration or cargo-deny is missing; `always` does not enforce tool installation.
* **SQLx:** Looks for supported SQLx query macros in Rust source. Without macros it skips, even in `require` mode. With macros, `auto` skips when a database URL or sqlx-cli is missing; `require` makes those missing prerequisites fail. `REQUIRE_SQLX_CHECK=1` also requires them unless `sqlx = "off"`. The URL is read from `DATABASE_URL`, otherwise a simple `DATABASE_URL=...` line in `cargo_dir/.env`. The command is `cargo sqlx prepare --workspace --check -- --all-targets` plus configured features; it needs a usable database/schema.
* **Semver:** Applies to publishable library/proc-macro workspace packages (`publish = false` packages are excluded). `auto` skips without cargo-semver-checks; `always` fails if the tool is missing, but still skips a workspace with no eligible libraries. When `RUSTFLOW_BASE` exists, it supplies `--baseline-rev`; otherwise the tool's own baseline behavior applies. `off` skips the step.

### Extra gates

```toml
[[gates.extra]]
name = "migration check"
cmd = ["sqlx", "migrate", "run"]
profiles = ["full"]
when_env = "DATABASE_URL"
timeout_s = 1800
hint = "Check the local test database and migrations."
```

`cmd` is an argument array executed from `cargo_dir`, without a shell. A nonzero exit fails the gate. `name` defaults to `"extra"`, `profiles` to `["full"]`, `timeout_s` to `1800`, and `hint` to an empty string. `when_env` optionally skips the command when the named environment variable is empty; CI deliberately runs it anyway. This tests the process environment, not the `.env` parser used for SQLx discovery. Extra gate definitions must provide a usable command.

## Environment variables

Rustflow inherits the ordinary process environment for tools, authentication, and build configuration; it does not turn arbitrary environment names into TOML overrides. The following variables are explicitly read or set by the implementation.

| Variable | Meaning |
| --- | --- |
| `CLAUDE_PROJECT_DIR` | Hook/gate project-root hint when it names an existing directory; MCP root discovery also starts here. Set by Claude Code. Removed from inherited agent session identity before Rustflow launches a fresh provider process. |
| `RUSTFLOW_BASE` | Comparison revision for changed-file formatting, manifest-change detection, and semver baseline. Set to the resolved run base for orchestrated agents and gates. Standalone formatting otherwise uses `HEAD`. |
| `RUSTFLOW_RUN` | Current run ID, set for agents; hooks use it to match accepted baseline failures to the correct run. |
| `RUSTFLOW_ROLE` | Role set for provider processes and used by hooks for read-only/write/audit behavior and active-run protection. Intended as run context rather than a user configuration override. |
| `RUSTFLOW_HOOKS` | `0` bypasses Rustflow post-edit and Stop gate hooks. It does not disable orchestrator/CI gates or every guard hook. |
| `RUST_EDIT_CHECK` | `0` skips only the post-edit `cargo check`; formatting and Stop checks are separate. |
| `RUST_GATE_SKIP` | Set to `1` for orchestrated agents to avoid duplicate rust-review-kit gates. This is an integration variable, not a Rustflow gate override. |
| `CARGO_TARGET_DIR` | Set to the main checkout's `target/` in worktree mode. Forwarded to agents and gates; automatically added to writable Codex directories for write calls. |
| `DATABASE_URL` | SQLx database URL; also inspected by migration guards. SQLx gate discovery falls back to `cargo_dir/.env` when unset. |
| `REQUIRE_SQLX_CHECK` | `1` requires SQLx prerequisites when macros are present, subject to `sqlx != "off"`. |
| `CLAUDE_CONFIG_DIR` | Read by `doctor` when locating `.claude.json` for folder-trust checks. |
| `RUSTFLOW_PRIVATE_CONSOLE` | Internal Windows detached-worker marker used for process/cancellation handling; fresh provider sessions scrub it. |
| `CARGO_TERM_COLOR` | Gate subprocesses set it to `never`. |
| `RUST_BACKTRACE` | Gate subprocesses set it to `0`. |
| `PYTHONIOENCODING` | Provider subprocess wrapper supplies `utf-8` only when it is not already set. |

`PATH` controls executable discovery. Provider authentication variables, including `ANTHROPIC_*`, `OPENAI_*`, and `CLAUDE_CODE_USE_BEDROCK`, remain inherited; Rustflow neither validates nor interprets their provider-specific values.

Fresh provider subprocesses scrub these parent-session variables to prevent accidental session reuse or inherited sandbox identity:

```text
CLAUDECODE
CLAUDE_CODE_SESSION_ID
CLAUDE_CODE_REMOTE_SESSION_ID
CLAUDE_CODE_CHILD_SESSION
CLAUDE_CODE_ENTRYPOINT
CLAUDE_CODE_SESSION_ATTENDED
CLAUDE_CODE_SESSION_ORIGIN
CLAUDE_PID
CLAUDE_EFFORT
CLAUDE_PROJECT_DIR
CLAUDE_AFTER_LAST_COMPACT
CLAUDE_CODE_MESSAGING_SOCKET
CLAUDE_CODE_MESSAGING_TOKEN
CODEX_THREAD_ID
CODEX_SANDBOX
CODEX_SANDBOX_NETWORK_DISABLED
RUSTFLOW_ROLE
RUSTFLOW_PRIVATE_CONSOLE
```

Rustflow then supplies the current role/run context explicitly. See [procutil.py](../rustflow/procutil.py), [hooks.py](../rustflow/hooks.py), and [the provider runners](../rustflow/agents.py) for the environment and CLI boundaries. For contributor checks, see [Development](development.md).
