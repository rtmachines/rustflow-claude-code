"""Acceptance criteria. A criterion is semantic (reviewers judge `text`) and, when it names a `test`,
also mechanical: the orchestrator - not a model - checks that this exact test ran and passed on the
current tree. A reviewer's `met` never satisfies a mechanical criterion, and neither does a green
`cargo test` that ran zero tests or a different test."""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


class CriteriaError(ValueError):
    pass


@dataclass
class Criterion:
    id: str
    text: str
    test: str = ""          # exact test path, e.g. "parser::tests::trims_whitespace"
    source: str = "plan"    # plan | file

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Criterion":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def for_prompt(self) -> str:
        if self.test:
            return f"[{self.id}] {self.text} (required test `{self.test}` - the orchestrator checks it passes)"
        return f"[{self.id}] {self.text}"


_TEST_NAME = re.compile(r"^[^\s`]+(?: [^`]+)?$")


def _clean_test(raw: Any, where: str) -> str:
    test = str(raw or "").strip().strip("`").strip()
    if test and not _TEST_NAME.match(test):
        raise CriteriaError(f"{where}: invalid test name {raw!r}")
    return test


def load_file(path: Path) -> list[Criterion]:
    """TOML:  [[criteria]]  text = "..."  test = "module::tests::name" (optional)  id = "..." (optional)"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CriteriaError(f"criteria file not found: {path}") from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CriteriaError(f"{path}: {exc}") from exc
    items = data.get("criteria")
    if not isinstance(items, list) or not items:
        raise CriteriaError(f"{path}: needs at least one [[criteria]] table")
    out: list[Criterion] = []
    seen: set[str] = set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise CriteriaError(f"{path}: criteria[{i}] must be a table")
        unknown = set(item) - {"id", "text", "test"}
        if unknown:
            raise CriteriaError(f"{path}: criteria[{i}]: unknown keys {sorted(unknown)} (use id, text, test)")
        text = str(item.get("text") or "").strip()
        if not text:
            raise CriteriaError(f"{path}: criteria[{i}] needs a non-empty `text`")
        cid = str(item.get("id") or f"C{i}").strip()
        if cid in seen:
            raise CriteriaError(f"{path}: duplicate criterion id {cid!r}")
        seen.add(cid)
        out.append(Criterion(cid, text, _clean_test(item.get("test"), f"{path}: criteria[{i}]"), "file"))
    return out


def from_plan(plan: dict | None, existing: list[Criterion]) -> list[Criterion]:
    """Planner criteria (strings or {text, test}); ids continue after `existing`, duplicates dropped."""
    out: list[Criterion] = []
    texts = {c.text.lower() for c in existing}
    tests = {c.test for c in existing if c.test}
    n = len(existing)
    for item in (plan or {}).get("acceptance_criteria", []) or []:
        if isinstance(item, dict):
            text, test = str(item.get("text") or "").strip(), item.get("test")
        else:
            text, test = str(item).strip(), ""
        try:
            test = _clean_test(test, "plan")
        except CriteriaError:
            test = ""
        if not text or text.lower() in texts:
            continue
        if test in tests:
            test = ""
        n += 1
        out.append(Criterion(f"C{n}", text, test, "plan"))
        texts.add(text.lower())
        if test:
            tests.add(test)
    return out


def load_state(state: dict) -> list[Criterion]:
    return [Criterion.from_dict(d) for d in state.get("criteria") or []]


# ── mechanical evaluation ────────────────────────────────────────────────────
def _variants(observed: str) -> set[str]:
    """`cargo test`: "mod::name"; nextest: "<binary-id> mod::name" - accept both spellings."""
    s = observed.strip()
    out = {s}
    parts = s.split()
    if len(parts) == 2:  # nextest: binary id + test path, neither contains spaces (doc tests do)
        out.add(parts[1])
        out.add(f"{parts[0]}::{parts[1]}")
    return out


def _matches(observed: str, name: str) -> bool:
    return any(v == name or v.endswith("::" + name) for v in _variants(observed))


def test_status(name: str, passed: Iterable[str], failed: Iterable[str]) -> tuple[str, str]:
    """met: every test matching `name` passed (at least one ran). Short names are allowed; when they
    match several tests, all of them must pass."""
    hits = [t for t in passed if _matches(t, name)]
    bad = [t for t in failed if _matches(t, name)]
    if bad:
        return "not_met", "failed: " + ", ".join(bad[:3])
    if not hits:
        return "not_met", "did not run (no passing test with this name)"
    return "met", "passed: " + ", ".join(hits[:3])


def evaluate(criteria: list[Criterion], steps: list) -> list[dict]:
    """steps: gates.Step objects (or dicts) of one gate run. Only criteria with a `test` are evaluated."""
    passed: list[str] = []
    failed: list[str] = []
    notes: list[str] = []
    for s in steps:
        get = s.get if isinstance(s, dict) else (lambda k, d=None, _s=s: getattr(_s, k, d))
        if get("name") not in ("tests", "doctests"):
            continue
        passed += list(get("passed_tests") or [])
        failed += list(get("failed_tests") or [])
        if get("skipped"):
            notes.append(f"{get('name')} skipped: {get('reason') or '?'}")
    out = []
    for c in criteria:
        if not c.test:
            continue
        status, detail = test_status(c.test, passed, failed)
        if status != "met" and notes:
            detail += " (" + "; ".join(notes) + ")"
        out.append({"id": c.id, "test": c.test, "text": c.text, "status": status, "detail": detail})
    return out


def unmet(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("status") != "met"]
