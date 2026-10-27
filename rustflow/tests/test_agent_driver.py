"""Driving rustflow from an agent: detached runs, wait/cancel/resume, active-run lock, hooks of the
driving session, next_actions, MCP server, cost estimate."""

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import git, read_log
from rustflow import gates, hooks, runs, triage
from rustflow.mcp_server import Server

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _wait_for(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _hook(repo, sub, payload, role=None):
    env = {k: v for k, v in os.environ.items() if k != "RUSTFLOW_ROLE"}
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    if role:
        env["RUSTFLOW_ROLE"] = role
    r = subprocess.run([sys.executable, str(repo / ".rustflow" / "hooks.py"), sub], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, cwd=repo)
    return r


def test_lock_refreshes_private_console_on_resume(tmp_path, monkeypatch):
    from types import SimpleNamespace
    env = {"RUSTFLOW_PRIVATE_CONSOLE": "1"}
    monkeypatch.setattr(runs, "os", SimpleNamespace(name="nt", environ=env, getpid=lambda: 42))
    monkeypatch.setattr(gates, "read_active", lambda repo: None)
    state = {"run_id": "same-run"}
    runs.acquire(tmp_path, state)
    assert state["private_console"] is True
    # A later foreground resume must not broadcast to the user's shared console.
    env.clear()
    runs.acquire(tmp_path, state)
    assert state["private_console"] is False


def test_detached_run_lock_hooks_cancel_resume(rust_repo, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    info = runs.spawn_detached(rust_repo, ["run"], TASK)
    assert info["status"] == "started", info
    rid = info["run_id"]
    run_dir = runs.find_run(rust_repo, rid)
    assert _wait_for(lambda: gates.read_active(rust_repo) is not None)
    act = gates.read_active(rust_repo)
    assert act["run_id"] == rid
    # one run per repo
    with pytest.raises(runs.RunLocked):
        runs.spawn_detached(rust_repo, ["run"], "another task")
    # the driving session (no role) may not edit the run's checkout, nor change git state there
    assert _wait_for(lambda: (gates.read_active(rust_repo) or {}).get("phase") == "implement", 90), (
        (run_dir / "console.log").read_text(encoding="utf-8", errors="replace")[-4000:])
    r = _hook(rust_repo, "guard-edit", {"tool_input": {"file_path": str(rust_repo / "src" / "lib.rs")}})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny" and rid in r.stdout
    r = _hook(rust_repo, "guard-bash", {"tool_input": {"command": "git switch main"}})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # ... and its Stop hook does not gate (and block on) the run's half-done work
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 { return 1; }\n")
    assert _hook(rust_repo, "stop", {"session_id": "driver"}).stdout == ""
    git(rust_repo, "checkout", "--", "src/lib.rs")
    # the run's own agents are not affected by the driver guard
    r = _hook(rust_repo, "guard-edit", {"tool_input": {"file_path": str(rust_repo / "src" / "lib.rs")}},
              role="implementer")
    assert r.stdout == ""
    # wait with a short timeout -> still running (75)
    s = runs.wait(runs.find_run(rust_repo, rid), 0.5, out=lambda *_: None)
    assert s["status"] == "running" and runs.exit_code(s["status"]) == 75
    assert s["next_actions"][0]["action"] == "wait"
    assert _wait_for(lambda: bool(list((run_dir / "attempts").glob("*.json"))))
    # cancel -> interrupted, lock released, resumable
    res = runs.cancel(rust_repo, runs.find_run(rust_repo, rid))
    assert res["status"] == "interrupted", (
        res, (run_dir / "console.log").read_text(encoding="utf-8", errors="replace")[-4000:])
    if os.name == "nt":
        assert res["forced"] is False
    assert gates.read_active(rust_repo) is None
    interrupted = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in (run_dir / "attempts").glob("*.json")}
    assert len(interrupted) == 1
    assert all(a["measured_cost_usd"] is None and a["measurement_status"] == "unknown"
               for a in interrupted.values())
    monkeypatch.delenv("FAKE_SLEEP")
    info2 = runs.spawn_detached(rust_repo, ["resume", rid], None, run_id=rid)
    assert info2["status"] in ("started", "finished_immediately"), info2
    s = runs.wait(runs.find_run(rust_repo, rid), 120, out=lambda *_: None)
    assert s["status"] == "approved_with_advisories", s
    assert gates.read_active(rust_repo) is None
    assert any(a["action"] == "merge" and a.get("needs_user") for a in s["next_actions"])
    attempts = [json.loads(p.read_text(encoding="utf-8")) for p in (run_dir / "attempts").glob("*.json")]
    assert len(attempts) == 7  # interrupted implementer + implement/review x2/fix/review x2
    assert s["usage_summary"]["attempt_count"] == 7 and s["cost_usd"] == 1
    assert s["usage_summary"]["cost_usd"]["known"] == 1
    for name, original in interrupted.items():
        preserved = json.loads((run_dir / "attempts" / name).read_text(encoding="utf-8"))
        assert preserved["attempt_id"] == original["attempt_id"] and preserved["status"] == "interrupted"
    assert len({a["attempt_id"] for a in attempts}) == 7


def test_cli_detach_and_wait_json(rust_repo, monkeypatch, capsys):
    from rustflow.cli import main
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    assert main(["run", "--repo", str(rust_repo), "--detach", TASK]) == 0
    info = json.loads(capsys.readouterr().out)
    code = main(["wait", info["run_id"], "--repo", str(rust_repo), "--timeout", "120", "--json"])
    s = json.loads(capsys.readouterr().out)
    assert code == 0 and s["status"].startswith("approved")
    main(["report", "--repo", str(rust_repo)])
    brief = capsys.readouterr().out
    assert "next:" in brief and len(brief.splitlines()) < 40          # compact for an agent's context


def test_next_actions_cover_statuses():
    base = {"run_id": "r", "base_sha": "abcdef1234567", "branch": "rustflow/r", "isolation": "branch",
            "original_branch": "main", "pid": 0, "findings": [
                {"id": "F1", "severity": "P1", "status": "open", "title": "bug", "file": "a.rs", "line": 1}]}
    for st, expected in (("approved", "merge"), ("changes_requested", "ask_user"), ("stalled", "ask_user"),
                         ("max_rounds", "ask_user"), ("gates_red", "inspect"), ("baseline_red", "ask_user"),
                         ("budget", "resume"), ("interrupted", "resume"), ("review_failed", "inspect"),
                         ("no_changes", "ask_user"), ("running", "resume")):
        acts = runs.next_actions({**base, "status": st})
        assert any(a["action"] == expected for a in acts), (st, acts)   # running + dead pid = crashed -> resume
    acts = runs.next_actions({**base, "status": "approved", "escalate": True})
    assert any(a["action"] == "offer_audit" for a in acts)


def test_agents_cannot_start_rustflow(monkeypatch, capsys):
    from test_units import _guard
    assert _guard(monkeypatch, "implementer", "rustflow run --detach 'x'", capsys) == "deny"
    assert _guard(monkeypatch, "reviewer_a", "python3 -m rustflow review", capsys) == "deny"
    assert _guard(monkeypatch, "", "rustflow status --json", capsys) is None


def _mcp(requests):
    inp = io.BytesIO(b"".join((json.dumps(r) + "\n").encode() for r in requests))
    out = io.BytesIO()
    Server(stdin=inp, stdout=out).serve(join_timeout=None)
    msgs = [json.loads(l) for l in out.getvalue().decode().splitlines()]
    return {m["id"]: m for m in msgs if "id" in m}


def test_mcp_protocol_basics(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    res = _mcp([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "rustflow_triage", "arguments": {"task": "unsafe FFI wrapper with atomics"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
    ])
    assert res[1]["result"]["protocolVersion"] == "2025-06-18" and "tools" in res[1]["result"]["capabilities"]
    names = {t["name"] for t in res[2]["result"]["tools"]}
    assert {"rustflow_start", "rustflow_wait", "rustflow_status", "rustflow_cancel"} <= names
    for t in res[2]["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"
    tri = res[3]["result"]
    assert not tri["isError"] and tri["structuredContent"]["difficulty"] == "hard"
    assert "no run history" in tri["structuredContent"]["estimate"]["text"]
    assert res[4]["error"]["code"] == -32602 and res[5]["result"] == {} and res[6]["error"]["code"] == -32601


def test_mcp_start_and_wait(rust_repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(rust_repo))
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    res = _mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "rustflow_start", "arguments": {"mode": "run", "task": TASK}}}])
    started = res[1]["result"]["structuredContent"]
    assert started["status"] == "started"
    res = _mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "rustflow_wait", "arguments": {"run": started["run_id"], "timeout_s": 120}}}])
    s = res[2]["result"]["structuredContent"]
    assert s["status"].startswith("approved") and s["next_actions"]
    assert "next:" in res[2]["result"]["content"][0]["text"]


