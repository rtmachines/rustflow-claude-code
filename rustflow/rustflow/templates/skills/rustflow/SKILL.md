---
name: rustflow
description: How to drive the rustflow orchestrator as an agent - decide when to delegate a Rust change to it, estimate and confirm cost, start it in the background, wait without hitting tool timeouts, and act on its outcome (next_actions). Use when the user asks for a Rust feature, bugfix or refactor that spans more than a trivial local edit, asks for "the full workflow", or asks to review/fix a branch with independent reviewers.
---

# Driving rustflow

**Interfaces.** Prefer the MCP tools when the `rustflow` server is connected: `mcp__rustflow__rustflow_triage`,
`mcp__rustflow__rustflow_start`, `mcp__rustflow__rustflow_wait`, `mcp__rustflow__rustflow_status`,
`mcp__rustflow__rustflow_report`, `mcp__rustflow__rustflow_cancel`, `mcp__rustflow__rustflow_resume`,
`mcp__rustflow__rustflow_gates`. They may be deferred: if you do not see them, load them with ToolSearch
(query `rustflow`) before falling back to the CLI (`rustflow ...` via Bash) - same operations, same output.

rustflow runs: triage -> (plan) -> implement -> deterministic gates -> 2 independent reviews (Claude +
Codex) -> fix loop -> final gates, on its own branch, with its own agents. You are the dispatcher: you
choose, confirm, start, wait, and report. You do not do the work it is doing.

## 1. Delegate or do it yourself?
- **Yourself**: a local, low-risk edit (one function, docs, a rename, a clippy fix). The project hooks
  gate your own work (`cargo check` after each edit, gates when you stop).
- **`rustflow run`**: a feature/bugfix across files, anything touching `unsafe`/FFI/concurrency/public
  API, or when the user wants independent review before merge.
- **Review only**: quick and interactive -> `/rustflow-pr-review [base]` (Claude Code); two model
  families + structured findings + optional fixes -> `rustflow review --base <ref> [--fix]`.

## 2. Triage and confirm
Call `mcp__rustflow__rustflow_triage` or `rustflow triage "<task>" --json`. Show the user difficulty, model
routing and the cost estimate. **Ask before starting** when difficulty is `hard`, the estimate is
unknown, or the estimate exceeds what the user indicated - unless the user already approved a budget
that covers it (then just say what you are starting). Write the task precisely: goal, constraints,
acceptance criteria, files/crates involved - the implementer and reviewers only see this text.
When a criterion can be proven by a test, put it in a criteria file and pass it (`criteria` in
`rustflow_start`, `--criteria FILE` on the CLI): `[[criteria]] text = "...", test = "mod::tests::name"`.
rustflow then requires that exact test to run and pass - a reviewer's "met" is not enough.

## 3. Start in the background (never block on a long run)
- MCP: `mcp__rustflow__rustflow_start {mode: "run"|"review", task, base?, fix?, isolation?}`
- CLI: `rustflow run --detach -f task.md` (or `--detach "<task>"`) -> JSON with `run_id`.
- Use `isolation: "worktree"` if you or the user will keep working in this checkout; the default
  `branch` switches this checkout to `rustflow/<run>` for the duration.
- One run per repo: if a run is active, wait for it or ask the user before `rustflow cancel`.

## 4. Wait correctly
- MCP: `mcp__rustflow__rustflow_wait {run, timeout_s: 600..1800}` (an MCP call has no 10-minute limit; fewer
  waits = fewer turns); CLI: `rustflow wait <run_id> --timeout 540` (stays under the 10-minute Bash limit). Exit code / status: 0 approved, 2 finished not approved,
  3 crashed, **75 still running -> call wait again**.
- While it runs: no edits and no git commands in the run's checkout (hooks will deny them anyway).
  Answer the user, plan, read code - fine.
- Read `rustflow status` / `mcp__rustflow__rustflow_status` (brief) first; open the full report only if needed.

## 5. Act on the outcome - follow `next_actions`
Each summary ends with `next_actions`. On MCP, call the step's `mcp` (`{tool, arguments}`) - the MCP answers
already show it - instead of translating the CLI `command`. Do the ones without `needs_user`; for `ask_user` and
`needs_user` items ask the user with the given question/command. Typical outcomes:
- `approved` / `approved_with_advisories` -> summarize: what changed, tests, cost, branch, advisories;
  offer merge (`git merge --squash`, user decides), PR comment (`rustflow comment`), and the soundness
  audit if `escalate` is true. If `degraded` is non-empty, say plainly that fewer reviewers than planned
  approved it (never "two independent reviewers") and ask before merge whether to review again
  (`rustflow resume <id> --extra-rounds 1 --detach`).
- `criteria_unmet` -> reviewers approved but a required test did not pass on the final tree; show which
  (`T-<id>` findings / `mechanical` in the summary) and ask: fix round (`rustflow resume <id> --extra-rounds 2
  --detach`), fix by hand, or drop the criterion.
- `tree_changed` -> files changed while reviewers worked (someone edited the checkout); make sure nobody
  edits it, then `rustflow resume <id> --detach` (re-runs gates and review).
- `max_rounds` / `stalled` / `final_gates_red` -> list the blocking findings; ask: another fix round of the
  same run (`rustflow resume <id> --extra-rounds 2 --detach`; MCP: `rustflow_resume {run, extra_rounds: 2}` -
  same branch or worktree), fix by hand, or stop.
- `agent_failed` -> a model call failed and changed nothing (overloaded, network, login): if the note says
  transient, `rustflow resume <id> --detach`; otherwise run `rustflow doctor` and tell the user what to fix.
- `gates_red`, `baseline_red`, `budget`, `crashed`, `interrupted` -> follow the action (inspect gates,
  ask about the base, raise budget then `rustflow resume <id> --detach`).
- `approved` with isolation `worktree` -> the merge command runs in the main checkout; afterwards
  `git worktree remove <workdir>` (both need the user).

## Never
- Merge, push, or delete the run branch without the user.
- Edit files in a checkout while a run owns it, or start a second run in the same repo.
- Silence gates (`#[allow]`, `#[ignore]`, `RUSTFLOW_HOOKS=0`) to make a result look green.
