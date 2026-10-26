"""Repositories that are not rustfmt-clean: gates format only what the change touched, fmt failures are
itemized per file (so a red baseline can accept them), and a preflight stop leaves the checkout as it was."""

import json
import subprocess
from pathlib import Path

from conftest import git
from rustflow import gates, runs
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."
UGLY = "#[test]\nfn   ugly( ){assert_eq!(1,1);}\n"      # a test target root: `cargo fmt --all` formats it


def _run(repo, monkeypatch, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    rc = Orchestrator(repo, Config.load(repo), Options(**opts)).start(TASK)
    return rc, json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


def _unformatted_base(repo):
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "ugly.rs").write_text(UGLY)
    subprocess.run(["cargo", "generate-lockfile", "--offline", "-q"], cwd=repo, check=True)  # base = a real repo
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unformatted file")


def test_fmt_check_output_is_itemized_per_file(tmp_path):
    out = (f"Diff in {tmp_path / 'src' / 'a.rs'}:1:\n-fn   x( ){{}}\n+fn x() {{}}\n"
           f"Diff in {tmp_path / 'src' / 'a.rs'}:9:\n-x\n+y\n"
           f"Diff in {tmp_path / 'tests' / 'b.rs'} at line 3:\n-z\n+w\n")
    diags = gates.parse_fmt_check(out, tmp_path)
    assert [d.file for d in diags] == ["src/a.rs", "tests/b.rs"]
    step = gates.Step("fmt", False, diagnostics=diags)
    sigs = gates.GateResult("full", False, None, steps=[step]).signatures()
    assert sigs == {"fmt|src/a.rs|fmt|not rustfmt-formatted", "fmt|tests/b.rs|fmt|not rustfmt-formatted"}


