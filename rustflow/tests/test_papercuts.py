"""Small things a driving agent or a user trips over: leftover worktrees, waiters stealing each other's
progress lines, a broken rustflow.toml silently ignored by the hooks, an untrusted folder."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git, skip_agent_sleep
from rustflow import agents, gates, gitutil, hooks, runs
from rustflow.config import Config, load_defaults
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _run(repo, scenario, monkeypatch, config=None, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    skip_agent_sleep(monkeypatch)
    Orchestrator(repo, Config.load(repo, config), Options(**opts)).start(TASK)
    return json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


# ── worktree cleanup ─────────────────────────────────────────────────────────
@pytest.fixture
def finished_worktree(rust_repo):
    orch = Orchestrator(rust_repo, Config.load(rust_repo), Options())
    orch.run_dir = rust_repo / ".rustflow" / "runs" / "cleanup"
    orch.run_dir.mkdir(parents=True)
    wt = rust_repo / ".rustflow" / "wt" / "cleanup"
    gitutil.add_worktree(rust_repo, wt, "rustflow/cleanup", "HEAD")
    orch.state = {"run_id": "cleanup", "repo": str(rust_repo), "workdir": str(wt),
                  "branch": "rustflow/cleanup", "isolation": "worktree", "checkpoints": False,
                  "status": "stalled", "phase": "done", "round": 1}
    orch.save()
    return orch


@pytest.mark.parametrize("change", ["untracked", "tracked", "staged"])
def test_cleanup_keeps_changes_hidden_by_git_config(finished_worktree, change):
    orch = finished_worktree
    git(orch.repo, "config", "status.showUntrackedFiles", "no")
    path = orch.workdir / ("notes.txt" if change == "untracked" else "src/lib.rs")
    content = b"uncommitted work\n"
    path.write_bytes(content)
    if change == "staged":
        git(orch.workdir, "add", "src/lib.rs")
    orch._remove_finished_worktree()
    assert path.read_bytes() == content
    assert not orch.state.get("worktree_removed")


def test_git_refusal_keeps_worktree_logs_reason_and_releases_lock(finished_worktree):
    orch = finished_worktree
    git(orch.repo, "worktree", "lock", "--reason", "in use", str(orch.workdir))
    runs.acquire(orch.repo, orch.state)
    orch.loop()
    assert orch.workdir.is_dir() and not orch.state.get("worktree_removed")
    assert "worktree kept" in (orch.run_dir / "rustflow.log").read_text()
    assert not gates.active_lock_path(orch.repo).exists()


def test_cleanup_error_still_releases_lock(finished_worktree, monkeypatch):
    orch = finished_worktree
    runs.acquire(orch.repo, orch.state)

    def fail():
        raise OSError("cleanup failed")

    monkeypatch.setattr(orch, "_remove_finished_worktree", fail)
    with pytest.raises(OSError, match="cleanup failed"):
        orch.loop()
    assert not gates.active_lock_path(orch.repo).exists()


@pytest.fixture
def audit_worktree(finished_worktree):
    orch = finished_worktree
    results = orch.workdir / ".rust-review-results" / "audit-1"
    results.mkdir(parents=True)
    (results / "REPORT.md").write_bytes(("# Audyt\r\n" + "pełny wynik\n" * 1000).encode())
    (results / "details").mkdir()
    (results / "details" / "trace.bin").write_bytes(bytes(range(256)))
    orch.state["audit"] = {"report": ".rust-review-results/audit-1/REPORT.md", "excerpt": "fragment"}
    orch.save()
    assert not git(orch.workdir, "status", "--porcelain", "--untracked-files=all").strip()
    return orch


@pytest.mark.parametrize("absolute_report", [False, True])
def test_cleanup_archives_complete_audit_and_persists_paths(audit_worktree, absolute_report):
    orch = audit_worktree
    source = orch.workdir / ".rust-review-results"
    expected = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    if absolute_report:
        orch.state["audit"]["report"] = str(orch.workdir / orch.state["audit"]["report"])
    orch._remove_finished_worktree()
    assert not orch.workdir.exists()
    archive = orch.run_dir / "audit" / "results"
    for relative, content in expected.items():
        assert (archive / relative).read_bytes() == content
    st = json.loads((orch.run_dir / "state.json").read_text())
    rpt = archive / "audit-1" / "REPORT.md"
    assert st["audit"]["report"] == str(rpt.resolve())
    assert runs.summary(st)["audit_report"] == str(rpt.resolve())
    assert str(rpt.resolve()) in (orch.run_dir / "report.md").read_text()


@pytest.mark.parametrize("failure", ["copy", "state", "report"])
def test_failed_archive_keeps_source_and_cleanup_can_retry(audit_worktree, monkeypatch, failure):
    orch = audit_worktree
    source = orch.workdir / ".rust-review-results" / "audit-1" / "REPORT.md"
    expected = source.read_bytes()
    archive = orch.run_dir / "audit" / "results"
    archive.mkdir(parents=True)
    (archive / "previous.txt").write_bytes(b"earlier audit")

    def fail(*args, **kwargs):
        raise OSError("archive unavailable")

    with monkeypatch.context() as patch:
        if failure == "copy":
            patch.setattr(shutil, "copytree", fail)
        else:
            patch.setattr(orch, "save" if failure == "state" else "_write_report", fail)
        orch._remove_finished_worktree()
    assert source.read_bytes() == expected
    assert not orch.state.get("worktree_removed")
    assert "archive unavailable" in (orch.run_dir / "rustflow.log").read_text()
    # A partial archive is updated, while earlier results remain available.
    destination = archive / "audit-1" / "REPORT.md"
    destination.parent.mkdir(exist_ok=True)
    destination.write_bytes(b"partial")
    orch._remove_finished_worktree()
    assert not orch.workdir.exists()
    assert destination.read_bytes() == expected
    assert (archive / "previous.txt").read_bytes() == b"earlier audit"


def test_resume_restores_baseline_before_agents_and_stop_still_blocks_new_errors(
        finished_worktree, monkeypatch, capsys):
    orch = finished_worktree
    signature = "fmt|tests/ugly.rs|fmt|not rustfmt-formatted"
    orch.state["baseline_signatures"] = [signature]
    orch._remove_finished_worktree()
    assert not orch.workdir.exists()
    called = []

    def agent_phase():
        baseline = orch.workdir / ".rustflow" / "cache" / "baseline.json"
        assert json.loads(baseline.read_text()) == {"run": "cleanup", "signatures": [signature]}
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(orch.workdir))
        monkeypatch.setenv("RUSTFLOW_ROLE", "implementer")
        monkeypatch.setenv("RUSTFLOW_RUN", "cleanup")
        monkeypatch.delenv("RUSTFLOW_HOOKS", raising=False)
        diagnostic = gates.Diagnostic("error", "not rustfmt-formatted", code="fmt", file="tests/ugly.rs")
        result = gates.GateResult("stop", False, None, steps=[gates.Step("fmt", False, diagnostics=[diagnostic])])
        assert result.signatures() == {signature}
        monkeypatch.setattr(hooks.gates, "run_gates", lambda *a, **kw: result)
        capsys.readouterr()
        assert hooks.stop({"session_id": "accepted"}) == 0
        assert not capsys.readouterr().out
        diagnostic.file = "tests/new.rs"
        assert hooks.stop({"session_id": "new-error"}) == 0
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
        called.append(True)
        orch.state.update(phase="done", status="interrupted")

    monkeypatch.setattr(orch, "phase_gates", agent_phase)
    assert orch.resume(orch.run_dir, extra_rounds=1) == 2
    assert called == [True]
    assert not gates.active_lock_path(orch.repo).exists()


def test_baseline_write_failure_stops_resume_and_retry_restores_existing_worktree(finished_worktree, monkeypatch):
    orch = finished_worktree
    orch.state["baseline_signatures"] = ["accepted failure"]
    orch._remove_finished_worktree()
    called = []

    def agent_phase():
        data = json.loads((orch.workdir / ".rustflow/cache/baseline.json").read_text())
        assert data == {"run": "cleanup", "signatures": ["accepted failure"]}
        called.append(True)
        orch.state.update(phase="done", status="interrupted")

    monkeypatch.setattr(orch, "phase_gates", agent_phase)
    replace = os.replace

    def fail_baseline(src, dst):
        if Path(dst).name == "baseline.json":
            raise OSError("baseline unavailable")
        return replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_baseline)
        assert orch.resume(orch.run_dir, extra_rounds=1) == 2
    assert not called
    assert orch.workdir.is_dir()
    assert not (orch.workdir / ".rustflow/cache/baseline.json").exists()
    assert not gates.active_lock_path(orch.repo).exists()
    assert "baseline unavailable" in (orch.run_dir / "rustflow.log").read_text()
    assert json.loads((orch.run_dir / "state.json").read_text())["status"] == "stalled"
    assert orch.resume(orch.run_dir, extra_rounds=1) == 2
    assert called == [True] and not orch.state.get("worktree_removed")


def test_resume_without_signatures_does_not_create_baseline(finished_worktree, monkeypatch):
    orch = finished_worktree
    orch._remove_finished_worktree()

    def agent_phase():
        assert not (orch.workdir / ".rustflow/cache/baseline.json").exists()
        orch.state.update(phase="done", status="interrupted")

    monkeypatch.setattr(orch, "phase_gates", agent_phase)
    assert orch.resume(orch.run_dir, extra_rounds=1) == 2


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
