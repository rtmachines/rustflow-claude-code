# Troubleshooting

Start with the run's summary and its recorded reason:

```bash
rustflow status --json
rustflow report --full
rustflow doctor
```

Pass a run ID after `status` or `report` to inspect an earlier run. Omitted IDs select the latest run; a unique partial ID also works. `next_actions` in JSON summaries gives suggested CLI and MCP follow-ups. See [usage](usage.md), [configuration](configuration.md), and [architecture](architecture.md) for the command and implementation details.

## Find the evidence

Artifacts live under `.rustflow/runs/<run-id>/` in the original repository, including when the work happens in a worktree.

| Path | Inspect it for |
| --- | --- |
| `state.json` | Status, phase, round, `stop_reason`, `termination_reason`, failed agent, findings, budget counters, and aggregate usage. |
| `report.md` | Full findings, routing, criteria, per-attempt usage, estimated costs, final gates, and suggested merge steps. |
| `rustflow.log` | Compact phase and agent progress. |
| `console.log` | Detached process stdout/stderr, including startup or crash errors. |
| `task.md`, optional `plan.json` | The original request and successfully parsed plan. |
| `agents/*.prompt.md`, `*.argv.json` | What each provider was asked to do and its exact argument vector. |
| `agents/*.stdout.json`, `*.events.jsonl`, `*.last.txt`, `*.stderr.log`, `*.result.json` | Claude output, Codex event stream/last message, stderr when present, and locally parsed results. Suffixes depend on provider. |
| `attempts/<attempt-id>.json` | Physical initial/retry/repair outcome, usage completeness, capture limits, and unique artifact paths. |
| `gates/<label>.json`, `gates/<label>/*.log` | Parsed steps and raw command output for baseline, post-change, or final checks. |
| `round-<n>.json` | Findings, verdicts, failed reviewers, criteria, and risk signals from one completed review round. |
| `audit/` | Archived optional audit artifacts when applicable. |

For repeated logical names, the short `agents/<name>.*` paths are latest-result views. Use paths in the attempt record to inspect an earlier invocation. A crash before the adapter finished can leave less artifact metadata than a completed attempt.

## Status and exit codes

An agent's `ok` result and a reviewer's `APPROVE` do not independently approve a run. Approval depends on the state machine, accepted gates, finding policy, and required-test evidence.

| Status | Meaning and next step |
| --- | --- |
| `running` | The process is still active. Use `wait`; avoid edits or Git changes in its checkout. |
| `approved` | No open blocking findings, accepted final gates, and required tests proven. Review the report before integrating. |
| `approved_with_advisories` | Approval with advisory or disputed findings; inspect them and any degraded-review warning. |
| `changes_requested` | Report-only review found blocking issues. Start `rustflow review --fix --base <base>` if you want automated fixes. |
| `baseline_red` | The starting implementation baseline fails. Fix it or start a new run with `--accept-red-baseline`. |
| `gates_red` | Gate repair allowance was exhausted. Inspect raw gates and fix the code or start another run. Plain resume does not reopen this finished status. |
| `final_gates_red` | Review approved, but strict final verification failed. Inspect final logs and continue with `--extra-rounds`. |
| `gate_changes_pending` | The run's changes alter gate configuration, lints, silencing attributes or tests in a way that was neither proven harmless nor accepted by both reviewers. The user decides: revert the change (the cleanest choice), or commit a deliberate gate change himself. Then continue with `--extra-rounds`. Never accept it from an automated driver. |
| `criteria_unmet` | Review and final gates were accepted, but named required tests were not proven. Fix/add those tests and continue with `--extra-rounds`. |
| `max_rounds` | Blocking findings remain at the review-round limit. Continue with `--extra-rounds` after assessing them. |
| `stalled` | A fixer made no progress, repeated findings survived, or the tree oscillated. Inspect the dispute and choose a direction before continuing. |
| `budget` | The Claude monetary budget or an execution allowance stopped work. Check `termination_reason` and raise the relevant limit. |
| `incomplete` | Output could not be captured completely, for example overflow, timeout, or a pipe problem. Inspect process metadata, resolve the cause, then resume. |
| `interrupted` | Ctrl+C, cancellation, or a handled termination signal stopped the run. Resume after checking the checkout. |
| `crashed` | An unexpected exception was saved, or status detects a missing PID for a state still marked running. Inspect traceback/console logs, fix the cause, then resume. |
| `tree_changed` | Files changed during review; those review results were discarded. Stop competing edits and resume through gates/review. |
| `agent_failed` | A required agent failed without changing the tree, or no reviewer returned usable output. Correct the cause and resume the phase. |
| `audit_modified_tree` | The optional plugin audit modified source files outside its report directory. Inspect and resolve those changes; this finished status is not plain-resumable. |
| `no_changes` | A successful implementer made no change, or there is no diff to review. Check the request and review base; start a new run if needed. |
| `error` | Preflight or another configuration requirement failed. Read `stop_reason`, correct it, and start a new run. |

