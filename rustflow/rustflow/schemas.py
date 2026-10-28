"""JSON schemas for agent outputs (strict-mode compatible for both Claude --json-schema and
Codex --output-schema: every property required, additionalProperties false) + a small validator."""

from __future__ import annotations

import json
import re
from typing import Any

CATEGORIES = ["correctness", "soundness", "concurrency", "error-handling", "api", "performance",
              "tests", "idiom", "docs", "security", "build", "acceptance"]


def _obj(props: dict, ) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}

FINDING = _obj({
    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
    "category": {"type": "string", "enum": CATEGORIES},
    "file": STR,
    "line": {"type": "integer"},
    "title": STR,
    "trigger": STR,
    "impact": STR,
    "rule": STR,
    "fix": STR,
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "previous_id": {"type": ["string", "null"]},
})

REVIEW_SCHEMA = _obj({
    "summary": STR,
    "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "ESCALATE"]},
    "findings": {"type": "array", "items": FINDING},
    "disputes": {"type": "array", "items": _obj({
        "id": STR,
        "decision": {"type": "string", "enum": ["accept_rejection", "reraise"]},
        "note": STR,
    })},
    "acceptance": {"type": "array", "items": _obj({
        "criterion": STR,
        "status": {"type": "string", "enum": ["met", "not_met", "unclear"]},
        "note": STR,
        # not_met: 0-based index into this review's `findings` of the finding that describes the gap, else null
        "finding": {"type": ["integer", "null"]},
    })},
    "residual_risks": STR_LIST,
    # votes on gate-affecting changes listed in the prompt; absent = no vote (the validator defaults it to [])
    "gate_changes": {"type": "array", "items": _obj({
        "id": STR,
        "decision": {"type": "string", "enum": ["accept", "reject"]},
        "reason": STR,
    })},
})

FIX_SCHEMA = _obj({
    "summary": STR,
    "resolutions": {"type": "array", "items": _obj({
        "id": STR,
        "status": {"type": "string", "enum": ["fixed", "rejected", "partial"]},
        "note": STR,
    })},
    "tests_added": STR_LIST,
})

PLAN_SCHEMA = _obj({
    "summary": STR,
    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
    "steps": STR_LIST,
    # test: exact name of the test that proves the criterion ("" when not provable by a test)
    "acceptance_criteria": {"type": "array", "items": _obj({"text": STR, "test": STR})},
    "files_to_touch": STR_LIST,
    "risks": STR_LIST,
    "open_questions": STR_LIST,
})

IMPLEMENT_SCHEMA = _obj({
    "summary": STR,
    "files_changed": STR_LIST,
    "tests_added": STR_LIST,
    "gates_green": {"type": "boolean"},
    "notes": STR,
})


# ── validation ───────────────────────────────────────────────────────────────
class SchemaError(ValueError):
    pass


_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict, "null": type(None)}


def validate(value: Any, schema: dict, path: str = "$") -> Any:
    """Validate + lightly coerce (e.g. "42" -> 42 for integers). Returns the coerced value."""
    types = schema.get("type")
    if types is not None:
        tlist = types if isinstance(types, list) else [types]
        if "integer" in tlist and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if "integer" in tlist and isinstance(value, float) and value.is_integer():
            value = int(value)
        ok = any(isinstance(value, _TYPES[t]) and not (t == "integer" and isinstance(value, bool))
                 for t in tlist)
        if not ok:
            raise SchemaError(f"{path}: expected {types}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        if isinstance(value, str) and value.upper() in schema["enum"]:
            value = value.upper()
        elif isinstance(value, str) and value.lower() in schema["enum"]:
            value = value.lower()
        else:
            raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict) and "properties" in schema:
        out = {}
        for key in schema.get("required", []):
            if key not in value:
                prop = schema["properties"].get(key, {})
                t = prop.get("type")
                if t == "array":
                    value[key] = []
                elif isinstance(t, list) and "null" in t:
                    value[key] = None
                else:
                    raise SchemaError(f"{path}: missing required key '{key}'")
        for key, sub in schema["properties"].items():
            if key in value:
                out[key] = validate(value[key], sub, f"{path}.{key}")
        return out
    if isinstance(value, list) and "items" in schema:
        return [validate(v, schema["items"], f"{path}[{i}]") for i, v in enumerate(value)]
    return value


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> Any:
    """Find a JSON object in free text (fenced block, whole text, or outermost braces)."""
    text = (text or "").strip()
    if not text:
        raise SchemaError("empty output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for m in reversed(list(_FENCE.finditer(text))):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise SchemaError("no JSON object found in output")
