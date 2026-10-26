"""Hooks executed exactly like Claude Code does: a separate process, JSON on stdin, installed layout."""

import json
import subprocess
import sys


def hook(repo, sub, payload, role=None):
    env = {k: v for k, v in __import__("os").environ.items() if k != "RUSTFLOW_ROLE"}
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    if role:
        env["RUSTFLOW_ROLE"] = role
    return subprocess.run([sys.executable, str(repo / ".rustflow" / "hooks.py"), sub],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=repo)


def test_post_edit_reports_compile_errors(rust_repo):
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 { \"x\" }\n")
    r = hook(rust_repo, "post-edit", {"tool_input": {"file_path": str(rust_repo / "src" / "lib.rs")}})
    assert r.returncode == 2
    assert "E0308" in r.stderr and "lib.rs" in r.stderr
    assert not (rust_repo / ".rustflow" / "__pycache__").exists()


def test_stop_blocks_then_gives_up(rust_repo):
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 {\n    return 1;\n}\n")
    payload = {"session_id": "s1", "hook_event_name": "Stop"}
    outs = [hook(rust_repo, "stop", payload) for _ in range(4)]
    decisions = [json.loads(o.stdout) for o in outs]
    assert all(d.get("decision") == "block" for d in decisions[:3])
    assert "needless_return" in decisions[0]["reason"] or "needless return" in decisions[0]["reason"]
    assert "systemMessage" in decisions[3]          # loop guard: 4th time lets the agent stop


def test_stop_green_and_reviewer_skip(rust_repo):
    (rust_repo / "src" / "lib.rs").write_text("/// f\npub fn f() -> i32 {\n    1\n}\n")
    assert hook(rust_repo, "stop", {"session_id": "s2"}).stdout == ""
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 { return 1; }\n")
    assert hook(rust_repo, "stop", {"session_id": "s3"}, role="reviewer_a").stdout == ""


def test_interactive_commit_gate(rust_repo):
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 {\n    return 1;\n}\n")
    r = hook(rust_repo, "guard-bash", {"tool_input": {"command": "git commit -am wip"}})
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "RED" in out["permissionDecisionReason"]
