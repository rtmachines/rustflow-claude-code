---
name: rustflow-fix
description: Procedure for resolving Rust review findings or red quality gates - root-cause fixes, regression tests, explicit per-finding resolution (fixed/partial/rejected with evidence). Use when asked to address review comments or fix failing cargo/clippy/tests, and always when invoked by the rustflow orchestrator as the fixer.
---

# Fixing review findings / red gates

## Per finding
1. Reproduce mentally or with a test: follow the trigger in the finding to the cited `file:line`.
2. Decide:
   - **fixed** - root cause removed; for behavioral bugs add a regression test that fails before the fix.
   - **partial** - part done; say exactly what remains and why.
   - **rejected** - only with concrete evidence (a code path that makes the trigger impossible, an
     existing test, the spec/task wording). Reviewers see your argument and may re-raise it; a second
     disagreement goes to a human, so argue precisely and briefly.
3. Keep fixes minimal and local. Do not rewrite unrelated code, do not reformat whole files by hand.

## Red gates
- Read the first error, not the last; one root cause often produces many diagnostics.
- Compile errors: fix types/borrows at the source; do not add `.clone()` or `'static` just to silence
  the borrow checker without understanding the ownership.
- Clippy: apply the suggested idiom; never `#[allow]` to pass (`#[expect(lint, reason = "...")]` only
  if the project rules allow it and the reason is real).
- Failing tests: decide whether the code or the test is wrong against the TASK - never weaken an
  assertion just to go green.

## Verify and report
```
python .rustflow/gates.py run --profile stop     # python3 on Linux
```
Do not commit. Return the JSON object: summary, resolutions (one per finding id), tests_added.
