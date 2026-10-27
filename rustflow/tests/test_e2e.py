"""End-to-end: real git + real cargo, fake claude/codex executables."""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import git, read_log
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def run(repo, scenario, monkeypatch, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    cfg = Config.load(repo, opts.pop("config", None))
    rc = Orchestrator(repo, cfg, Options(**opts)).start(TASK)
    state = json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))
    return rc, state


def test_happy_path(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "happy", monkeypatch)
    assert st["status"] == "approved_with_advisories", st.get("stop_reason")
    assert rc == 0
    assert st["difficulty"] == "easy"
    assert len(st["rounds"]) == 2
    r1 = st["rounds"][0]
    # Same bug from both reviewers merged and confirmed; hallucinated file dropped; P3 advisory.
    f1 = next(f for f in r1["findings"] if f["id"] == "F1")
    assert sorted(f1["reviewers"]) == ["reviewer_a", "reviewer_b"]
    assert any(d["file"] == "src/does_not_exist.rs" for d in r1["dropped"])
    assert any(f["severity"] == "P3" and f["status"] == "advisory" and f["file"] == "src/lib.rs"
               for f in r1["findings"])
    # Acceptance "not_met" from both reviewers merged into one blocking finding.
    assert sum(1 for f in r1["findings"] if f["category"] == "acceptance") == 1
    assert st["rounds"][1]["verdicts"] == {"reviewer_a": "APPROVE", "reviewer_b": "APPROVE"}
    assert st["final_gates_ok"] is True
    # Git: dedicated branch with checkpoint commits, original branch untouched.
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == st["branch"]
    log = git(rust_repo, "log", "--format=%s", f"main..{st['branch']}")
    assert "implement" in log and "fix round 1" in log
    assert ".trim()" in (rust_repo / "src" / "lib.rs").read_text()
    # Agent invocations: argv built per provider and role.
    calls = read_log(rust_repo)
    impl = next(c for c in calls if c["role"] == "implementer")
    assert impl["provider"] == "claude"
    assert impl["argv"][impl["argv"].index("--model") + 1] == "sonnet"
    assert impl["argv"][impl["argv"].index("--permission-mode") + 1] == "acceptEdits"
    assert impl["prompt_head"].startswith("/rustflow-implement")
    rb = next(c for c in calls if c["role"] == "reviewer_b")
    assert rb["provider"] == "codex"
    assert rb["argv"][rb["argv"].index("-s") + 1] == "read-only"
    assert 'model_reasoning_effort="medium"' in rb["argv"]
    assert rb["prompt_head"].startswith("$rustflow-review")
    ra = next(c for c in calls if c["role"] == "reviewer_a")
    assert ra["argv"][ra["argv"].index("--permission-mode") + 1] == "dontAsk"
    report = (find_run(rust_repo, "latest") / "report.md").read_text(encoding="utf-8")
    assert "ZATWIERDZONE" in report and "F1" in report


