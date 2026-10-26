"""Small things a driving agent or a user trips over: leftover worktrees, waiters stealing each other's
progress lines, a broken rustflow.toml silently ignored by the hooks, an untrusted folder."""

import json
import os
import subprocess
import sys

from conftest import git
from rustflow import agents, runs
from rustflow.config import Config, load_defaults
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _run(repo, scenario, monkeypatch, config=None, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    Orchestrator(repo, Config.load(repo, config), Options(**opts)).start(TASK)
    return json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


# ── worktree cleanup ─────────────────────────────────────────────────────────
def test_finished_worktree_run_removes_its_clean_worktree(rust_repo, monkeypatch):
    st = _run(rust_repo, "happy", monkeypatch, isolation="worktree")
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["worktree_removed"] is True and not os.path.isdir(st["workdir"])
    assert ".trim()" in git(rust_repo, "show", f"{st['branch']}:src/lib.rs")     # the work lives on the branch
    merge = next(a for a in runs.next_actions(st) if a["action"] == "merge")
    assert "worktree remove" not in merge.get("note", "")


def test_worktree_with_uncommitted_work_is_kept(rust_repo, monkeypatch, tmp_path):
    cfg = tmp_path / "rf.toml"
    cfg.write_text((rust_repo / "rustflow.toml").read_text().replace("checkpoint_commits = true",
                                                                     "checkpoint_commits = false"))
    st = _run(rust_repo, "happy", monkeypatch, config=cfg, isolation="worktree")
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert not st.get("worktree_removed") and os.path.isdir(st["workdir"])
    merge = next(a for a in runs.next_actions(st) if a["action"] == "merge")
    assert "worktree remove" in merge["note"] and "commit" in merge["note"]


def test_extra_rounds_recreate_a_removed_worktree(rust_repo, monkeypatch):
    st = _run(rust_repo, "stall", monkeypatch, isolation="worktree")
    assert st["status"] == "stalled" and st["worktree_removed"]
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    rc = Orchestrator(rust_repo, Config.load(rust_repo), Options()).resume(find_run(rust_repo, "latest"),
                                                                          extra_rounds=2)
    st2 = json.loads((find_run(rust_repo, "latest") / "state.json").read_text(encoding="utf-8"))
    assert rc == 0 and st2["status"].startswith("approved"), st2.get("stop_reason")
    assert ".trim()" in git(rust_repo, "show", f"{st2['branch']}:src/lib.rs")


# ── waiters ──────────────────────────────────────────────────────────────────
def test_cli_and_mcp_waiters_each_get_every_progress_line(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    (run / "rustflow.log").write_text("[10:00:00] one\n[10:00:01] two\n", encoding="utf-8")
    (run / "state.json").write_text(json.dumps({"run_id": "r", "status": "approved", "pid": 0}), encoding="utf-8")
    cli, mcp, again = [], [], []
    runs.wait(run, 1, out=cli.append, channel="cli")
    runs.wait(run, 1, out=mcp.append, channel="mcp")
    runs.wait(run, 1, out=again.append, channel="cli")
    assert len(cli) == len(mcp) == 2 and again == []


# ── broken rustflow.toml ────────────────────────────────────────────────────
def test_stop_hook_says_when_rustflow_toml_is_ignored(rust_repo):
    cfg = rust_repo / "rustflow.toml"
    cfg.write_text(cfg.read_text().replace("[gates]\n", "[gates]\nedit_check = true\n", 1))   # duplicate key
    env = {k: v for k, v in os.environ.items() if k != "RUSTFLOW_ROLE"}
    env.update(CLAUDE_PROJECT_DIR=str(rust_repo), RUSTFLOW_ROLE="implementer")
    r = subprocess.run([sys.executable, str(rust_repo / ".rustflow" / "hooks.py"), "stop"],
                       input=json.dumps({"session_id": "c"}), capture_output=True, text=True, env=env, cwd=rust_repo)
    out = json.loads(r.stdout)
    assert "rustflow.toml" in out["systemMessage"] and "default" in out["systemMessage"]


# ── untrusted folder ────────────────────────────────────────────────────────
def _doctor_lines(tmp_path, monkeypatch, projects):
    from rustflow.doctor import Doctor
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({"projects": projects}), encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    (repo / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["mcp__rustflow"]}}')
    lines: list[str] = []
    Doctor(repo, Config(load_defaults()), out=lines.append).check_trust()
    return repo, lines


def test_doctor_warns_about_an_untrusted_folder(tmp_path, monkeypatch):
    repo, lines = _doctor_lines(tmp_path, monkeypatch, {})
    assert any("[WARN]" in l and "trust" in l and "permissions.allow" in l for l in lines), lines


def test_doctor_accepts_a_trusted_folder_or_parent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _, lines = _doctor_lines(tmp_path, monkeypatch, {str(repo.resolve()): {"hasTrustDialogAccepted": True}})
    assert any("[OK" in l and "trust" in l for l in lines), lines
    _, lines = _doctor_lines(tmp_path, monkeypatch, {str(tmp_path.resolve()): {"hasTrustDialogAccepted": True}})
    assert not any("[WARN]" in l for l in lines), lines
