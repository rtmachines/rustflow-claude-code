"""An approval from one reviewer when two were planned must be visible to the driving agent: summary,
brief, report and next_actions - not only a log line."""

import json

from conftest import skip_agent_sleep
from rustflow import agents, runs
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _run(repo, scenario, monkeypatch, config=None, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    skip_agent_sleep(monkeypatch)
    rc = Orchestrator(repo, Config.load(repo, config), Options(**opts)).start(TASK)
    return rc, _state(repo)


def _state(repo):
    return json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


def test_approval_by_one_of_two_reviewers_is_flagged(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "codex_down", monkeypatch)
    assert st["status"].startswith("approved") and rc == 0          # status and exit code unchanged
    assert st["degraded_review"]["failed"] == ["reviewer_b"]
    s = runs.summary(st)
    assert s["degraded"] == ["reviewer_b"]
    brief = runs.brief_markdown(s)
    assert "DEGRADED" in brief and "reviewer_b" in brief
    ask = next(a for a in s["next_actions"] if a["action"] == "ask_user" and "reviewer_b" in a["question"])
    assert ask["command"] == f"rustflow resume {st['run_id']} --extra-rounds 1 --detach"
    report = (find_run(rust_repo, "latest") / "report.md").read_text(encoding="utf-8")
    assert "reviewer_b" in report and "jeden recenzent" in report.lower()


def test_degraded_run_can_be_reviewed_again(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "codex_down", monkeypatch)
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    rc2 = Orchestrator(rust_repo, Config.load(rust_repo), Options()).resume(find_run(rust_repo, "latest"),
                                                                           extra_rounds=1)
    st2 = _state(rust_repo)
    assert rc2 == 0 and st2["status"].startswith("approved"), st2.get("stop_reason")
    assert "degraded_review" not in st2
    assert set(st2["rounds"][-1]["verdicts"]) == {"reviewer_a", "reviewer_b"}


def test_planned_single_reviewer_rereview_is_not_degraded(rust_repo, monkeypatch, tmp_path):
    cfg = tmp_path / "rf.toml"
    cfg.write_text((rust_repo / "rustflow.toml").read_text().replace('rereview = "both"', 'rereview = "first"'))
    rc, st = _run(rust_repo, "happy", monkeypatch, config=cfg)
    assert st["status"].startswith("approved") and len(st["rounds"]) == 2
    assert list(st["rounds"][1]["verdicts"]) == ["reviewer_a"]          # by configuration, not a failure
    assert "degraded_review" not in st and runs.summary(st)["degraded"] == []


def test_failure_in_an_earlier_round_only_is_not_degraded():
    st = {"run_id": "r", "status": "approved", "pid": 0, "findings": [], "rounds": [
        {"round": 1, "failed_reviewers": ["reviewer_b"]}, {"round": 2, "failed_reviewers": []}]}
    assert runs.summary(st)["degraded"] == []
