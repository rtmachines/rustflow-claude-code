#!/usr/bin/env python3
"""Fake `claude` / `codex` CLIs for end-to-end tests. Behavior keyed on $RUSTFLOW_ROLE, prompt content
and $FAKE_SCENARIO. Records every invocation (argv, role) to $FAKE_LOG as JSON lines."""

import json
import os
import sys
from pathlib import Path

PROVIDER = Path(sys.argv[0]).name.replace(".py", "")
ARGS = sys.argv[1:]
ROLE = os.environ.get("RUSTFLOW_ROLE", "")
SCENARIO = os.environ.get("FAKE_SCENARIO", "happy")
LOG = os.environ.get("FAKE_LOG")

BUGGY = '''/// Parses `key=value` pairs separated by `;`.
pub fn parse_kv(input: &str) -> Vec<(String, String)> {
    input
        .split(';')
        .filter_map(|pair| {
            let (k, v) = pair.split_once('=')?;
            Some((k.to_string(), v.to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_pairs() {
        assert_eq!(
            parse_kv("a=1;b=2"),
            vec![("a".into(), "1".into()), ("b".into(), "2".into())]
        );
    }
}
'''

CLIPPY_RED = BUGGY.replace("""        .collect()
}""", """        .collect::<Vec<_>>()
        .into_iter()
        .collect()
}

pub fn answer() -> i32 {
    return 42;
}""", 1)

FIXED = '''/// Parses `key=value` pairs separated by `;`. Whitespace around keys and values is trimmed.
pub fn parse_kv(input: &str) -> Vec<(String, String)> {
    input
        .split(';')
        .filter_map(|pair| {
            let (k, v) = pair.split_once('=')?;
            Some((k.trim().to_string(), v.trim().to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_pairs() {
        assert_eq!(
            parse_kv("a=1;b=2"),
            vec![("a".into(), "1".into()), ("b".into(), "2".into())]
        );
    }

    #[test]
    fn trims_whitespace() {
        assert_eq!(parse_kv(" a = 1 "), vec![("a".into(), "1".into())]);
    }
}
'''


def arg_after(flag):
    if flag in ARGS:
        i = ARGS.index(flag)
        return ARGS[i + 1] if i + 1 < len(ARGS) else None
    return None


def line_of(text, needle):
    for i, l in enumerate(text.splitlines(), 1):
        if needle in l:
            return i
    return 1


def lib():
    return Path("src/lib.rs")


def finding(sev, cat, file, line, title, conf="high", prev=None):
    return {"severity": sev, "category": cat, "file": file, "line": line, "title": title,
            "trigger": "input ' a = 1 '", "impact": "keys contain spaces", "rule": "task: trim whitespace",
            "fix": "trim k and v", "confidence": conf, "previous_id": prev}


def review(prompt):
    round2 = "ROUND 2" in prompt or "ROUND 3" in prompt
    text = lib().read_text()
    line = line_of(text, "split_once")
    fixed = ".trim()" in text
    findings, disputes = [], []
    if not fixed:
        prev = "F1" if round2 and SCENARIO != "nolabel" else None
        findings.append(finding("P1", "correctness", "src/lib.rs", line, "Whitespace around key/value is not trimmed", prev=prev))
        if round2 and SCENARIO == "dispute":
            disputes.append({"id": "F1", "decision": "reraise", "note": "the task says trim"})
    if not round2 and ROLE == "reviewer_b":
        findings.append(finding("P1", "correctness", "src/does_not_exist.rs", 3, "Hallucinated file"))
        findings.append(finding("P3", "docs", "./src/lib.rs", 1, "Doc comment could mention separator", conf="medium"))
        if SCENARIO == "reject":
            findings.append(finding("P2", "idiom", "src/lib.rs", 2, "Return impl Iterator instead of Vec", conf="medium"))
    verdict = "REQUEST_CHANGES" if findings and any(f["severity"] in ("P0", "P1") for f in findings) else "APPROVE"
    return {"summary": f"{ROLE} review", "verdict": verdict, "findings": findings, "disputes": disputes,
            "acceptance": [{"criterion": "trim whitespace",
                            "status": "met" if fixed or SCENARIO == "dispute" else "not_met", "note": ""}],
            "residual_risks": []}


