"""A driving agent on MCP: next_actions name the MCP tool call (not only a CLI string), waits can be long,
and no tool answer outgrows the MCP output limit."""

import json

import pytest

from rustflow import mcp_server, runs

TOOLS = {t["name"]: t for t in mcp_server.TOOLS}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}

BASE = {"run_id": "20261026-x", "base_sha": "abcdef1234567", "branch": "rustflow/20261026-x", "isolation": "branch",
        "original_branch": "main", "pid": 0, "mode": "run",
        "findings": [{"id": "F1", "severity": "P1", "status": "open", "title": "bug", "file": "a.rs", "line": 1}],
        "rounds": [{"round": 1, "failed_reviewers": []}]}
STATUSES = ("running", "approved", "changes_requested", "criteria_unmet", "max_rounds", "stalled", "final_gates_red",
            "gates_red", "baseline_red", "budget", "interrupted", "tree_changed", "agent_failed", "review_failed",
            "no_changes")


def _valid(call: dict) -> None:
    tool = TOOLS[call["tool"]]
    schema = tool["inputSchema"]
    args = call["arguments"]
    assert set(schema.get("required", [])) <= set(args), (call, "missing required")
    for k, v in args.items():
        prop = schema["properties"][k]                       # KeyError = argument the tool does not take
        assert isinstance(v, _TYPES[prop["type"]]), (k, v)
        if "enum" in prop:
            assert v in prop["enum"], (k, v)
        if "maximum" in prop:
            assert v <= prop["maximum"], (k, v)


@pytest.mark.parametrize("status", STATUSES)
def test_every_rustflow_command_has_a_valid_mcp_call(status):
    state = {**BASE, "status": status}
    if status == "running":
        state["pid"] = __import__("os").getpid()             # alive -> stays "running"
    for act in runs.next_actions(state):
        cmd = act.get("command", "")
        if cmd.startswith("rustflow ") and not cmd.startswith(("rustflow comment", "rustflow doctor")):
            assert "mcp" in act, act
        if "mcp" in act:
            _valid(act["mcp"])


def test_mcp_calls_match_the_cli_commands():
    acts = runs.next_actions({**BASE, "status": "max_rounds"})
    assert acts[0]["mcp"] == {"tool": "rustflow_resume", "arguments": {"run": "20261026-x", "extra_rounds": 2}}
    acts = runs.next_actions({**BASE, "status": "changes_requested"})
    assert acts[0]["mcp"] == {"tool": "rustflow_start",
                              "arguments": {"mode": "review", "fix": True, "base": "abcdef123456"}}
    acts = runs.next_actions({**BASE, "status": "running", "pid": __import__("os").getpid()})
    assert acts[0]["mcp"]["tool"] == "rustflow_wait"


def test_mcp_brief_shows_tool_calls_instead_of_cli_commands():
    s = runs.summary({**BASE, "status": "max_rounds"})
    cli, mcp = runs.brief_markdown(s), runs.brief_markdown(s, mcp=True)
    assert "rustflow resume 20261026-x --extra-rounds 2 --detach" in cli
    assert 'rustflow_resume {"run": "20261026-x", "extra_rounds": 2}' in mcp and "--detach" not in mcp


def test_wait_allows_long_waits_on_mcp():
    prop = TOOLS["rustflow_wait"]["inputSchema"]["properties"]["timeout_s"]
    assert prop["maximum"] == 1800 and prop["default"] == 600       # MCP_TOOL_TIMEOUT is ~28 h, not Bash's 10 min


def _big_state(tmp_path, n=300):
    f = {"severity": "P2", "category": "idiom", "file": "src/lib.rs", "line": 7, "title": "x" * 300,
         "trigger": "t" * 2000, "impact": "i" * 2000, "rule": "r" * 500, "fix": "f" * 2000, "confidence": "high",
         "reviewers": ["reviewer_a"], "previous_id": None, "status": "advisory", "round": 1,
         "also": [{"reviewer": "reviewer_b", "title": "y" * 300, "trigger": "z" * 2000}] * 3}
    st = {**BASE, "status": "approved_with_advisories", "repo": str(tmp_path),
          "findings": [{**f, "id": f"F{i}"} for i in range(n)]}
    run = tmp_path / ".rustflow" / "runs" / st["run_id"]
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (run / "report.md").write_text("# report\n" + ("line of the report\n" * 20000), encoding="utf-8")
    return st


@pytest.mark.parametrize("fmt", ["json", "full"])
def test_report_stays_under_the_mcp_output_limit(tmp_path, monkeypatch, fmt):
    _big_state(tmp_path)
    monkeypatch.setattr(mcp_server, "_repo", lambda: tmp_path)
    text, data = mcp_server.t_report({"run": "20261026-x", "format": fmt})
    assert len(text) <= mcp_server.MAX_TEXT_CHARS < 50_000       # Claude Code spills >50k chars to a file
    assert "truncated" in text and "rustflow report" in text      # says how to get everything
    if fmt == "json":
        payload = json.loads(text)
        assert payload["findings_total"] == 300 and payload["findings"][0]["id"] == "F0"
        assert "also" not in payload["findings"][0] and len(payload["findings"][0]["fix"]) <= 400


def test_small_json_report_is_complete(tmp_path, monkeypatch):
    _big_state(tmp_path, n=3)
    monkeypatch.setattr(mcp_server, "_repo", lambda: tmp_path)
    payload = json.loads(mcp_server.t_report({"run": "20261026-x", "format": "json"})[0])
    assert payload["findings_total"] == 3 and len(payload["findings"]) == 3 and not payload["findings_truncated"]


def test_full_report_reaches_the_model_through_structured_content(tmp_path, monkeypatch):
    """Measured live (Claude Code 2.1.308): the model gets `structuredContent`, not the `content` text.
    The full report has to be in the structured part, or a driver asking for it never sees it."""
    _big_state(tmp_path, n=2)
    monkeypatch.setattr(mcp_server, "_repo", lambda: tmp_path)
    text, data = mcp_server.t_report({"run": "20261026-x", "format": "full"})
    assert data["report"] == text and data["report"].startswith("# report")
    assert len(json.dumps(data)) < 50_000
