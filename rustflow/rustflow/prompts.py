"""Role prompts. Models get English instructions; human-readable prose follows [project].language."""

from __future__ import annotations

from dataclasses import dataclass, field

GATES = "python3 .rustflow/gates.py"   # set by the orchestrator from [project].python / platform

LANG_NAMES = {"pl": "Polish", "en": "English", "de": "German"}

LENSES = {
    "correctness": (
        "PRIMARY LENS - correctness & soundness: logic errors against the task, edge cases and "
        "boundary values, panics reachable from inputs (unwrap/expect/index/overflow/slicing), error "
        "propagation (no swallowed errors, `let _ =`, `.ok()`), `unsafe` invariants, async/concurrency "
        "(locks held across `.await`, blocking calls in async, cancel safety of `select!`, races, "
        "deadlocks), resource handling, and whether tests really exercise the new branches."
    ),
    "design": (
        "PRIMARY LENS - design & maintainability: idiomatic Rust (ownership and borrowing, needless "
        "`clone`/allocation, iterator use, `&str`/`&[T]` parameters), API design (types that encode "
        "invariants, typed errors with thiserror in libraries, semver impact of public changes), module "
        "boundaries, performance only with concrete evidence, docs on public items, test quality."
    ),
    "regressions": (
        "PRIMARY LENS - regressions & evidence: what the change breaks for EXISTING callers and data "
        "(changed defaults, error types, ordering, panics on inputs that used to work), boundary values and "
        "empty/huge/unicode inputs, missing or weak tests for each new branch (a test that would pass "
        "without the change proves nothing), resource handling on every path (files, sockets, tasks, locks, "
        "temp dirs), public API and semver impact. Idiom or style remarks only as P3 and only with a concrete "
        "benefit - clippy already covers most of them."
    ),
    "audit": (
        "PRIMARY LENS - soundness audit: every `unsafe` block/fn/impl, FFI boundaries, `repr`, aliasing "
        "and lifetimes of raw pointers, `Send`/`Sync` impls, atomic orderings, lock ordering. State the "
        "invariant each unsafe block relies on and whether the surrounding safe code upholds it."
    ),
}


def _lang(language: str) -> str:
    name = LANG_NAMES.get(language, language)
    return (f"Write all human-readable prose (summary, titles, notes, trigger/impact/fix text) in {name}. "
            "Keep JSON keys, enum values, file paths and code identifiers exactly as specified.")


def _block(title: str, body: str) -> str:
    body = (body or "").strip()
    return f"## {title}\n\n{body}\n" if body else ""


@dataclass
class ReviewContext:
    task: str
    base: str
    head_tree: str
    diff_stat: str
    diff_inline: str | None
    gates_summary: str
    advisory: list[str] = field(default_factory=list)
    risk: str = ""
    acceptance: list[str] = field(default_factory=list)
    round_no: int = 1
    previous: str = ""           # formatted previous blocking findings incl. fixer resolutions
    delta_stat: str = ""
    delta_inline: str | None = None
    prev_tree: str = ""
    gate_changes: str = ""       # pending gate-affecting changes that cannot be proven, formatted for voting


def plan_prompt(task: str, language: str) -> str:
    return f"""{_lang(language)}

Produce an implementation plan for the TASK below in this Rust repository. Do NOT modify any file.
Explore the code you need (Cargo.toml, relevant modules, existing tests, CLAUDE.md/AGENTS.md, REVIEW.md).

Deliver:
- steps: ordered, concrete, each naming the files/items it touches;
- acceptance_criteria: observable, testable statements (behavior, errors, edge cases). For each give `test`:
  the exact path of ONE test that will prove it, e.g. `parser::tests::trims_whitespace` (the implementer
  must create a test with exactly this name; the orchestrator checks that it runs and passes), or ""
  when no unit/integration test can prove it;
- files_to_touch, risks (unsafe, concurrency, API/semver, migrations, performance), open_questions;
- difficulty: your honest estimate (easy | medium | hard).
Prefer the smallest design that satisfies the task. Your final answer is the JSON object only.

{_block("TASK", task)}"""


def criteria_block(criteria: list | None) -> str:
    """criteria: rustflow.criteria.Criterion list."""
    if not criteria:
        return ""
    body = "\n".join(f"- {c.for_prompt()}" for c in criteria)
    if any(c.test for c in criteria):
        body += ("\n\nA named test is mandatory: create it with exactly this path (module::...::name) or keep the "
                 "existing one, make it exercise the criterion, never `#[ignore]` it. The orchestrator runs the "
                 "test suite and fails the criterion if that test did not run and pass.")
    return _block("ACCEPTANCE CRITERIA (reviewers will check each)", body)


