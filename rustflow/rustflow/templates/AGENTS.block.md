<!-- rustflow:begin (managed by `rustflow init`; edit outside this block) -->
## Rust workflow (rustflow)

This repository uses **rustflow**: triage -> (plan) -> implement -> gates -> 2 independent reviews ->
fix loop -> final gates. Config: `rustflow.toml`. Review rules and severity matrix: `REVIEW.md`.

- Definition of done = green gates: `python .rustflow/gates.py run --profile stop`
  (`python3` on Linux; `--profile full` adds doc tests and cargo-deny). CI runs `python .rustflow/gates.py ci`.
- Never pass a gate by silencing it: no `#[allow]`, no `#[ignore]`, no weakened or deleted tests,
  no lint-config edits, unless the user explicitly asks.
- Every new behavior gets a test; every bug fix gets a regression test.
- Under the orchestrator (`$RUSTFLOW_ROLE` is set) do not commit or change git state - rustflow
  manages branches and checkpoint commits.
- Role skills: `rustflow-plan`, `rustflow-implement`, `rustflow-review`, `rustflow-fix`
  (`.claude/skills/` for Claude Code, `.agents/skills/` for Codex).
- Reviews are read-only and follow `REVIEW.md` (P0-P3, `file:line`, trigger, impact, rule, fix).
- `unsafe`/FFI/atomics in a change -> verdict ESCALATE -> dedicated soundness audit before merge.
- Driving rustflow from an agent: skill `rustflow` (start with `--detach`, poll `rustflow wait`, follow
  `next_actions`; exit 75 = still running).
<!-- rustflow:end -->