UNSAFE_ADDON = """
/// Raw access used by the FFI layer.
///
/// # Safety
/// Callers must uphold nothing; this is a test fixture.
pub unsafe fn raw() {}
"""


def with_unsafe(src):
    return src.replace("#[cfg(test)]", UNSAFE_ADDON.lstrip("\n") + "\n#[cfg(test)]", 1)


def act(prompt):
    if ROLE == "tob_audit":
        assert prompt.startswith("/rust-review:rust-review ")
        out = Path(".rust-review-results") / "20261026T000000Z"
        out.mkdir(parents=True, exist_ok=True)
        (out / "REPORT.md").write_text("# Rust Security Review\n\nNo exploitable issues in `raw`.\n")
        if SCENARIO == "tamper":
            lib().write_text(lib().read_text() + "\n// audit was here\n")
        return None
    if ROLE == "implementer":
        if os.environ.get("FAKE_SLEEP"):
            import time
            time.sleep(float(os.environ["FAKE_SLEEP"]))
        if SCENARIO in ("escalate", "tamper"):
            lib().write_text(with_unsafe(BUGGY))
            return {"summary": "with unsafe"}
        lib().write_text(CLIPPY_RED if SCENARIO == "gatefix" else BUGGY)
        return {"summary": "implemented parse_kv", "files_changed": ["src/lib.rs"], "tests_added": ["parses_pairs"],
                "gates_green": True, "notes": ""}
    if ROLE == "fixer":
        if "gates are RED" in prompt:
            lib().write_text(BUGGY)
            return {"summary": "removed needless return / collect", "resolutions": [], "tests_added": []}
        if SCENARIO == "dispute":
            return {"summary": "disagree", "resolutions": [{"id": "F1", "status": "rejected",
                    "note": "inputs are pre-trimmed by the caller"}], "tests_added": []}
        if SCENARIO == "crash":
            raise SystemExit(0)
        if SCENARIO == "nolabel":  # cosmetic change, bug stays
            lib().write_text(lib().read_text() + "\n// touched\n")
            return {"summary": "touched", "resolutions": [{"id": "F1", "status": "partial", "note": "tried"}],
                    "tests_added": []}
        if SCENARIO == "oscillate":  # round 1: cosmetic edit; round 2: back to the implementer's version
            lib().write_text(BUGGY + ("\n// attempt 1\n" if "(round 1)" in prompt else ""))
            return {"summary": "attempt", "resolutions": [{"id": "F1", "status": "partial", "note": "tried"}],
                    "tests_added": []}
        if SCENARIO == "stall":
            return {"summary": "could not fix", "resolutions": [{"id": "F1", "status": "partial", "note": "tried"}],
                    "tests_added": []}
        lib().write_text(with_unsafe(FIXED) if SCENARIO == "escalate" else FIXED)
        return {"summary": "trimmed", "resolutions": [{"id": "F1", "status": "fixed", "note": "trim + test"}],
                "tests_added": ["trims_whitespace"]}
    if ROLE in ("reviewer_a", "reviewer_b", "auditor"):
        if SCENARIO == "reviewer_touch" and ROLE == "reviewer_a":
            lib().write_text(lib().read_text() + "\n// edited during review\n")
        return review(prompt)
    if ROLE == "planner":
        return {"summary": "plan", "difficulty": "hard", "steps": ["write parse_kv", "test it"],
                "acceptance_criteria": [{"text": "trim whitespace",
                                         "test": os.environ.get("FAKE_PLAN_TEST", "")}], "files_to_touch": ["src/lib.rs"], "risks": [],
                "open_questions": []}
    return {"difficulty": "easy", "rationale": "small", "risk_areas": []}