def implement_prompt(task: str, language: str, plan: dict | None, baseline: str | None,
                     criteria: list | None = None) -> str:
    parts = [f"""{_lang(language)}

Implement the TASK below in this Rust repository.

Rules:
- Read CLAUDE.md / AGENTS.md and REVIEW.md first; follow project conventions.
- Write or extend tests for every new behavior and edge case (unit tests next to code, integration tests in tests/).
- Keep the diff focused on the task; no drive-by refactors or dependency churn.
- Never silence problems: no `#[allow(...)]` to pass clippy, no `#[ignore]`, no deleted/weakened tests,
  no `unwrap()` on recoverable paths in production code.
- Change gate configuration files (rustflow.toml, .cargo/config*, clippy.toml, rustfmt.toml, deny.toml,
  rust-toolchain, nextest config, `[lints]` in Cargo.toml), add `#[allow]` / `#[expect]` / `#[ignore]`, or remove a
  test ONLY when the task explicitly requires it, and justify each such change in your report. The orchestrator
  detects these changes mechanically, re-runs the gates without them, and blocks the run if they hide a failure.
- Before you finish run the gates yourself: `{GATES} run --profile stop`.
  Finish only when they are green, or explain precisely why they cannot be.
- Do not commit, push, or change git state - the orchestrator handles git.
- Finish with a short plain-text report: what changed (files), tests added, gate status, notes
  (deviations from the plan, risks, follow-ups). Be factual - reviewers will check every claim.
""", _block("TASK", task)]
    if plan:
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.get("steps", [])))
        parts.append(_block("APPROVED PLAN (follow it; deviate only with a reason in notes)", steps))
    parts.append(criteria_block(criteria))
    if baseline:
        parts.append(_block(
            "PRE-EXISTING GATE FAILURES ON THE BASE (not caused by you)",
            "These failed before your change. Do not hide them. Fix them only if they block your task "
            "or are trivially related; mention them in notes.\n\n" + baseline))
    return "\n".join(p for p in parts if p)


def review_prompt(ctx: ReviewContext, lens: str, language: str, reviewer: str) -> str:
    lens_text = LENSES.get(lens, LENSES["correctness"])
    if ctx.diff_inline is not None:
        diff_part = f"```diff\n{ctx.diff_inline}\n```"
    elif ctx.round_no > 1 and ctx.delta_inline is not None:
        diff_part = ("Not inlined in this round: the DELTA above is what changed since the previous review. "
                     f"For context read `git diff {ctx.base} {ctx.head_tree} -- <file>` (read-only) where needed.")
    else:
        diff_part = (f"The diff is large. Read it yourself: `git diff {ctx.base} {ctx.head_tree}` "
                     "(read-only), file by file.")
    parts = [f"""{_lang(language)}

You are reviewer `{reviewer}`. Review the change set of the TASK below. You are READ-ONLY: never edit files,
never change git state, never run commands that write (cargo build artefacts are already produced).
Another reviewer works independently; do not try to be exhaustive on their lens, but do report every
real P0/P1 you notice regardless of lens.

{lens_text}

Method:
1. Read REVIEW.md (severity matrix P0-P3 and project rules) and the TASK.
2. Read the diff, then the surrounding code, callers, tests and public boundaries before judging.
3. For each suspected defect re-derive it adversarially: concrete trigger (input/call path), impact,
   violated rule. Drop taste-only remarks, hypothetical misuse and unmeasured performance claims.
4. Re-read every cited line. `file` is repo-relative, `line` is the line number in the NEW version.
5. acceptance: list every explicit requirement of the TASK (and the acceptance criteria below) with
   met | not_met | unclear; for a listed criterion begin `criterion` with its id, e.g. `[C1] ...`.
   Whether a named required test passes is checked by the orchestrator, not
   by you; your job is whether the code meets the criterion and whether that test really exercises it
   (a test that would pass without the change, or asserts nothing relevant, makes it not_met).
6. verdict: APPROVE only if there is no P0/P1 and gates are green; ESCALATE if the change touches
   unsafe/FFI/atomics/lock ordering that needs a dedicated soundness audit; else REQUEST_CHANGES.

Gate facts beat opinions: gate failures are already known to the orchestrator; do not re-report them
unless you can add the root cause.

Your final answer is the JSON object described by the schema.
""", _block("TASK", ctx.task)]
    if ctx.acceptance:
        parts.append(_block("ACCEPTANCE CRITERIA", "\n".join(f"- {c}" for c in ctx.acceptance)))
    parts.append(_block("CHANGE SET", f"Base: {ctx.base}  ->  working tree {ctx.head_tree}\n\n"
                        f"```\n{ctx.diff_stat.strip()}\n```"))
    if ctx.round_no > 1 and ctx.previous:
        parts.append(_block(
            f"ROUND {ctx.round_no}: PREVIOUS BLOCKING FINDINGS AND THE FIXER'S RESPONSE",
            "For each item: if the problem still exists, report it again with `previous_id` set to its id. "
            "If the fixer REJECTED it, answer in `disputes` with accept_rejection (the argument holds) or "
            "reraise (explain why in note, and also report it with previous_id). Do not report items that "
            "are now fixed. Focus new findings on the delta below, but report any regression.\n\n"
            + ctx.previous))
        delta = (f"```diff\n{ctx.delta_inline}\n```" if ctx.delta_inline is not None else
                 f"Read it with `git diff {ctx.prev_tree} {ctx.head_tree}`.")
        parts.append(_block("DELTA SINCE PREVIOUS REVIEW", f"```\n{ctx.delta_stat.strip()}\n```\n\n{delta}"))
    if ctx.gate_changes:
        parts.append(_block(
            "GATE-AFFECTING CHANGES NEEDING YOUR VOTE",
            "These changes alter what the gates check and cannot be proven harmless mechanically. For each, "
            "add an entry to `gate_changes` (`id`, `decision` accept|reject, `reason`). Vote `accept` only if "
            "the TASK requires the change and it does not hide a defect; otherwise vote `reject`. A change is "
            "accepted only when both reviewers accept. Do not also report these as findings.\n\n"
            + ctx.gate_changes))
    parts.append(_block("DIFF", diff_part))
    parts.append(_block("GATES (deterministic, run by the orchestrator)", ctx.gates_summary))
    if ctx.risk:
        parts.append(_block("RISK SIGNALS IN ADDED LINES", ctx.risk))
    if ctx.advisory:
        parts.append(_block("ADVISORY LINTS ON CHANGED LINES (hints, not verdicts)",
                            "\n".join(f"- {a}" for a in ctx.advisory)))
    return "\n".join(p for p in parts if p)


