---
name: rustflow-pr-review
description: Interactive review of the Rust changes on the current branch inside this Claude Code session - forked read-only reviewer, deterministic gates in check-only mode, REVIEW.md P0-P3 matrix, escalation to a soundness audit for unsafe/FFI. Use whenever the user asks to review Rust changes, a Rust PR or a module, e.g. "zrób review", "review this PR".
argument-hint: "[base-ref, default origin/main -> main -> master]"
context: fork
agent: rustflow-reviewer
background: false
allowed-tools: Bash({{PY}} .rustflow/hooks.py review-context *) Bash({{PY}} .rustflow/hooks.py review-context) PowerShell({{PY}} .rustflow/hooks.py review-context *) PowerShell({{PY}} .rustflow/hooks.py review-context)
---

# Rust PR review (interactive)

Base requested by the user (empty = origin/main, then main, then master): $ARGUMENTS

## Change context (generated)

```!
{{PY}} .rustflow/hooks.py review-context $ARGUMENTS
```

## Task

Review exactly this change set following your procedure and report contract.

- Read the diff yourself: `git diff <BASE_SHA> <WORKTREE_TREE>` (both from the context above; this
  includes committed, uncommitted and untracked work). Work file by file for large diffs.
- If the context says the gates are cached GREEN for this tree, do not rerun them.
- If `ESCALATION_SIGNALS` above is non-zero, your verdict must be `ESCALATE` unless every signal is a
  false positive that you explain under "Residual risks".
