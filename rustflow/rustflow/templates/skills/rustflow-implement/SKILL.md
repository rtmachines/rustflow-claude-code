---
name: rustflow-implement
description: Disciplined procedure for implementing a Rust change (feature, bugfix, refactor) in this repository - understand, test-first, minimal diff, green gates. Use when asked to implement or change Rust code, and always when invoked by the rustflow orchestrator as the implementer.
---

# Implementing a Rust change

## 1. Understand before typing
- Read `AGENTS.md` / `CLAUDE.md` and `REVIEW.md` (severity matrix + project rules).
- Locate the code: `Cargo.toml` (workspace members, features, MSRV, lints), the modules involved,
  their callers and the existing tests. Use the LSP (go-to-definition, references) when available.
- Restate the task as acceptance criteria for yourself: observable behavior, errors, edge cases.
- A criterion with a required test name is checked mechanically: the test must exist under exactly
  that path (module::...::name), run (not `#[ignore]`d, not filtered out) and pass.
  If an approved plan or criteria were given, they are the contract.

## 2. Tests first for behavior
- For every new behavior or bug: write the failing test first (unit test in the module's
  `#[cfg(test)] mod tests`, integration test in `tests/` for public API).
- Cover edge cases: empty input, boundaries, invalid input -> error variant, concurrency where relevant.
- Tests assert behavior, not implementation details. No sleeps for synchronization.

## 3. Implement with Rust discipline
- Ownership: borrow (`&str`, `&[T]`, `impl AsRef<..>`) instead of cloning; clone only with a reason.
- Errors: libraries return typed errors (`thiserror`), binaries may use `anyhow`; propagate with `?`
  and add context; never swallow (`let _ =`, `.ok()` without handling).
- No `unwrap`/`expect` on recoverable production paths; `expect("why this cannot fail")` only for
  proven invariants.
- Async: no blocking I/O and no `std::sync::Mutex` guard across `.await`; use `spawn_blocking`,
  `tokio::sync` primitives; `select!` branches must be cancel-safe.
- `unsafe`: avoid; if unavoidable, minimal scope + `// SAFETY:` comment with the invariant and a test.
- Public API: doc comments with an example for new public items; mind semver.
- Keep the diff focused. No drive-by refactors, renames or new dependencies unless required
  (and then justify it in your notes).

## 4. Verify - the gates are the definition of done
```
python .rustflow/gates.py run --profile stop     # fmt, clippy -D warnings, tests  (python3 on Linux)
python .rustflow/gates.py run --profile full     # + doc tests, cargo-deny on manifest changes
```
- Hooks also run `cargo check` after each edit and the gates when you stop; read their output and fix
  the root cause.
- Forbidden ways to "pass": `#[allow(..)]`, `#[ignore]`, deleting/weakening tests or assertions,
  `cfg`-ing code out, editing lint configuration.

## 5. Finish
- Do not commit or touch git state when running under rustflow (it checkpoints for you).
- Report: summary, files changed, tests added, whether gates are green, notes (deviations, risks,
  follow-ups). Be factual - reviewers will check every claim.
