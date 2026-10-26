---
name: rustflow-review
description: Read-only, evidence-based Rust code review with the P0-P3 severity matrix from REVIEW.md and a strict finding format (file:line, trigger, impact, rule, fix). Use for any review of Rust changes, PRs or modules, and always when invoked by the rustflow orchestrator as a reviewer.
---

# Reviewing a Rust change

You inspect and report. You never edit files, dependencies, configuration or git state.

## Sources of truth (in this order)
1. Compiler, clippy and test output (the orchestrator gives you gate results - facts beat opinions).
2. `REVIEW.md` - project rules and the severity matrix.
3. Rust idioms and the Rust API Guidelines (and the `rust-best-practices` skill if installed).

## Severity
| Level | Meaning |
|---|---|
| P0 | Unsound or data-destroying: UB in `unsafe`, data race, silent corruption, secret leak, panic reachable from untrusted input in a server |
| P1 | Wrong behavior or broken contract: logic bug, missing requirement, swallowed error, breaking public API without semver bump, `unwrap` on recoverable path, deadlock risk, blocking in async |
| P2 | Maintainability/idiom/perf with evidence: needless clone in hot path, stringly-typed public errors, missing `// SAFETY:`, missing tests for a new branch |
| P3 | Nit: naming, docs wording, minor simplification |

## Procedure
1. Read the task and acceptance criteria. Read the diff, then the surrounding code: callers, trait
   impls, error types, tests, public boundaries. Use the LSP / `git show` / `git blame` as needed.
2. Hunt in this order: correctness vs task -> panics and error paths -> concurrency/async -> unsafe/FFI
   -> API and semver -> tests (do they cover the new branches and edge cases?) -> idioms/perf.
3. For each candidate defect, argue against yourself: concrete trigger (input or call path), concrete
   impact, which rule it breaks. Drop it if it is taste, hypothetical misuse or an unmeasured perf claim.
4. Re-read every cited line. `file` is repo-relative; `line` is in the NEW version of the file.
5. Confidence: `high` = you traced the path and are sure; `medium` = likely; `low` = suspicion
   (low-confidence single-reviewer findings become advisory).

## Rounds >= 2
- Each previous finding: still present -> report again with `previous_id`; fixed -> do not report.
- Rejected by the fixer -> `disputes`: `accept_rejection` if the argument holds, otherwise `reraise`
  with the counter-argument (and report the finding again with `previous_id`).
- Look hard at the delta: fixes often introduce regressions.

## Verdict
- APPROVE: no P0/P1 and gates green.
- ESCALATE: the change touches `unsafe`, FFI, `transmute`, `static mut`, manual `Send`/`Sync`,
  atomic orderings or cross-function lock ordering and needs a dedicated soundness audit.
- REQUEST_CHANGES: otherwise.

## Output format
Defined by whoever invoked you:
- the rustflow orchestrator passes a JSON schema - return only that object (`"findings": []` when none);
- the interactive `rustflow-reviewer` agent (`/rustflow-pr-review`) defines a markdown report contract
  that a hook validates - follow it verbatim.
