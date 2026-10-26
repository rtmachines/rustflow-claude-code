"""Results must describe one tree: gates that rewrite sources and reviews of a moving checkout are rejected."""

import json
import shutil
import subprocess

import pytest

from conftest import git
from rustflow import gates
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.rs").write_text("fn a() {}\n", encoding="utf-8")
    (repo / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def test_tree_stability_step(tmp_path):
    repo = _repo(tmp_path)
    t0 = gates.tree_fingerprint(repo)
    assert gates.tree_stability_step(repo, t0, t0, allow_lockfile=False) is None
    (repo / "Cargo.lock").write_text("# lock 2\n", encoding="utf-8")
    t1 = gates.tree_fingerprint(repo)
    assert gates.tree_stability_step(repo, t0, t1, allow_lockfile=True) is None
    step = gates.tree_stability_step(repo, t0, t1, allow_lockfile=False)
    assert step is not None and not step.ok and step.tail == "Cargo.lock"
    (repo / "gen.rs").write_text("// generated\n", encoding="utf-8")
    t2 = gates.tree_fingerprint(repo)
    step = gates.tree_stability_step(repo, t0, t2, allow_lockfile=True)
    assert step.name == "tree" and [d.file for d in step.diagnostics] == ["gen.rs"]
    # Signature names the file, so an accepted red baseline with the same writer stays comparable.
    res = gates.GateResult("full", False, t2, steps=[step])
    assert res.signatures() == {"tree|gen.rs||changed while the gates were running"}


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not available")
def test_build_script_writing_into_src_fails_full_gates(tmp_path, shared_target, monkeypatch):
    repo = tmp_path / "bs"
    subprocess.run(["cargo", "new", "--lib", "--vcs", "none", "-q", str(repo)], check=True)
    (repo / "src" / "lib.rs").write_text("//! demo\n", encoding="utf-8")
    (repo / "build.rs").write_text(
        'fn main() {\n    let d = std::env::var("CARGO_MANIFEST_DIR").unwrap();\n'
        '    std::fs::write(format!("{d}/src/stamp.txt"), "x").unwrap();\n}\n', encoding="utf-8")
    (repo / ".gitignore").write_text("target/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "bs-target"))  # fresh: build.rs must run
    res = gates.run_gates(repo, "full", strict=True, use_cache=False,
                          cfg={**gates.DEFAULT_GATES, "doc_tests": False, "semver": "off", "deny": "off"})
    tree = next(s for s in res.steps if s.name == "tree")
    assert not res.ok and not tree.ok and "src/stamp.txt" in tree.tail
    # The edit/stop profiles (hooks) do not pay for this check.
    (repo / "src" / "stamp.txt").unlink()
    res2 = gates.run_gates(repo, "stop", use_cache=False,
                           cfg={**gates.DEFAULT_GATES, "tests": "off", "tree_stable": True})
    assert all(s.name != "tree" for s in res2.steps)


def test_review_of_a_moving_tree_is_discarded_and_resumable(rust_repo, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "reviewer_touch")
    rc = Orchestrator(rust_repo, Config.load(rust_repo), Options()).start(TASK)
    run_dir = find_run(rust_repo, "latest")
    st = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert rc != 0 and st["status"] == "tree_changed", st["status"]
    assert st["phase"] == "gates" and st["round"] == 0 and st["rounds"] == []
    # Stop editing -> resume re-runs gates and the review on the current tree.
    git(rust_repo, "add", "-A")
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    assert Orchestrator(rust_repo, Config.load(rust_repo), Options()).resume(run_dir) == 0
    st = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["rounds"][0]["round"] == 1
