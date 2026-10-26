"""Difficulty triage: heuristic score (PL + EN keywords, size, repo shape) with optional LLM opinion."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rustflow.config import DIFFICULTIES, Config

# (weight, regex) - matched case-insensitively against the task prompt.
SIGNALS: list[tuple[int, str, str]] = [
    (3, "unsafe/FFI/soundness", r"\bunsafe\b|\bffi\b|extern\s*\"?c\"?|transmute|\braw pointer|wskaźnik\w* surow|soundness|\bmiri\b|no_mangle|repr\(c\)"),
    (2, "concurrency/async", r"\basync\b|\bawait\b|tokio|współbieżn|concurren|\bthread|wątk|\block|mutex|rwlock|atomic|deadlock|zakleszcz|race|wyścig|select!|channel|kanał"),
    (2, "type-level/lifetimes/macros", r"lifetime|czas\w* życia|\bgat\b|higher.ranked|\bhrtb\b|trait object|dyn trait|generic|generyczn|proc.?macro|makr|macro_rules|const generic|variance|pin\b|\bunpin\b"),
    (2, "architecture/refactor", r"refaktor|refactor|architekt|redesign|przeprojekt|migrac|migrat|cały\w* (crate|moduł|projekt)|across (the )?(workspace|crates)|wiele crate|multiple crates|public api|publiczn\w* api|breaking"),
    (1, "performance", r"wydajno|performance|optymaliz|optimi[sz]|zero.copy|allocat|alokac|benchmark|simd|\bhot path"),
    (1, "persistence/protocol", r"\bsql\b|sqlx|migration|baz\w* danych|database|protok|protocol|serializ|parser|parsow"),
    (1, "error handling/robustness", r"error handling|obsług\w* błęd|retry|timeout|cancel|anulow|graceful"),
    (-2, "trivial", r"\btypo\b|literówk|rename|zmień nazwę|zmien nazwe|komentarz|comment|docstring|readme|bump|podbij|format(uj)?\b|\blint\b|clippy warning"),
]


@dataclass
class TriageResult:
    difficulty: str
    score: int
    reasons: list[str] = field(default_factory=list)
    source: str = "heuristic"
    llm: dict | None = None

    def to_dict(self) -> dict:
        return {"difficulty": self.difficulty, "score": self.score, "reasons": self.reasons,
                "source": self.source, "llm": self.llm}


def workspace_members(repo: Path) -> int:
    manifest = repo / "Cargo.toml"
    if not manifest.is_file():
        return 0
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return 1
    ws = data.get("workspace") or {}
    count = 0
    for pattern in ws.get("members", []) or []:
        count += len([p for p in repo.glob(pattern) if (p / "Cargo.toml").is_file()]) or 1
    return max(count, 1)


def heuristic(prompt: str, repo: Path | None, cfg: Config) -> TriageResult:
    text = prompt.lower()
    score = 0
    reasons: list[str] = []
    trivial = False
    for weight, label, rx in SIGNALS:
        hits = len(re.findall(rx, text))
        if not hits:
            continue
        if weight < 0:
            trivial = True
            continue
        delta = weight + (1 if hits >= 3 else 0)
        score += delta
        reasons.append(f"{label} (+{delta})")
    # "trivial" words only lower the score for short tasks with no other signal
    # ("add a doc comment" inside a feature request is not a trivial task).
    feature = re.search(r"\b(add|implement|create|introduce|support|dodaj|zaimplementuj|utwórz|stwórz|obsłuż)\w*", text)
    if trivial and score == 0 and len(prompt) < 300 and not feature:
        score -= 2
        reasons.append("trivial change (-2)")
    n = len(prompt)
    if n > 4000:
        score += 2
        reasons.append("very long task description (+2)")
    elif n > 1500:
        score += 1
        reasons.append("long task description (+1)")
    files = set(re.findall(r"[\w./-]+\.rs\b", prompt))
    if len(files) >= 4:
        score += 1
        reasons.append(f"{len(files)} files named (+1)")
    bullets = len(re.findall(r"^\s*(?:[-*]|\d+[.)])\s+", prompt, re.M))
    if bullets >= 6:
        score += 1
        reasons.append(f"{bullets} requirement bullets (+1)")
    if repo is not None:
        members = workspace_members(repo)
        if members >= 6:
            score += 1
            reasons.append(f"workspace with {members} crates (+1)")
    easy_max = int(cfg.get("triage.easy_max", 1))
    medium_max = int(cfg.get("triage.medium_max", 4))
    difficulty = "easy" if score <= easy_max else ("medium" if score <= medium_max else "hard")
    return TriageResult(difficulty, score, reasons or ["no complexity signals"])


TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["difficulty", "rationale", "risk_areas"],
    "properties": {
        "difficulty": {"type": "string", "enum": list(DIFFICULTIES)},
        "rationale": {"type": "string"},
        "risk_areas": {"type": "array", "items": {"type": "string"}},
    },
}

TRIAGE_PROMPT = """You are triaging a Rust programming task to choose model strength. Do not modify files.
Classify the difficulty:
- easy: local change, one module, no concurrency/unsafe/API redesign, obvious tests.
- medium: several files or a new feature with tests; ordinary async or error handling work.
- hard: unsafe/FFI, tricky concurrency or lifetimes, cross-crate refactor, public API redesign,
  performance work needing measurement, or vague requirements needing design decisions.