def fix_prompt(task: str, language: str, findings_text: str, round_no: int, allow_reject: bool,
               advisory_text: str = "") -> str:
    reject = ("You may reject a finding ONLY with concrete evidence (code reference, test, spec); set "
              "status `rejected` and give the argument in note - reviewers will see it." if allow_reject else
              "Rejecting findings is disabled: fix every item.")
    return f"""{_lang(language)}

Fix the blocking review findings (round {round_no}) for the TASK below. Each finding has an id.

Rules:
- Address every finding: status fixed | partial | rejected, with a short note per id.
- {reject}
- Fix the root cause, minimal change, and add a regression test for each behavioral bug.
- Never silence: no `#[allow]`, no `#[ignore]`, no weakened tests.
- A finding `G-...` is a gate-affecting change (gate config, `#[allow]`/`#[ignore]`, removed test) detected by the
  orchestrator: revert it, unless the TASK explicitly requires it - then set status `rejected` and give the
  justification in note. Only the orchestrator and the reviewers can accept such a change, never you.
- Run `{GATES} run --profile stop` before finishing; finish when green.
- Do not commit or change git state.
Your final answer is the JSON object described by the schema.

{_block("TASK (context)", task)}
{_block("BLOCKING FINDINGS", findings_text)}
{_block("ADVISORY (optional; fix only if trivial and safe)", advisory_text)}"""


def gate_fix_prompt(task: str, language: str, feedback: str, attempt: int, baseline: str | None) -> str:
    base = _block("PRE-EXISTING FAILURES ON THE BASE (for context)", baseline or "")
    return f"""{_lang(language)}

The deterministic Rust gates are RED after the last change (attempt {attempt}). Make them green.
Fix the root cause. Never silence lints with `#[allow]`, never `#[ignore]` or weaken tests, never
delete functionality required by the TASK. Run `{GATES} run --profile stop` to verify.
Do not commit or change git state. Finish with a short plain-text summary of what you changed and why
(the orchestrator reruns the gates to judge the result).

{_block("TASK (context)", task)}
{_block("GATE OUTPUT", feedback)}
{base}"""


def audit_prompt(ctx: ReviewContext, language: str) -> str:
    return review_prompt(ctx, "audit", language, "auditor")