def test_gate_fix_before_review(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "gatefix", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    roles = [c["role"] for c in read_log(rust_repo)]
    # implementer -> (gates red) -> fixer gate-fix -> reviewers ... reviewers never saw red code.
    assert roles[:2] == ["implementer", "fixer"]
    assert "reviewer_a" in roles[2:4]
    gate_files = sorted(p.name for p in (find_run(rust_repo, "latest") / "gates").glob("after-implement*.json"))
    first = json.loads((find_run(rust_repo, "latest") / "gates" / gate_files[0]).read_text())
    assert first["ok"] is False


def test_stall_detected(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "stall", monkeypatch)
    assert st["status"] == "stalled"
    assert rc == 2


def test_degraded_review_when_codex_fails(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "codex_down", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["rounds"][0]["failed_reviewers"] == ["reviewer_b"]


def test_dry_run_calls_nothing(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "happy", monkeypatch, dry_run=True)
    assert read_log(rust_repo) == []
    agents_dir = find_run(rust_repo, "latest") / "agents"
    assert (agents_dir / "implement.prompt.md").is_file()
    argv = json.loads((agents_dir / "r1-reviewer_b.argv.json").read_text())
    assert argv[argv.index("-s") + 1] == "read-only"
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_red_baseline_stops(rust_repo, monkeypatch):
    (rust_repo / "src" / "lib.rs").write_text("pub fn f() -> i32 { return 1; }\n")
    git(rust_repo, "commit", "-qam", "red")
    rc, st = run(rust_repo, "happy", monkeypatch)
    assert st["status"] == "baseline_red"
    assert read_log(rust_repo) == []


def test_gates_use_base_config(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken_gates", monkeypatch, difficulty="easy")
    assert st["status"] == "gates_red" and st["gates_cfg"]["tests"] == "auto"


def test_red_baseline_counts_occurrences(rust_repo, monkeypatch):
    (rust_repo / "src" / "lib.rs").write_text("pub fn a() -> i32 { return 1; }\n")
    git(rust_repo, "commit", "-qam", "red")
    rc, st = run(rust_repo, "red_baseline_multiset", monkeypatch, difficulty="easy", accept_red_baseline=True)
    assert st["status"] == "gates_red"
    # the second identical diagnostic is NEW: the code never reaches review
    assert not any(c["role"].startswith("reviewer") for c in read_log(rust_repo))
    assert st["baseline_signatures"].count("clippy|src/lib.rs|clippy::needless_return|unneeded `return` statement") == 1


def test_resume_without_gates_cfg(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "happy", monkeypatch, max_rounds=1)
    path = find_run(rust_repo, "latest") / "state.json"
    st.pop("gates_cfg")
    path.write_text(json.dumps(st), encoding="utf-8")
    Orchestrator(rust_repo, Config.load(rust_repo), Options()).resume(find_run(rust_repo, "latest"), extra_rounds=1)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert "gates_cfg" in after


def test_dirty_tree_refused(rust_repo, monkeypatch):
    (rust_repo / "notes.txt").write_text("wip")
    rc, st = run(rust_repo, "happy", monkeypatch)
    assert st["status"] == "error" and "dirty" in st["stop_reason"]


def test_budget_stop_and_resume(rust_repo, monkeypatch, tmp_path):
    low = tmp_path / "low.toml"
    low.write_text((rust_repo / "rustflow.toml").read_text().replace("max_total_usd = 60.0", "max_total_usd = 0.4"))
    rc, st = run(rust_repo, "happy", monkeypatch, config=low)
    assert st["status"] == "budget"
    phase = st["phase"]
    assert phase != "done"
    high = tmp_path / "high.toml"
    high.write_text((rust_repo / "rustflow.toml").read_text())
    rc2 = Orchestrator(rust_repo, Config.load(rust_repo, high), Options()).resume(find_run(rust_repo, "latest"))
    st2 = json.loads((find_run(rust_repo, "latest") / "state.json").read_text())
    assert st2["status"].startswith("approved"), st2.get("stop_reason")
    assert rc2 == 0


def test_review_mode_on_uncommitted_changes(rust_repo, monkeypatch):
    from fake_agent import BUGGY
    (rust_repo / "src" / "lib.rs").write_text(BUGGY)
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    cfg = Config.load(rust_repo)
    rc = Orchestrator(rust_repo, cfg, Options(mode="review", fix=False)).start("review my change")
    st = json.loads((find_run(rust_repo, "latest") / "state.json").read_text())
    assert st["status"] == "changes_requested"
    assert st["isolation"] == "none"
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert "implementer" not in [c["role"] for c in read_log(rust_repo)]


def test_cli_triage_and_gates(rust_repo):
    import sys
    out = subprocess.run([sys.executable, "-m", "rustflow", "triage", "--repo", str(rust_repo),
                          "fix", "typo", "in", "README"], capture_output=True, text=True)
    assert "difficulty: easy" in out.stdout
    out = subprocess.run([sys.executable, str(rust_repo / ".rustflow" / "gates.py"), "run", "--profile", "stop",
                          "--root", str(rust_repo)], capture_output=True, text=True)
    assert out.returncode == 0 and "ALL GREEN" in out.stdout, out.stdout + out.stderr


@pytest.fixture(autouse=True)
def _helpers_path(monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "fake_agents"))


def test_worktree_isolation_keeps_main_checkout_untouched(rust_repo, monkeypatch):
    before = (rust_repo / "src" / "lib.rs").read_text()
    rc, st = run(rust_repo, "happy", monkeypatch, isolation="worktree")
    assert st["status"].startswith("approved"), st.get("stop_reason")
    wt = st["workdir"]
    assert wt.replace("\\", "/").endswith(f".rustflow/wt/{st['run_id']}")
    assert (rust_repo / "src" / "lib.rs").read_text() == before          # main checkout untouched
    from pathlib import Path
    assert ".trim()" in git(rust_repo, "show", f"{st['branch']}:src/lib.rs")   # work is on the run branch
    assert st["worktree_removed"] and not Path(wt).exists()                    # clean worktree cleaned up
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(rust_repo, "status", "--porcelain").strip() == ""          # wt/ and runs/ are ignored
    assert all(c["cwd"] == wt for c in read_log(rust_repo))


def test_dispute_protocol_reaches_human_not_stall(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "dispute", monkeypatch)
    assert st["status"] == "approved_with_advisories", st.get("stop_reason")
    f1 = next(f for f in st["findings"] if f["id"] == "F1")
    assert f1["status"] == "disputed" and f1["dispute_count"] == 2
    assert len(st["rounds"]) == 3
    assert "Sporne" in (find_run(rust_repo, "latest") / "report.md").read_text(encoding="utf-8")


def test_stall_detected_without_previous_id(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "nolabel", monkeypatch)
    assert st["status"] == "stalled"
    r2 = st["rounds"][1]["findings"]
    assert any(f["id"] == "F1" and f["status"] == "open" and f["previous_id"] == "F1" for f in r2)  # linked by similarity


def test_report_only_review_never_touches_tree(rust_repo, monkeypatch):
    ugly = "pub fn f()->i32{return 1;}\n"                     # unformatted + clippy-red
    (rust_repo / "src" / "lib.rs").write_text(ugly)
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    Orchestrator(rust_repo, Config.load(rust_repo), Options(mode="review", fix=False)).start("review")
    assert (rust_repo / "src" / "lib.rs").read_text() == ugly
    assert "fixer" not in [c["role"] for c in read_log(rust_repo)]


def test_resume_refuses_wrong_branch(rust_repo, monkeypatch, tmp_path):
    low = tmp_path / "low.toml"
    low.write_text((rust_repo / "rustflow.toml").read_text().replace("max_total_usd = 60.0", "max_total_usd = 0.4"))
    rc, st = run(rust_repo, "happy", monkeypatch, config=low)
    assert st["status"] == "budget"
    git(rust_repo, "switch", "-q", "main")
    rc2 = Orchestrator(rust_repo, Config.load(rust_repo), Options()).resume(find_run(rust_repo, "latest"))
    assert rc2 == 2
    assert git(rust_repo, "log", "--format=%s", "-1", "main").strip() == "init"   # nothing committed on main


def test_unexpected_error_saves_state(rust_repo, monkeypatch):
    import rustflow.orchestrator as orch
    def boom(self):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(orch.Orchestrator, "phase_review", boom)
    rc, st = run(rust_repo, "happy", monkeypatch)
    assert rc == 3 and st["status"] == "crashed" and st["phase"] == "review"
    assert st["cost_usd"] > 0 and st["agents"]                 # implementer bookkeeping kept


def _tob_config(repo, tmp_path):
    cfg = tmp_path / "tob.toml"
    text = (repo / "rustflow.toml").read_text()
    text = text.replace('mode = "llm"', 'mode = "tob"').replace(
        'spec = ""                   # llm:', 'spec = "claude:opus:high"   # llm:')
    cfg.write_text(text)
    return cfg


def test_tob_audit_on_escalation(rust_repo, monkeypatch, tmp_path):
    rc, st = run(rust_repo, "escalate", monkeypatch, config=_tob_config(rust_repo, tmp_path))
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["escalate"] is True
    assert st["audit"]["report"] == ".rust-review-results/20261026T000000Z/REPORT.md"
    assert st["audit"]["files"] == ["src/lib.rs"]
    calls = [c for c in read_log(rust_repo) if c["role"] == "tob_audit"]
    assert len(calls) == 1                                     # once per run, not per round
    tools = calls[0]["argv"][calls[0]["argv"].index("--allowedTools") + 1]
    assert "Agent" in tools and "Bash(rtk rg *)" in tools
    report = (find_run(rust_repo, "latest") / "report.md").read_text(encoding="utf-8")
    assert "Trail of Bits" in report and "No exploitable issues" in report


def test_tob_audit_must_not_touch_sources(rust_repo, monkeypatch, tmp_path):
    rc, st = run(rust_repo, "tamper", monkeypatch, config=_tob_config(rust_repo, tmp_path))
    assert st["status"] == "audit_modified_tree"


def test_comment_dry_run(rust_repo, monkeypatch, capsys):
    from rustflow.cli import main
    run(rust_repo, "happy", monkeypatch)
    capsys.readouterr()
    assert main(["comment", "--repo", str(rust_repo), "--pr", "12", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("gh pr comment 12 --body-file ") and out.strip().endswith("pr-comment.md")


def test_comment_calls_gh(rust_repo, monkeypatch, tmp_path):
    import os, sys
    from rustflow.cli import main
    run(rust_repo, "happy", monkeypatch)
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin2" / "gh.py"
    gh.parent.mkdir()
    gh.write_text(f"import sys; open({str(log)!r}, 'w').write(' '.join(sys.argv[1:]))\n")
    monkeypatch.setattr("rustflow.procutil.resolve_command", lambda name: [sys.executable, str(gh)])
    assert main(["comment", "--repo", str(rust_repo)]) == 0
    assert log.read_text().startswith("pr comment --body-file ")


def test_review_context_and_report_hooks(rust_repo):
    import subprocess, sys
    (rust_repo / "src" / "lib.rs").write_text("pub unsafe fn x() {}\n")
    hooks = rust_repo / ".rustflow" / "hooks.py"
    env = {**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(rust_repo)}
    r = subprocess.run([sys.executable, str(hooks), "review-context", "main"], capture_output=True, text=True,
                       env=env, cwd=rust_repo)
    assert r.returncode == 0 and "ESCALATION_SIGNALS=1" in r.stdout and "src/lib.rs" in r.stdout
    bad = {"session_id": "s", "last_assistant_message": "## Findings\n- [P1] oops\n## Verdict: APPROVE\n"}
    outs = [subprocess.run([sys.executable, str(hooks), "check-report"], input=__import__("json").dumps(bad),
                           capture_output=True, text=True, env=env, cwd=rust_repo) for _ in range(3)]
    assert [o.returncode for o in outs] == [2, 2, 0]           # two resends, then the loop guard
    assert "APPROVE is invalid with P0/P1" in outs[0].stderr and "## Gates" in outs[0].stderr


def test_always_on_skills_reach_every_agent(rust_repo, monkeypatch):
    for base in (".claude/skills", ".agents/skills"):
        d = rust_repo / base / "rust-best-practices"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: rust-best-practices\ndescription: x\n---\nAPOLLO-MARKER\n")
    git(rust_repo, "add", "-A")
    git(rust_repo, "commit", "-qm", "apollo")
    rc, st = run(rust_repo, "happy", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert all("rust-best-practices" in a["skills"] for a in st["agents"])
    for c in read_log(rust_repo):
        if c["provider"] == "claude":
            f = c["argv"][c["argv"].index("--append-system-prompt-file") + 1]
            assert "APOLLO-MARKER" in Path(f).read_text()
        else:
            assert c["prompt_head"].startswith('<skill name="rust-best-practices"')


def test_missing_always_on_skill_stops_at_preflight(rust_repo, monkeypatch, tmp_path):
    cfg = tmp_path / "s.toml"
    cfg.write_text((rust_repo / "rustflow.toml").read_text().replace('reviewer = []               #',
                                                                     'reviewer = ["rust-best-practices"] #'))
    rc, st = run(rust_repo, "happy", monkeypatch, config=cfg)
    assert st["status"] == "error" and "rust-best-practices" in st["stop_reason"]
    assert read_log(rust_repo) == []


def test_weaken_ends_gate_changes_pending(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken", monkeypatch, difficulty="easy")
    assert st["status"] == "gate_changes_pending" and rc == 2
    e = next(x for x in st["gate_changes"] if x["path"] == ".cargo/config.toml")
    assert (e["kind"], e["proof"], e["status"]) == ("config", "failed", "blocked")
    assert e["justification"] == "the task requires this configuration" and e["fix_rounds"] >= 1


def test_weaken_revert_approved(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "weaken_revert", monkeypatch, difficulty="easy")
    assert st["status"] == "approved", st.get("stop_reason")
    assert st["gate_changes"] == []


def test_allow_unneeded_accepted_by_proof(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "allow_unneeded", monkeypatch, difficulty="easy")
    assert st["status"] == "approved_with_advisories", st.get("stop_reason")
    assert st["gate_changes"][0]["accepted_by"] == "proof"
    assert any(f["status"] == "accepted_gate_change" for f in st["findings"])


def test_digest_change_requires_new_proof(rust_repo, monkeypatch):
    rc, st = run(rust_repo, "digest_change", monkeypatch, difficulty="easy")
    assert st["status"] == "gate_changes_pending", st.get("stop_reason")
    assert st["gate_changes"][0]["proof"] == "failed"


def test_gate_changes_pending_is_continuable():
    from rustflow.orchestrator import CONTINUABLE
    assert "gate_changes_pending" in CONTINUABLE