You may glance at the repository layout (Cargo.toml, src tree) to calibrate. Answer with JSON only.

TASK:
"""


def estimate_cost(repo: Path, routing: dict[str, str], difficulty: str | None = None) -> dict:
    """Cost estimate from this repo's own run history (Claude USD only; Codex reports tokens).
    Honest by design: without history it says so instead of inventing a number."""
    import json
    from rustflow import usage
    runs_dir = repo / ".rustflow" / "runs"
    per_spec: dict[str, list[float]] = {}
    per_diff: dict[str, list[float]] = {}
    legacy_runs = excluded_incomplete_runs = 0
    for st_file in runs_dir.glob("*/state.json"):
        try:
            st = json.loads(st_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if st.get("dry_run") or st.get("status") in ("running", "error", "baseline_red"):
            continue
        summary = st.get("usage_summary")
        legacy = summary is None or not summary.get("history_complete", False)
        if legacy:
            legacy_runs += 1
        if summary is None:
            measured = usage.number(st.get("cost_usd"))
        else:
            claude = summary.get("providers", {}).get("claude", {})
            measured = claude.get("cost_usd", {}).get("total")
            if legacy:
                baseline = usage.number(st.get("usage_baseline", {}).get("cost_usd"))
                cost = claude.get("cost_usd", {"known": 0, "missing_attempts": 0})
                # Keep old history as explicitly limited data. Only add new costs
                # when measured; a timeout/unknown must not become a legacy sample.
                measured = (baseline + cost["known"] if baseline is not None and not cost["missing_attempts"]
                            else None)
        if measured is not None:
            per_diff.setdefault(st.get("difficulty", "?"), []).append(float(measured))
        else:
            excluded_incomplete_runs += 1
        for a in st.get("agents", []):
            if not a.get("spec", "").startswith("claude:"):
                continue
            legacy_sample = legacy and "attempt_ids" not in a
            cost = usage.number(a.get("cost_usd")) if legacy_sample else usage.number(a.get("measured_cost_usd"))
            if cost is not None and (legacy_sample or a.get("attempt_ids")):
                per_spec.setdefault(a.get("spec", "?"), []).append(float(cost))
    roles = {r: (sum(per_spec[s]) / len(per_spec[s]) if per_spec.get(s) else None) for r, s in routing.items()}
    hist = per_diff.get(difficulty or "", []) if difficulty else []
    out = {"runs_in_history": sum(len(v) for v in per_diff.values()), "per_call_usd": roles,
           "legacy_runs": legacy_runs, "excluded_incomplete_runs": excluded_incomplete_runs,
           "same_difficulty": {"n": len(hist), "mean_usd": round(sum(hist) / len(hist), 2) if hist else None,
                               "max_usd": round(max(hist), 2) if hist else None}}
    if hist:
        out["text"] = (f"past {difficulty} runs here: mean ${out['same_difficulty']['mean_usd']}, "
                       f"max ${out['same_difficulty']['max_usd']} (n={len(hist)}, Claude only)")
    elif any(v is not None for v in roles.values()):
        known = ", ".join(f"{r} ~${v:.2f}/call" for r, v in roles.items() if v is not None)
        out["text"] = f"no past {difficulty} runs; per-call history: {known} (Claude only)"
    else:
        out["text"] = "no run history yet - cost unknown (see [budget].max_total_usd for the cap)"
    if legacy_runs:
        out["text"] += f"; {legacy_runs} legacy run(s): measurement completeness unknown"
    if excluded_incomplete_runs:
        out["text"] += f"; {excluded_incomplete_runs} run(s) excluded from totals: incomplete measurements"
    return out
