---
name: rustflow-plan
description: Read-only planning of a non-trivial Rust change - explore the code, choose the smallest sound design, and produce ordered steps plus testable acceptance criteria. Use before implementing hard changes (unsafe, concurrency, cross-crate refactors, public API) and when invoked by the rustflow orchestrator as the planner.
---

# Planning a Rust change (read-only)

1. Map the terrain: workspace layout, the crates/modules involved, public API surface, error types,
   feature flags, existing tests and test utilities, MSRV and lint configuration.
2. Identify the hard parts early: ownership/lifetimes across boundaries, async/cancellation, shared
   state and lock ordering, `unsafe`/FFI invariants, semver impact, migrations, performance claims
   that need a benchmark.
3. Choose the smallest design that satisfies the task. Prefer types that make invalid states
   unrepresentable over runtime checks; prefer extending existing abstractions over new ones.
4. Write the plan:
   - **steps** - ordered, each naming files/items; tests come before or with the code they cover;
   - **acceptance_criteria** - observable and testable ("`parse("")` returns `Err(ParseError::Empty)`",
     "no `std::sync::Mutex` guard is held across `.await` in `Worker::run`"). Give each criterion a
     `test`: the exact path of the one test that will prove it (`parser::tests::empty_input_is_error`).
     The implementer must create a test with exactly that name and the orchestrator checks that it ran
     and passed - so name a test only when a unit/integration test can really prove the criterion,
     otherwise `""` (reviewers then judge it);
   - **risks** and **open_questions** - what a reviewer should look at hardest; questions you could not
     settle from the code (the implementer will choose the conservative option).
5. Do not edit files. Return only the JSON object required by the schema.
