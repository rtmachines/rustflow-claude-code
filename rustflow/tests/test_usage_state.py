"""Run accounting survives concurrent completions, cancellation and old histories."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import threading

import pytest

from rustflow import agents, procutil, report, runs, triage
from rustflow.config import AgentSpec, Config, load_defaults
from rustflow.orchestrator import Options, Orchestrator

SPEC = AgentSpec.parse("claude:opus:high")
FIXTURES = Path(__file__).parent / "fixtures"


def orchestrator(tmp_path, monkeypatch, *, dry_run=False):
    o = Orchestrator(tmp_path, Config(load_defaults()), Options(dry_run=dry_run), out=io.StringIO())
    monkeypatch.setattr(o, "loop", lambda: 0)
    assert o.start("usage test", run_id="usage-test") == 0
    for runner in o.runners.values():
        monkeypatch.setattr(runner, "binary", lambda: ["fake-provider"])
    monkeypatch.setattr(agents.time, "sleep", lambda _: None)
    return o


def invoke(o, name="review"):
    return o.call_agent("reviewer_a", name, SPEC, "p", write=False, schema=None, skill=None)


def feed(monkeypatch, payloads):
    pending = iter(payloads)
    def run(argv, **kwargs):
        payload = next(pending)
        return procutil.ProcResult(argv, int(payload.get("is_error", False)), json.dumps(payload), "", 2)
    monkeypatch.setattr(procutil, "run", run)


def test_logical_result_and_state_include_each_retry_once(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    feed(monkeypatch, json.loads((FIXTURES / "claude_usage.json").read_text()))
    result = invoke(o)
    state = runs.load_state(o.run_dir)
    assert state["cost_usd"] == 0.5 and state["tokens"]["input_tokens"] == 30
    assert len(state["agents"]) == 1
    assert state["agents"][0]["attempt_ids"] == result.attempt_ids
    assert state["usage_summary"]["cost_usd"]["total"] == 0.5
    assert state["usage_summary"]["providers"]["claude"]["tokens"]["total_tokens"]["total"] == 50
    assert "raw_usage" not in state["agents"][0]


def test_concurrent_reviewers_persist_every_cost_without_serializing_processes(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    barrier = threading.Barrier(8)
    payload = json.loads((FIXTURES / "claude_usage.json").read_text())[0]
    payload.update(is_error=False, subtype="success")
    def run(argv, **kwargs):
        barrier.wait(timeout=10)
        return procutil.ProcResult(argv, 0, json.dumps(payload), "", 2)
    monkeypatch.setattr(procutil, "run", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: invoke(o, f"review-{n}"), range(8)))
    state = runs.load_state(o.run_dir)
    assert state["cost_usd"] == 1 and state["tokens"]["input_tokens"] == 80
    assert state["usage_summary"]["attempt_count"] == len(state["agents"]) == 8
    assert len({r.attempt_ids[0] for r in results}) == 8
    assert state["usage_summary"]["cost_usd"]["total"] == 1


def test_resume_reconciles_crash_window_and_interrupts_unfinished_records(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    feed(monkeypatch, json.loads((FIXTURES / "claude_usage.json").read_text()))
    invoke(o)
    paths = sorted((o.run_dir / "attempts").glob("*.json"))
    assert len(paths) == 2
    originals = {p.name: p.read_bytes() for p in paths}
    abandoned = json.loads(paths[0].read_text())
    abandoned.update(attempt_id="abandoned", status="started", measured_cost_usd=None)
    (o.run_dir / "attempts" / "abandoned.json").write_text(json.dumps(abandoned))
    # The process died after persisting its result, before updating the state snapshot.
    o.state.update(cost_usd=0, tokens={}, status="interrupted")
    o.save()
    assert o.resume(o.run_dir) == 0
    assert o.state["cost_usd"] == 0.5
    recovered = json.loads((o.run_dir / "attempts" / "abandoned.json").read_text())
    assert recovered["status"] == "interrupted" and recovered["measurement_status"] == "unknown"
    assert recovered["tokens"]["total_tokens"] is None
    feed(monkeypatch, json.loads((FIXTURES / "claude_usage.json").read_text())[1:])
    invoke(o)  # same logical name after resume, new identity and artifact names
    assert o.state["cost_usd"] == 0.875 and o.state["tokens"]["input_tokens"] == 50
    assert o.state["usage_summary"]["attempt_count"] == 4
    for name, data in originals.items():
        assert (o.run_dir / "attempts" / name).read_bytes() == data
    assert len({a["logical_call_id"] for a in o.state["agents"]}) == 2
    o.state["status"] = "interrupted"
    o.save()
    assert o.resume(o.run_dir) == 0
    assert o.state["cost_usd"] == 0.875  # reconciliation is idempotent


def test_legacy_resume_keeps_a_baseline_without_fabricating_attempts(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    o.state.pop("usage_summary", None)
    o.state.pop("usage_baseline", None)
    o.state.update(status="interrupted", cost_usd=2, tokens={"input_tokens": 99},
                   agents=[{"name": "old", "spec": str(SPEC), "cost_usd": 2}])
    o.save()
    before = (o.run_dir / "state.json").read_bytes()
    summary = runs.summary(runs.load_state(o.run_dir))
    assert summary["usage_summary"]["history_complete"] is False
    assert summary["usage_summary"]["attempt_count"] == 0
    assert summary["usage_summary"]["cost_usd"]["total"] is None
    assert (o.run_dir / "state.json").read_bytes() == before
    assert o.resume(o.run_dir) == 0
    feed(monkeypatch, json.loads((FIXTURES / "claude_usage.json").read_text()))
    invoke(o)
    assert o.state["cost_usd"] == 2.5 and o.state["tokens"]["input_tokens"] == 129
    s = o.state["usage_summary"]
    assert s["history_complete"] is False and s["attempt_count"] == 2
    assert s["cost_usd"]["total"] is None and s["cost_usd"]["known"] == 0.5
    o.state["status"] = "interrupted"
    o.save()
    assert o.resume(o.run_dir) == 0 and o.state["cost_usd"] == 2.5


def test_dry_run_has_no_provider_attempts(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch, dry_run=True)
    invoke(o)
    assert not list((o.run_dir / "attempts").glob("*.json"))
    assert o.state["usage_summary"]["attempt_count"] == 0
    assert o.state["cost_usd"] == 0 and o.state["tokens"] == {}


def test_full_report_lists_attempts_but_brief_and_mcp_only_aggregate(tmp_path, monkeypatch):
    from rustflow import mcp_server
    o = orchestrator(tmp_path, monkeypatch)
    feed(monkeypatch, json.loads((FIXTURES / "claude_usage.json").read_text()))
    result = invoke(o)
    text = report.render(o.state, tmp_path)
    assert all(attempt_id in text for attempt_id in result.attempt_ids)
    assert "0.125" in text and "0.375" in text and "retry" in text
    summary = runs.summary(o.state)
    assert summary["usage_summary"]["attempt_count"] == 2
    assert all(attempt_id not in runs.brief_markdown(summary) for attempt_id in result.attempt_ids)
    monkeypatch.setattr(mcp_server, "_repo", lambda: tmp_path)
    _, data = mcp_server.t_status({"run": "usage-test"})
    assert data["usage_summary"] == summary["usage_summary"]
    assert "attempts" not in data and "agents" not in data


def test_estimator_excludes_unknown_and_timeout_samples_but_retains_zero(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    payload = json.loads((FIXTURES / "claude_usage.json").read_text())[1]
    feed(monkeypatch, [payload, {**payload, "total_cost_usd": None}, {**payload, "total_cost_usd": 0}])
    invoke(o, "paid")
    invoke(o, "missing")
    invoke(o, "free")
    monkeypatch.setattr(procutil, "run", lambda argv, **kw: procutil.ProcResult(argv, -1, "", "", 2, True))
    invoke(o, "timeout")
    o.state.update(status="approved", difficulty="medium")
    o.save()
    est = triage.estimate_cost(tmp_path, {"reviewer_a": str(SPEC)}, "medium")
    assert est["per_call_usd"]["reviewer_a"] == pytest.approx(0.1875)  # (0.375 + 0) / 2
    assert est["same_difficulty"]["n"] == 0  # this run has incomplete measured Claude cost
    assert est["excluded_incomplete_runs"] == 1
    assert est["legacy_runs"] == 0


def test_legacy_estimator_samples_survive_resume_and_remain_labelled(tmp_path, monkeypatch):
    o = orchestrator(tmp_path, monkeypatch)
    o.state.pop("usage_summary")
    o.state.pop("usage_baseline")
    o.state.update(status="interrupted", cost_usd=2, tokens={}, difficulty="medium",
                   agents=[{"name": "old", "spec": str(SPEC), "cost_usd": 2}])
    o.save()
    before = triage.estimate_cost(tmp_path, {"reviewer_a": str(SPEC)}, "medium")
    assert before["per_call_usd"]["reviewer_a"] == 2
    assert o.resume(o.run_dir) == 0
    o.state["status"] = "interrupted"
    o.save()
    after = triage.estimate_cost(tmp_path, {"reviewer_a": str(SPEC)}, "medium")
    assert after["per_call_usd"]["reviewer_a"] == 2
    assert after["legacy_runs"] == 1
    assert after["same_difficulty"] == before["same_difficulty"]
    payload = json.loads((FIXTURES / "claude_usage.json").read_text())[1]
    feed(monkeypatch, [payload, {**payload, "total_cost_usd": None}])
    invoke(o, "paid")
    invoke(o, "unknown")
    mixed = triage.estimate_cost(tmp_path, {"reviewer_a": str(SPEC)}, "medium")
    assert mixed["per_call_usd"]["reviewer_a"] == 1.1875  # (legacy 2 + measured 0.375) / 2
    assert mixed["legacy_runs"] == 1 and "completeness unknown" in mixed["text"]
    assert mixed["same_difficulty"]["n"] == 0  # new missing costs cannot enter a legacy total
