<!-- rustflow:begin (managed by `rustflow init`; edit outside this block) -->
@AGENTS.md
@REVIEW.md

Claude Code specifics: hooks in `.claude/settings.json` run `cargo fmt` + `cargo check` after each
edit and the rustflow gates when you stop (you cannot finish while they are red, max 3 blocks).
Use the `rust-analyzer` LSP for navigation and diagnostics.

Interactive reviews (interactive sessions only - an agent launched by the rustflow orchestrator follows
its own prompt and skill instead): by default `/rustflow-pr-review [base]` (forked, read-only
`rustflow-reviewer`, check-only gates, REVIEW.md matrix); when the user asks for rustflow, the full
workflow, independent Claude+Codex reviewers or automatic fixes, use the `rustflow` skill
(`rustflow review`) instead. Do not improvise an ad-hoc review. Verdict `ESCALATE` -> offer the
Trail of Bits audit command it prints (`/rust-review:rust-review ...`, runs in the main session).
To post a finished rustflow report on the PR: `rustflow comment`.

Delegating a whole change: follow the `rustflow` skill (triage -> confirm cost -> start detached ->
`rustflow wait` / `rustflow_wait` -> act on `next_actions`). While a run owns this checkout, hooks deny
your edits and git changes there.
<!-- rustflow:end -->