`wait` returns 0 for either approved status, 75 while running, 3 for a crash, and 2 for other finished outcomes. `status` and `report` return 0 when they successfully read the run, regardless of its outcome. Foreground run/review normally return 0 for approval, 2 for a nonapproved result, 3 for a caught unexpected exception, and 130 for an interrupt. A detached start returning successfully only means that it launched a process; inspect the eventual status. Plain `resume` of an already finished, nonresumable run can return 0 without changing its status, so check the summary as well as the command's exit code.

## Resume in the same checkout

```bash
rustflow resume <run-id>
rustflow resume <run-id> --detach
rustflow resume <run-id> --extra-rounds 2
```

Plain resume is supported for `budget`, `interrupted`, `crashed`, `tree_changed`, `agent_failed`, and `incomplete` (and a saved `running` state after process loss). `--extra-rounds N` reopens `max_rounds`, `stalled`, `final_gates_red`, `criteria_unmet`, or `gate_changes_pending`; it also allows another review of an approved run whose final round was degraded. The extra allowance continues the same run directory, finding IDs, branch/worktree, checkpoints, and usage history. It does not start an unrelated review checkout.

For branch isolation, switch to the run's recorded branch before resuming:

```bash
git switch rustflow/<run-id>
rustflow resume <run-id>
```

A missing worktree is recreated only when rustflow recorded that it removed the completed clean worktree and the branch remains available. A manually removed in-progress worktree is refused. Finished clean worktrees with committed checkpoints can be removed automatically; worktrees containing uncommitted work are retained. Keep their changes available before attempting manual cleanup.

Resume restores saved run options and keeps saved execution limits. It reconciles finished ledger entries, marks abandoned `started` attempts `interrupted`, and continues the persisted phase. A phase can launch another call if its earlier transition was never saved. Previous attempt artifacts and measurements remain available; the ledger is not a replay engine. A dry-run cannot be resumed into a real run: start a new real run instead.

## Execution limits, timeouts, and output capture

Three limits apply independently of Claude dollar accounting:

| Setting | Default | What consumes it |
| --- | --- | --- |
| `[budget].max_agent_calls` | `40` | Every physical provider invocation, including retries and JSON repair, across resume. |
| `[budget].max_active_seconds` | `7200` | Active wall time of orchestrator sessions, including gates, provider work, and retry waits; excludes time while stopped between sessions. |
| `[process].max_output_bytes` | `16777216` | Combined raw stdout and stderr per subprocess, not a total for the run. Codex's last-message file is also checked against the allowance when read. |

New runs can override these through `--max-agent-calls`, `--max-active-seconds`, and `--max-output-bytes`. Resume accepts the same flags but can only preserve or raise saved limits:

```bash
rustflow resume <run-id> --max-agent-calls 60
rustflow resume <run-id> --max-active-seconds 10800
rustflow resume <run-id> --max-output-bytes 33554432
```

