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
    d = json.loads(_stop(rust_repo, "a").stdout)
    assert d["decision"] == "block" and "rustflow gates failed" in d["reason"]


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
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "tests-removed" in out["permissionDecisionReason"]


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
    assert "could not run" in d["reason"] and "rustflow doctor" in d["reason"]
    assert "Revert them" not in d["reason"]


def test_missing_integrity_module_names_file(monkeypatch, git_repo):
    import pytest
    from rustflow import hooks
    monkeypatch.setitem(sys.modules, "integrity", None)
    monkeypatch.setitem(sys.modules, "rustflow.integrity", None)
    monkeypatch.delattr(__import__("rustflow"), "integrity", raising=False)
    with pytest.raises(RuntimeError, match=r"\.rustflow/integrity\.py"):
        hooks._integrity()
    # a repo with a commit: without HEAD the verdict returns "no commit yet" before importing integrity
    ok, why, internal = hooks._integrity_verdict(git_repo, {})
    assert not ok and internal and ".rustflow/integrity.py" in why and "rustflow doctor" in why


def _env(repo, **extra):
    return {**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(repo), **extra}


def test_orchestrated_role_has_no_integrity_block(rust_repo):
    _commit_lib(rust_repo)
    base = _git(rust_repo, "rev-parse", "HEAD").strip()
    (rust_repo / "src" / "lib.rs").write_text(
        "/// f\n#[allow(clippy::needless_return)]\npub fn f() -> i32 {\n    return 1;\n}\n")
    r = subprocess.run([sys.executable, str(rust_repo / ".rustflow" / "hooks.py"), "stop"],
                       input=json.dumps({"session_id": "o"}), capture_output=True, text=True, cwd=rust_repo,
                       env=_env(rust_repo, RUSTFLOW_ROLE="implementer", RUSTFLOW_BASE=base))
    assert "Gate-affecting" not in r.stdout and "decision" not in r.stdout


def test_commit_gate_config_error_denies(rust_repo, monkeypatch, capsys):
    from rustflow import hooks
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(rust_repo))
    monkeypatch.delenv("RUSTFLOW_ROLE", raising=False)

    def boom(root, rev):
        raise hooks.gates.GateConfigError("cannot resolve revision 'HEAD'")
    monkeypatch.setattr(hooks.gates, "load_gate_config_at", boom)
    assert hooks.guard_bash({"tool_input": {"command": "git commit -am x"}}) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "cannot read the gate configuration" in out["permissionDecisionReason"]


def test_stop_runs_on_rename_of_config_side(rust_repo):
    _commit_lib(rust_repo, GOOD_LIB + "#[cfg(test)]\nmod t {\n    #[test]\n    fn bad() {\n        assert!(false);\n    }\n}\n")
    (rust_repo / "tests").mkdir()
    (rust_repo / "tests" / "it.rs").write_text("#[test]\nfn t() {}\n")
    _git(rust_repo, "add", "-A")
    _git(rust_repo, "commit", "-qm", "it")
    _git(rust_repo, "mv", "tests/it.rs", "tests/it.rs.off")
    assert json.loads(_stop(rust_repo, "r").stdout)["decision"] == "block"


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


# ── I2: gitignored config files are visible to the integrity check ──────────
MASKED_LIB = "/// f\npub fn f() -> i32 {\n    return 1;\n}\n"
CAP_LINTS = '[build]\nrustflags = ["--cap-lints", "allow"]\n'


def _ignore_cargo_dir(repo):
    gi = repo / ".gitignore"
    gi.write_text(gi.read_text() + ".cargo/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore .cargo")


def _ignored_config(repo, text):
    (repo / ".cargo").mkdir(exist_ok=True)
    (repo / ".cargo" / "config.toml").write_text(text)


def test_ignored_cargo_config_masking_lints_blocks_stop(rust_repo):
    _commit_lib(rust_repo, MASKED_LIB)
    _ignore_cargo_dir(rust_repo)
    _ignored_config(rust_repo, CAP_LINTS)
    d = json.loads(_stop(rust_repo, "i1").stdout)
    assert d["decision"] == "block" and "Gate-affecting" in d["reason"] and ".cargo/config.toml" in d["reason"]


def test_ignored_cargo_config_blocks_commit_gate(rust_repo):
    _commit_lib(rust_repo, MASKED_LIB)
    _ignore_cargo_dir(rust_repo)
    _ignored_config(rust_repo, CAP_LINTS)
    (rust_repo / "README.md").write_text("x\n")
    r = hook(rust_repo, "guard-bash", {"tool_input": {"command": "git commit -am x"}})
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "Commit blocked" in out["permissionDecisionReason"]


def test_harmless_ignored_config_passes_with_a_note(rust_repo):
    _commit_lib(rust_repo)
    _ignore_cargo_dir(rust_repo)
    _ignored_config(rust_repo, "[term]\nverbose = false\n")
    d = json.loads(_stop(rust_repo, "i2").stdout)
    assert d.get("decision") != "block" and "proven harmless" in d["systemMessage"]


def test_gate_cache_not_reused_after_ignored_config_changes(rust_repo):
    _commit_lib(rust_repo, MASKED_LIB)
    _ignore_cargo_dir(rust_repo)
    _ignored_config(rust_repo, CAP_LINTS)
    assert json.loads(_stop(rust_repo, "i3a").stdout)["decision"] == "block"   # gates green under the mask, cached
    (rust_repo / ".cargo" / "config.toml").unlink()
    d = json.loads(_stop(rust_repo, "i3b").stdout)
    assert d["decision"] == "block" and "rustflow gates failed" in d["reason"]   # not served from the cache
