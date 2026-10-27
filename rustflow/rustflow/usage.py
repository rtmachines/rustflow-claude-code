"""Provider measurements and the append-only identity of individual CLI attempts.

Unknown is represented by None, including incomplete totals. Known subtotals remain
available separately; budget estimates are never promoted to measured costs.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

TOKEN_FIELDS = {
    "claude": ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
    "codex": ("input_tokens", "output_tokens", "cache_read_input_tokens"),
}


def number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if integer and not isinstance(value, int):
        return None
    try:
        return value if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def normalize(provider: str, raw: Any, cost: Any) -> dict:
    fields = TOKEN_FIELDS[provider]
    parts = raw if provider == "codex" and isinstance(raw, list) else [raw]
    parts = parts or [None]
    tokens, known = {}, {}
    observed = False
    for key in fields:
        source = "cached_input_tokens" if provider == "codex" and key == "cache_read_input_tokens" else key
        values = [number(p.get(source), integer=True) if isinstance(p, dict) else None for p in parts]
        observed = observed or any(v is not None for v in values)
        known[key] = sum(v for v in values if v is not None)
        tokens[key] = known[key] if all(v is not None for v in values) else None
    # Codex input already includes cached input. Claude's cache counters are separate.
    total_fields = fields if provider == "claude" else ("input_tokens", "output_tokens")
    known["total_tokens"] = sum(known[k] for k in total_fields)
    tokens["total_tokens"] = (known["total_tokens"] if all(tokens[k] is not None for k in total_fields) else None)
    measured = number(cost)
    complete = measured is not None and all(v is not None for v in tokens.values())
    present = measured is not None or observed
    return {"raw_usage": json_safe(raw), "tokens": tokens, "known_tokens": known,
            "measured_cost_usd": measured,
            "measurement_status": "complete" if complete else "partial" if present else "unknown"}


def raw_tokens(raw: Any) -> dict:
    """Compatible provider-named counters, with invalid measurements excluded."""
    parts = raw if isinstance(raw, list) else [raw]
    out: dict[str, int] = {}
    for part in parts:
        if isinstance(part, dict):
            for key, value in part.items():
                if number(value, integer=True) is not None:
                    out[key] = out.get(key, 0) + value
    return out


def _metric(values: list, known: list | None = None, *, history_complete: bool = True) -> dict:
    missing = sum(v is None for v in values)
    subtotal = sum(v for v in (known if known is not None else values) if v is not None)
    if isinstance(subtotal, float):
        subtotal = round(subtotal, 10)
    return {"known": subtotal, "total": subtotal if not missing and history_complete else None,
            "missing_attempts": missing}


def _aggregate(rows: list[dict], history_complete: bool) -> dict:
    return {
        "attempt_count": len(rows),
        "measurements": {status: sum(r["measurement_status"] == status for r in rows)
                         for status in ("complete", "partial", "unknown")},
        "cost_usd": _metric([r.get("measured_cost_usd") for r in rows], history_complete=history_complete),
        "estimated_cost_usd": round(sum(r.get("estimated_cost_usd") or 0 for r in rows), 10),
        "duration_s": sum(r.get("duration_s") or 0 for r in rows),
    }


def summarize(rows: list[dict], *, history_complete: bool = True) -> dict:
    out = {"schema_version": 1, "history_complete": history_complete,
           **_aggregate(rows, history_complete), "providers": {}}
    for provider in sorted({r["provider"] for r in rows}):
        subset = [r for r in rows if r["provider"] == provider]
        group = _aggregate(subset, history_complete)
        group["tokens"] = {
            key: _metric([r["tokens"].get(key) for r in subset],
                         [r["known_tokens"].get(key) for r in subset], history_complete=history_complete)
            for key in (*TOKEN_FIELDS[provider], "total_tokens")}
        out["providers"][provider] = group
    return out


def summary_for_state(state: dict) -> dict:
    # Read old states without changing them on disk or manufacturing attempts.
    return state.get("usage_summary") or summarize([], history_complete=False)


def brief(summary: dict) -> str:
    cost = summary["cost_usd"]
    measured = (f"measured ${cost['total']:.4f}" if cost["total"] is not None else
                f"known measured ${cost['known']:.4f}; cost missing for {cost['missing_attempts']} attempt(s)")
    history = "; legacy history completeness unknown" if not summary["history_complete"] else ""
    return (f"usage: {summary['attempt_count']} attempt(s), {measured}; "
            f"timeout estimates ${summary['estimated_cost_usd']:.4f}{history}")


def json_safe(value: Any) -> Any:
    """Retain invalid raw counters for diagnosis without emitting nonstandard JSON numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(v) for key, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_attempt(directory: Path, record: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record['attempt_id']}.json"
    tmp = directory / f".{record['attempt_id']}-{uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(json_safe(record), indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def load_attempts(directory: Path, *, interrupt_started: bool = False) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if interrupt_started and row["status"] == "started":
            row.update(status="interrupted", finished_at=timestamp(), duration_s=None,
                       termination_reason="interrupted", output_complete=False,
                       error="previous process ended before recording a result", estimated_cost_usd=None,
                       **normalize(row["provider"], None, None))
            write_attempt(directory, row)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["started_at"], r["attempt_id"]))
