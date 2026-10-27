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


# ── gate integrity in interactive mode (HEAD config, scan + proof) ───────────
import subprocess  # noqa: E402

GOOD_LIB = "/// f\npub fn f() -> i32 {\n    1\n}\n"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _commit_lib(repo, text=GOOD_LIB):
    (repo / "src" / "lib.rs").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "lib")


def _stop(repo, sid, role=None, **_):
    return hook(repo, "stop", {"session_id": sid}, role=role)


def test_stop_uses_head_config(rust_repo):
    _commit_lib(rust_repo)
    cfg = rust_repo / "rustflow.toml"
    cfg.write_text(cfg.read_text() + '\n[gates]\ntests = "off"\n')
    (rust_repo / "src" / "lib.rs").write_text(GOOD_LIB + "#[cfg(test)]\nmod t {\n    #[test]\n    fn bad() {\n        assert!(false);\n    }\n}\n")
    assert json.loads(_stop(rust_repo, "a").stdout)["decision"] == "block"


def test_stop_blocks_masking_allow(rust_repo):
    _commit_lib(rust_repo)
    (rust_repo / "src" / "lib.rs").write_text(
        "/// f\n#[allow(clippy::needless_return)]\npub fn f() -> i32 {\n    return 1;\n}\n")
    d = json.loads(_stop(rust_repo, "b").stdout)
    assert d["decision"] == "block" and "Gate-affecting" in d["reason"]


def test_stop_passes_unneeded_allow(rust_repo):
    _commit_lib(rust_repo, "/// f\npub fn f() -> i32 {\n    g()\n}\nfn g() -> i32 {\n    1\n}\n")
    (rust_repo / "src" / "lib.rs").write_text(
        "/// f\npub fn f() -> i32 {\n    g()\n}\n#[allow(dead_code)]\nfn g() -> i32 {\n    1\n}\n")
    out = _stop(rust_repo, "c").stdout
    assert out == "" or "decision" not in json.loads(out)


def test_stop_runs_on_config_only_change(rust_repo):
    _commit_lib(rust_repo, "/// f\npub fn f() -> i32 {\n    return 1;\n}\n")   # HEAD code only passes with the lint masked
    (rust_repo / ".cargo").mkdir()
    (rust_repo / ".cargo" / "config.toml").write_text('[build]\nrustflags = ["--cap-lints", "allow"]\n')
    d = json.loads(_stop(rust_repo, "d").stdout)
    assert d["decision"] == "block" and "Gate-affecting" in d["reason"]


def test_commit_gate_denies_gate_change(rust_repo):
    _commit_lib(rust_repo, GOOD_LIB + "#[cfg(test)]\nmod t {\n    #[test]\n    fn ok() {}\n}\n")
    (rust_repo / "src" / "lib.rs").write_text(GOOD_LIB)
    r = hook(rust_repo, "guard-bash", {"tool_input": {"command": "git commit -am x"}})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_head_skips_integrity(tmp_path, shared_target):
    import shutil
    if shutil.which("cargo") is None:
        import pytest
        pytest.skip("cargo not available")
    repo = tmp_path / "nohead"
    subprocess.run(["cargo", "new", "--lib", "--vcs", "none", "-q", str(repo)], check=True)
    (repo / "src" / "lib.rs").write_text(GOOD_LIB)
    (repo / ".gitignore").write_text("target/\n")
    _git(repo, "init", "-q", "-b", "main")
    from rustflow.installer import Installer
    Installer(repo, with_rust_skills=False, ci=False, out=lambda *_: None, python_cmd=sys.executable).run()
    d = json.loads(hook(repo, "stop", {"session_id": "n"}).stdout)
    assert d.get("decision") != "block" and "no commit yet" in d["systemMessage"]


def test_integrity_exception_blocks(rust_repo):
    _commit_lib(rust_repo)
    (rust_repo / ".rustflow" / "integrity.py").write_text(
        "def scan(*a, **k):\n    raise RuntimeError('boom')\n\ndef is_config_path(*a, **k):\n    return False\n")
    (rust_repo / "src" / "lib.rs").write_text(GOOD_LIB + "// touched\n")
    d = json.loads(_stop(rust_repo, "e").stdout)
    assert d["decision"] == "block" and "boom" in d["reason"]


def test_head_config_git_error_blocks(rust_repo):
    env_hook = subprocess.run(
        [sys.executable, str(rust_repo / ".rustflow" / "hooks.py"), "stop"],
        input=json.dumps({"session_id": "g"}), capture_output=True, text=True, cwd=rust_repo,
        env={**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(rust_repo), "RUSTFLOW_ROLE": "implementer",
             "RUSTFLOW_BASE": "no-such-rev"})
    d = json.loads(env_hook.stdout)
    assert d["decision"] == "block" and "no-such-rev" in d["reason"]


def test_audit_regressions_mode_a(rust_repo):
    _commit_lib(rust_repo, "/// f\npub fn f() -> i32 {\n    return 1;\n}\n")
    cfg = rust_repo / "rustflow.toml"
    cfg.write_text(cfg.read_text() + '\n[gates]\ntests = "off"\nclippy_args = []\n')
    assert json.loads(_stop(rust_repo, "w1").stdout)["decision"] == "block"
    _git(rust_repo, "checkout", "--", "rustflow.toml")
    (rust_repo / ".cargo").mkdir(exist_ok=True)
    (rust_repo / ".cargo" / "config.toml").write_text('[build]\nrustflags = ["--cap-lints", "allow"]\n')
    assert json.loads(_stop(rust_repo, "w2").stdout)["decision"] == "block"