def test_check_mode_reports_files_and_fix_mode_touches_only_changed_files(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    res = gates.run_gates(rust_repo, "edit", strict=True)
    fmt = res.steps[0]
    assert not fmt.ok and [d.file for d in fmt.diagnostics] == ["tests/ugly.rs"]
    (rust_repo / "src" / "lib.rs").write_text("pub fn  f( )->i32{1}\n")     # the change: unformatted too
    monkeypatch.delenv("RUSTFLOW_BASE", raising=False)                     # interactive: changed vs HEAD
    res = gates.run_gates(rust_repo, "edit", use_cache=False)
    assert res.steps[0].ok, res.feedback()
    assert "pub fn f() -> i32 {" in (rust_repo / "src" / "lib.rs").read_text()
    assert (rust_repo / "tests" / "ugly.rs").read_text() == UGLY


def test_fmt_fix_scope_all_keeps_the_old_behaviour(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    cfg = (rust_repo / "rustflow.toml").read_text()
    (rust_repo / "rustflow.toml").write_text(cfg.replace('fmt_fix_scope = "changed"', 'fmt_fix_scope = "all"', 1))
    gates.run_gates(rust_repo, "edit", use_cache=False)
    assert (rust_repo / "tests" / "ugly.rs").read_text() != UGLY


def test_red_fmt_baseline_is_accepted_without_reformatting_the_repo(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    rc, st = _run(rust_repo, monkeypatch, accept_red_baseline=True)
    assert st["baseline_signatures"] == ["fmt|tests/ugly.rs|fmt|not rustfmt-formatted"]
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert (rust_repo / "tests" / "ugly.rs").read_text() == UGLY         # pre-existing file left alone
    changed = git(rust_repo, "diff", "--name-only", st["base_sha"], "HEAD").split()
    assert changed == ["src/lib.rs"]                                       # the diff is only the task


def test_preflight_stop_leaves_the_checkout_on_the_original_branch(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    rc, st = _run(rust_repo, monkeypatch)
    assert st["status"] == "baseline_red"
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert "rustflow/" not in git(rust_repo, "branch", "--list")
    assert not st.get("branch")
    question = runs.next_actions(st)[0]["question"]
    assert "cargo fmt --all" in question and "tests/ugly.rs" in question  # fmt-only base: say how to fix it


def test_preflight_stop_removes_the_worktree(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    rc, st = _run(rust_repo, monkeypatch, isolation="worktree")
    assert st["status"] == "baseline_red"
    assert not (rust_repo / ".rustflow" / "wt" / st["run_id"]).exists()
    assert "rustflow/" not in git(rust_repo, "branch", "--list")
    assert Path(st["workdir"]) == rust_repo


def test_changed_file_with_a_syntax_error_fails_fmt_and_stays_as_written(rust_repo, monkeypatch):
    monkeypatch.delenv("RUSTFLOW_BASE", raising=False)
    broken = "pub fn f( {\n"
    (rust_repo / "src" / "lib.rs").write_text(broken)
    step = gates.GateRunner(rust_repo, gates.load_gate_config(rust_repo)).fmt(fix=True)
    assert not step.ok and "src/lib.rs" in " ".join(step.cmd) and "unclosed delimiter" in step.tail
    assert (rust_repo / "src" / "lib.rs").read_text() == broken


def test_touched_file_unformatted_at_base_is_left_as_written(rust_repo, monkeypatch):
    """Formatting a file that was not rustfmt-clean at the base would put a reformat of the whole file into
    the diff (measured: +542/-140 instead of +103 on a real library). Clean-at-base and new files are formatted."""
    _unformatted_base(rust_repo)
    monkeypatch.delenv("RUSTFLOW_BASE", raising=False)
    edited = UGLY + "#[test]\nfn   added( ){}\n"
    (rust_repo / "tests" / "ugly.rs").write_text(edited)
    (rust_repo / "src" / "lib.rs").write_text("pub fn  f( )->i32{1}\n")        # clean at base -> formatted
    (rust_repo / "tests" / "new.rs").write_text("#[test]\nfn   fresh( ){}\n")   # new file -> formatted
    res = gates.run_gates(rust_repo, "edit", use_cache=False)
    assert res.steps[0].ok, res.feedback()
    assert (rust_repo / "tests" / "ugly.rs").read_text() == edited
    assert "pub fn f() -> i32 {" in (rust_repo / "src" / "lib.rs").read_text()
    assert "fn fresh() {}" in (rust_repo / "tests" / "new.rs").read_text()


def test_fmt_fix_scope_touched_formats_whole_touched_files(rust_repo, monkeypatch):
    _unformatted_base(rust_repo)
    monkeypatch.delenv("RUSTFLOW_BASE", raising=False)
    cfg = (rust_repo / "rustflow.toml").read_text()
    (rust_repo / "rustflow.toml").write_text(cfg.replace('fmt_fix_scope = "changed"', 'fmt_fix_scope = "touched"', 1))
    (rust_repo / "tests" / "ugly.rs").write_text(UGLY + "\n")
    gates.run_gates(rust_repo, "edit", use_cache=False)
    assert "fn ugly() {" in (rust_repo / "tests" / "ugly.rs").read_text()


def test_post_edit_hook_leaves_a_file_unformatted_at_base(rust_repo):
    import os
    import sys
    _unformatted_base(rust_repo)
    edited = UGLY + "#[test]\nfn   added( ){}\n"
    target = rust_repo / "tests" / "ugly.rs"
    target.write_text(edited)
    env = {k: v for k, v in os.environ.items() if k not in ("RUSTFLOW_ROLE", "RUSTFLOW_BASE")}
    env["CLAUDE_PROJECT_DIR"] = str(rust_repo)
    r = subprocess.run([sys.executable, str(rust_repo / ".rustflow" / "hooks.py"), "post-edit"],
                       input=json.dumps({"session_id": "h", "tool_input": {"file_path": str(target)}}),
                       capture_output=True, text=True, env=env, cwd=rust_repo)
    assert r.returncode == 0, r.stderr
    assert target.read_text() == edited
