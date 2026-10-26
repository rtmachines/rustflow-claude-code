# Rust review rules (project)

Read by every reviewer (rustflow reviewers, `rust-reviewer` agent, `code-review` plugin).
Adapt the project rules to your codebase; keep the severity matrix - the orchestrator's blocking
policy (`[loop].block_on` in rustflow.toml) is defined in terms of it.

## Severity matrix

| Level | Meaning | Examples | Merge? |
|---|---|---|---|
| **P0** | Unsound or data-destroying | UB/memory error in `unsafe`, data race, silent data corruption, secret leak, panic reachable from untrusted input in a server | Never |
| **P1** | Wrong behavior or broken contract | logic bug, unmet requirement, failing test, error swallowed (`let _ =`, `.ok()` without handling), breaking public API without semver bump, `unwrap` on a recoverable production path, deadlock risk, blocking call inside async | Not before fix |
| **P2** | Maintainability / idiom / perf with evidence | unnecessary `clone` in hot path, `Result<T, String>` in public API, missing `// SAFETY:` comment, missing tests for a new branch | Fix or ticket |
| **P3** | Nit | naming, doc wording, minor simplification | Optional |

A finding must have `file.rs:line`, trigger, impact, rule, fix. No taste-only findings.

## Project rules (template - adapt)

- Edition 2024, MSRV in `Cargo.toml` `rust-version`; toolchain pinned in `rust-toolchain.toml`.
- Libraries: typed errors with `thiserror`; `anyhow` only in binaries/tests.
- No `unwrap`/`expect` in production code paths, except proven invariants with an `expect("why this cannot fail")` message.
- Every `unsafe` block has a `// SAFETY:` comment stating the invariant; every `unsafe fn` documents `# Safety`.
- Async: no blocking I/O or `std::sync::Mutex` held across `.await`; `tokio::select!` branches must be cancel-safe or documented.
- Lint suppressions: `#[expect(clippy::x, reason = "...")]`, never crate-wide `#[allow]`.
- New dependency -> justify in PR; must pass `cargo deny check` (licenses, advisories, sources).
- Public API changes in library crates -> `cargo semver-checks` must pass or version is bumped.
- Every new public item has a doc comment; every new behavior has a test.