def test_estimate_from_history(tmp_path):
    runs_dir = tmp_path / ".rustflow" / "runs"
    for i, cost in enumerate((1.0, 3.0)):
        d = runs_dir / f"r{i}"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"status": "approved", "difficulty": "medium", "cost_usd": cost,
                                                  "agents": [{"spec": "claude:opus:high", "cost_usd": cost}]}))
    est = triage.estimate_cost(tmp_path, {"implementer": "claude:opus:high", "reviewer_b": "codex:gpt-6-astra:high"},
                               "medium")
    assert est["same_difficulty"] == {"n": 2, "mean_usd": 2.0, "max_usd": 3.0}
    assert est["per_call_usd"]["implementer"] == 2.0 and est["per_call_usd"]["reviewer_b"] is None


def test_installer_with_mcp_and_strict_agents(tmp_path, monkeypatch):
    from rustflow.agents import AgentCall, ClaudeRunner
    from rustflow.config import AgentSpec, Config, load_defaults
    from rustflow.installer import Installer
    from rustflow import procutil
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    Installer(tmp_path, with_rust_skills=False, python_cmd="python3", ci=False, with_mcp=True, out=lambda *_: None).run()
    mcp = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["rustflow"]
    assert mcp["args"][-1] == "mcp"
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "mcp__rustflow" in s["permissions"]["allow"] and s["enabledMcpjsonServers"] == ["rustflow"]
    assert (tmp_path / ".claude" / "skills" / "rustflow" / "SKILL.md").is_file()
    monkeypatch.setattr(procutil, "resolve_command", lambda name: ["claude"])
    call = AgentCall(role="implementer", name="i", spec=AgentSpec("claude", "opus", "high"), prompt="x", write=True,
                     schema=None, cwd=tmp_path, log_dir=tmp_path / "l")
    assert "--strict-mcp-config" in ClaudeRunner(Config(load_defaults())).build_argv(call)


def test_mcp_start_filters_flags_by_mode(monkeypatch, tmp_path):
    from rustflow import mcp_server
    seen = {}
    monkeypatch.setattr(mcp_server.runs, "spawn_detached",
                        lambda repo, args, task: seen.setdefault("args", args) and {"status": "started", "run_id": "r", "pid": 1})
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    text, info = mcp_server.t_start({"mode": "review", "base": "main", "fix": False, "allow_dirty": True, "task": "x"})
    assert seen["args"] == ["review", "--base", "main"] and info["ignored_for_mode"] == ["allow_dirty"]