These values are new total allowances, not increments. Editing configuration alone does not replace saved execution limits on resume. Positive integer CLI values are required. After a crash with an open session, active-time accounting adds one second of estimated time beyond the last persisted heartbeat; the estimate is exposed in `execution_budget`.

| `termination_reason` | Diagnosis and action |
| --- | --- |
| `agent_call_limit` | Attempt allowance cannot admit the next call or entire initial reviewer group. Raise `--max-agent-calls`. Group admission can fail with unused slots if all planned initial reviewers do not fit. |
| `active_time_limit` | The shared deadline expired. Raise `--max-active-seconds` after checking where time went. |
| `output_limit` | Combined stdout/stderr or the read Codex last-message artifact exceeded its byte limit. Inspect the retained prefix and command, reduce noisy output or raise `--max-output-bytes`. |
| `timeout` | A command's local timeout elapsed before the run deadline. For agents, inspect `[roles.<role>].timeout_min`; gate/extras timeouts are separate. |
| `pipe_timeout` | A process exited but output pipes stayed open, often because a descendant retained them. Inspect the command and its child-process handling. |
| `pipe_error` | Capturing a pipe failed. Inspect process logs and environment before resuming. |
| `cancelled` / `interrupted` | Cancellation or interruption stopped capture. Confirm the intended run has stopped, then resume when appropriate. |
| `spawn_error` | The OS could not start the command. Check executable resolution, permissions, and the recorded argv/stderr. |

`output_complete = false` means the captured prefix cannot establish success even if it contains valid JSON, passing tests, or a zero exit code. Incomplete reviewer output stops the run before aggregation; it cannot become approval by the other reviewer. Incomplete gate output cannot be accepted as a red baseline, prove required tests, enter the success cache, or be sent into a routine gate-fix loop.

Capture is bounded in raw bytes before UTF-8 decoding. A multibyte character cut at the boundary may display as a replacement character. The executor attempts to kill the process tree on timeout/overflow/cancellation. On Windows it uses `taskkill /F /T`; on POSIX it kills the process group.

## Agent failures and automatic retries

Check `failed_agent`, the attempt's `error`, raw stderr, and `agents/*.argv.json`.

| Symptom | Action |
| --- | --- |
| Not logged in, unauthorized, forbidden, invalid API key | Authenticate the selected provider CLI in this environment, then resume. |
| Unknown/unavailable model or effort | Check the configured role specification and allowlist; run `rustflow doctor`. |
| Unknown option/argument | Check that the installed provider CLI supports the recorded arguments and selected features; update the CLI or compatible provider configuration. |
| Missing executable | Set `[providers.<provider>].bin` or correct `PATH`. A preparation failure before provider invocation has no attempt record. |
| Overloaded service, network issue, missing/invalid output | Eligible failures get one automatic retry. If it still fails, resume after the service recovers. |
| Claude structured-output failure | With repair enabled and a usable session ID, rustflow makes one JSON-only follow-up in that same session. Inspect the `json_repair` record if it fails. |
| Permission-denial warnings | Check role tool lists, provider settings, hook guards, and denied calls in the result. Each denied provider tool request can consume a turn. |
| Requested/observed model mismatch | Inspect `observed_model`, `model_mismatch`, provider fallback configuration, and Claude `modelUsage`. |
| `error_max_budget` / `error_max_turns` | Inspect partial edits and provider result; increase the appropriate provider role allowance where applicable. These are not automatically retried. |

Permanent failures are not retried. Timeouts, incomplete output, provider budget/turn stops, and a failed JSON repair are not automatically retried either. An infrastructure retry waits five seconds and obeys the shared deadline/cancellation. Write roles retry only when the tree stayed unchanged; partial edits instead proceed to gates for assessment.

A single ordinary reviewer failure can result in a degraded review rather than a total stop. `degraded` identifies reviewers planned for the final round that returned no result. This is different from deliberately using `rereview = "first"`. Inspect the failed reviewer's error and, if needed, correct its role budget/configuration and run:

```bash
rustflow resume <run-id> --extra-rounds 1
```

## Monetary budget and missing usage

For a monetary `budget` stop without an execution `termination_reason`, inspect `[budget].max_total_usd` and the booked `cost_usd`. Raise the configured total if appropriate, then resume. Claude role caps are `[roles.<role>].max_budget_usd`; the total is checked before logical calls, while the cap is passed to Claude. Codex does not currently supply measured USD to this accounting.

`cost_usd` includes known costs of failed and successful Claude attempts, plus configured role-cap estimates for Claude timeouts. A timeout can therefore increase booked cost without a measured bill. Retry and JSON repair keep their original call's monetary arguments; no fresh USD reservation is made for them, and parallel calls can already be running when the total is reached.

Use `usage_summary` to distinguish measured cost from estimates. `known` is the measured subtotal; `total = null` means some cost is missing or the historical attempt inventory is incomplete. `missing_attempts` counts only registered attempts missing that measurement. `estimated_cost_usd` is separate. Explicit zero is known; missing/invalid/negative/nonfinite values are unknown. Codex cached input is part of its input total; Claude's separate cache counters must be present for a complete token total.

Old runs can be read without conversion. On resume, previous cost/tokens are preserved in `usage_baseline`; historical completeness remains unknown. Do not interpret `null` as zero or infer missing charges from `missing_attempts` alone. [Architecture: attempt ledger](architecture.md#attempt-ledger-and-usage-semantics) documents the complete field semantics and estimator behavior.

## Red gates and baseline failures

Run the installed gate script for check-only diagnostics, or use CI mode for the CI policy:

```bash
python .rustflow/gates.py run --profile full --check
python .rustflow/gates.py ci
```

The package command `rustflow gates --profile full` normally permits formatting fixes; `rustflow gates --ci` selects strict CI mode. Full profiles include optional checks whose skip reasons matter. `cargo-deny`, SQLx, semver checks, tests, and extras can be skipped based on configuration, tool availability, repository metadata, or environment. A green summary does not mean every optional tool ran.

| Gate issue | Action |
| --- | --- |
| Existing baseline failure | Fix the base first, or start a new implementation run with `--accept-red-baseline`. Only identifiable pre-existing signatures are accepted; new/unitemized failures still block. |
| Base is not rustfmt-clean | Prefer a separate formatting cleanup. With an accepted red baseline, default `fmt_fix_scope = "changed"` avoids formatting files already dirty at the base; `touched` formats touched files even if they were dirty. |
| Clippy compilation failure and skipped tests | Fix compilation first. Tests are intentionally skipped when they would repeat compilation errors. |
| SQLx metadata mismatch | Set a reachable `DATABASE_URL`, run `cargo sqlx prepare --workspace -- --all-targets`, and commit the updated `.sqlx/` metadata as appropriate. Inspect the recorded gate command for feature flags. |
| SQLx missing database/tool | In `auto`, a missing `DATABASE_URL` or sqlx-cli can skip the check. `sqlx = "require"` or `REQUIRE_SQLX_CHECK=1` makes it fail when SQLx macros need checking. |
| Semver failure | Inspect the public API change and version bump against the configured run base. `semver = "always"` also fails if the tool is missing. |
| Standalone semver checks a new unpublished crate | Without `RUSTFLOW_BASE`, the tool chooses its own baseline and may query a registry. For an offline demo use the `stop` profile, or set `[gates].semver = "off"` for full gates; this deliberately skips API compatibility checking. |
| `tree` step failed | A build script, test, code generator, or extra command wrote source files during gates. Put generated output under `target/` or `OUT_DIR`, ignore runtime artifacts, or run generation before gates. |
| Stale-looking successful gate result | Run strict `--check` or `--no-cache` to bypass cache. Required-test checks and final gates already bypass it. |
| Local final passes but CI differs | CI runs environment-conditional extras unconditionally and checks deny policy even when manifests are unchanged; compare tool versions, environment, features, and skip reasons. |

Full gates check tree stability after any intended formatting. Without `--locked`, creation/update of `Cargo.lock` is allowed; other source changes fail the tree check. `[gates].tree_stable = false` disables this check, so use it only when the changed policy is intentional.

## Required tests stay unmet

Inspect the report's acceptance table, `state.mechanical`, and raw test output. The named test must actually run and pass on the current tree. Check spelling, module/binary qualification, enabled features, `#[ignore]`, and whether tests or doctests were disabled or skipped. A successful command that ran no tests cannot satisfy a criterion.

A short name can match several tests; every observed match must pass. Prefer a fully qualified path when names collide. Nextest output supports the test path and binary-qualified forms. Reviewer `met` votes cannot override missing mechanical evidence. Correct the test or criterion and start a suitable new run, or fix/add the required test and continue the existing run with `--extra-rounds`.

## Active lock, cancellation, and external edits

If rustflow reports another active run, use its ID:

```bash
rustflow wait <run-id> --timeout 540
rustflow cancel <run-id>
```

Wait timing out with exit 75 means the run continues; wait again. CLI and MCP waiters keep separate progress-log offsets. Cancellation first sends an interrupt so the orchestrator can kill agents and persist resumable state; if the process remains alive after the grace period, cancellation attempts a forced stop. Inspect the returned `forced` and `interrupt_error` fields and the subsequent effective status.

The active lock lives at `.rustflow/runs/ACTIVE.json` and is validated against PID liveness. Installed hooks prevent the driving agent from editing the active run's checkout, avoid Stop gates on unfinished work, and block recursive rustflow calls from run agents. Do not remove a live run's lock to start competing work.

For `tree_changed`, stop editors, generators, or other processes that modify files during review, then resume. rustflow discards those review results and reruns gates and review against the current tree. For `stalled`, compare the round JSON and fixer resolutions: a no-change response, repeated blockers, and oscillation need different human decisions. Repeated rejected findings can become disputed advisories; inspect them before accepting the result.

## Installation, Windows, and hook checks

- Run `rustflow doctor --selftest` to exercise generated hooks against a temporary crate with the configured interpreter and exec-form invocation. This detects interpreter and shim problems that can otherwise leave hooks inactive.
- On Windows, generated hooks use `py -3` by default and do not require Git Bash. Set a different interpreter with `rustflow init --python-cmd "python"`; mixed-platform teams can use `--local` for local Claude settings.
- Provider npm `.cmd` shims are unwrapped when possible. Inspect `argv.json` and check that `node` and the underlying script are available if startup or quoting fails.
- For `Blocking waiting for file lock`, check whether rust-analyzer is sharing Cargo's build directory. The editor setting `"rust-analyzer.cargo.targetDir": true` gives it a separate target directory.
- If initialization cannot download the default Rust knowledge repository, check GitHub access. Use `rustflow init --no-rust-skills` when intentionally working offline. Existing local knowledge copies are preserved unless forced; older configurations with `[skills].max_kb = 32` should be raised to `64` for the default index.
- An explicitly configured missing always-on skill stops preflight. `rustflow doctor` shows the selected skills per role. Check the provider-specific `.claude/skills/` or `.agents/skills/` installation and `skills.max_kb`.
- If global Claude hooks interfere with child agents, inspect `[providers.claude].setting_sources`. Selecting `"project,local"` excludes user-level settings while retaining project integration.

Recovery behavior is covered by [`test_budget_recovery.py`](../tests/test_budget_recovery.py), [`test_limit_flow.py`](../tests/test_limit_flow.py), [`test_usage_state.py`](../tests/test_usage_state.py), [`test_agent_driver.py`](../tests/test_agent_driver.py), [`test_agent_hardening.py`](../tests/test_agent_hardening.py), [`test_criteria.py`](../tests/test_criteria.py), and [`test_tree_stability.py`](../tests/test_tree_stability.py).