def earlier_calls(role):
    """Invocations of `role` before this one (the log line of this call is already written)."""
    if not LOG or not Path(LOG).is_file():
        return 0
    rows = [json.loads(l) for l in Path(LOG).read_text(encoding="utf-8").splitlines() if l.strip()]
    return sum(1 for r in rows if r["role"] == role) - 1


def overloaded():
    """What `claude -p` prints when the API is overloaded: a result object with is_error."""
    if PROVIDER == "codex":
        print("stream error: 529 overloaded", file=sys.stderr)
        return 1
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 529 overloaded",
                      "api_error_status": 529, "total_cost_usd": 0.0, "session_id": "sess-err", "usage": {}}))
    return 1


def main():
    if "--version" in ARGS:
        print(f"fake-{PROVIDER} 9.9.9")
        return 0
    prompt = sys.stdin.read()
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"provider": PROVIDER, "role": ROLE, "argv": ARGS,
                                 "prompt_head": prompt[:200], "cwd": os.getcwd()}) + "\n")
    if SCENARIO == "codex_down" and PROVIDER == "codex":
        print("boom", file=sys.stderr)
        return 1
    if SCENARIO == "codex_auth" and PROVIDER == "codex":
        print("Error: unauthorized - not logged in. Run `codex login`.", file=sys.stderr)
        return 1
    if ROLE == "implementer" and (SCENARIO == "impl_down" or (SCENARIO == "impl_flaky" and earlier_calls(ROLE) == 0)):
        return overloaded()
    if SCENARIO == "reviewers_down" and ROLE in ("reviewer_a", "reviewer_b"):
        return overloaded()
    gate_fix = "gates are RED" in prompt
    if SCENARIO == "json_always" and ROLE == "reviewer_a":  # structured output never validates, repair included
        print(json.dumps({"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True,
                          "total_cost_usd": 0.1, "session_id": "sess-rev", "usage": {}}))
        return 1
    if ROLE == "fixer" and "--resume" in ARGS:  # structured-output repair turn: answer only, touch nothing
        assert ARGS[ARGS.index("--resume") + 1] == "sess-fix" and "--json-schema" in ARGS
        data = {"summary": "repaired", "resolutions": [{"id": "F1", "status": "fixed", "note": "trim + test"}],
                "tests_added": ["trims_whitespace"]}
    else:
        data = act(prompt)
    if PROVIDER == "claude":
        assert "-p" in ARGS and "--effort" in ARGS
        assert ("--json-schema" in ARGS) == (ROLE not in ("implementer", "tob_audit") and not gate_fix)
        if data is None:
            data = {}
        if SCENARIO == "fix_json" and ROLE == "fixer" and not gate_fix and "--resume" not in ARGS:
            print(json.dumps({"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True,
                              "total_cost_usd": 0.25, "session_id": "sess-fix", "usage": {"input_tokens": 100},
                              "errors": ["Failed to provide valid structured output after 5 attempts"]}))
            return 1
        denials = ([{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "ls -la src"}}]
                   if SCENARIO == "denials" and ROLE == "implementer" else [])
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                          "structured_output": data, "total_cost_usd": 0.25, "session_id": "sess-1",
                          "usage": {"input_tokens": 100, "output_tokens": 50},
                          "permission_denials": denials, "num_turns": 3,
                          **({"modelUsage": {os.environ["FAKE_MODEL_REPORT"]: {"outputTokens": 50},
                                             "claude-haiku-5-5": {"outputTokens": 3}}}
                             if os.environ.get("FAKE_MODEL_REPORT") else {})}))
    else:
        assert ARGS[0] == "exec" and ARGS[-1] == "-"
        out = arg_after("-o")
        Path(out).write_text(json.dumps(data), encoding="utf-8")
        print(json.dumps({"type": "thread.started", "thread_id": "th-1"}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 80}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
