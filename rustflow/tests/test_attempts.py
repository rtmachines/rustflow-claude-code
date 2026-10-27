"""The real adapters persist each CLI invocation before merging logical results."""

import json
from pathlib import Path

import pytest

from rustflow import agents, procutil, schemas
from rustflow.config import AgentSpec, Config, load_defaults

FIXTURES = Path(__file__).parent / "fixtures"


def call_and_runner(tmp_path, monkeypatch, provider="claude", **kwargs):
    runner = (agents.ClaudeRunner if provider == "claude" else agents.CodexRunner)(Config(load_defaults()))
    monkeypatch.setattr(runner, "binary", lambda: ["fake-provider"])
    monkeypatch.setattr(agents.time, "sleep", lambda _: None)
    spec = "claude:opus:high" if provider == "claude" else "codex:gpt-6.1-sol:high"
    call = agents.AgentCall("reviewer_a", "review", AgentSpec.parse(spec), "p", False,
                            kwargs.pop("schema", None), tmp_path, tmp_path / "agents", **kwargs)
    return call, runner


def records(tmp_path):
    return sorted([json.loads(p.read_text()) for p in (tmp_path / "attempts").glob("*.json")],
                  key=lambda a: (a["logical_call_id"], a["attempt_number"]))


def responses(monkeypatch, payloads, *, check_started=None):
    pending = list(payloads)
    def run(argv, **kwargs):
        if check_started:
            check_started()
        payload = pending.pop(0)
        return procutil.ProcResult(argv, int(payload.get("is_error", False)), json.dumps(payload), "", 2.0)
    monkeypatch.setattr(procutil, "run", run)


def test_retry_accounts_for_both_attempts_before_merging(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch)
    payloads = json.loads((FIXTURES / "claude_usage.json").read_text())
    responses(monkeypatch, payloads)
    result = agents.run_with_retry(runner, call)
    assert result.ok and result.text == "done"
    assert result.cost_usd == 0.5  # 0.125 + 0.375, including failed work
    assert result.tokens["input_tokens"] == 30 and result.duration_s == 4.0
    rows = records(tmp_path)
    assert len(rows) == 2
    assert [a["kind"] for a in rows] == ["initial", "retry"]
    assert [a["status"] for a in rows] == ["failed", "succeeded"]
    assert [a["measured_cost_usd"] for a in rows] == [0.125, 0.375]
    assert [a["tokens"]["total_tokens"] for a in rows] == [20, 30]
    assert result.attempt_ids == [a["attempt_id"] for a in rows]
    assert len({a["logical_call_id"] for a in rows}) == 1
    assert rows[0]["raw_usage"] == payloads[0]["usage"]
    for row in rows:
        for path in row["artifacts"].values():
            assert (tmp_path / path).is_file()


@pytest.mark.parametrize("repair_ok", [True, False])
def test_json_repair_is_two_attempts_with_no_extra_retry(tmp_path, monkeypatch, repair_ok):
    call, runner = call_and_runner(tmp_path, monkeypatch, schema=schemas.REVIEW_SCHEMA)
    payloads = json.loads((FIXTURES / "claude_usage.json").read_text())
    payloads[0].update(subtype="error_max_structured_output_retries", errors=["structured output missing"])
    if repair_ok:
        payloads[1]["structured_output"] = {"summary": "done", "verdict": "APPROVE", "findings": [],
                                              "disputes": [], "acceptance": [], "residual_risks": []}
    responses(monkeypatch, payloads)
    result = agents.run_with_retry(runner, call, retries=3)
    rows = records(tmp_path)
    assert len(rows) == 2 and [a["kind"] for a in rows] == ["initial", "json_repair"]
    assert result.ok is repair_ok and result.json_repair
    assert result.cost_usd == 0.5 and result.duration_s == 4.0
    assert result.tokens["input_tokens"] == 30
    assert len(result.attempt_ids) == 2


def test_attempt_is_durable_before_process_and_exception_keeps_unknown_usage(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch)
    def interrupted(*args, **kwargs):
        row, = records(tmp_path)
        assert row["status"] == "started" and row["started_at"]
        raise KeyboardInterrupt
    monkeypatch.setattr(procutil, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run(call)
    row, = records(tmp_path)
    assert row["status"] == "interrupted" and row["measured_cost_usd"] is None
    assert row["output_complete"] is False and row["termination_reason"] == "interrupted"
    assert row["measurement_status"] == "unknown" and row["duration_s"] >= 0


def test_timeout_has_estimate_without_a_measurement(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch, max_budget_usd=5)
    monkeypatch.setattr(procutil, "run", lambda argv, **kw: procutil.ProcResult(argv, -1, "", "", 2, True))
    result = agents.run_with_retry(runner, call)
    assert result.cost_usd == 5 and result.cost_estimated
    row, = records(tmp_path)
    assert row["status"] == "timed_out"
    assert row["measured_cost_usd"] is None and row["estimated_cost_usd"] == 5
    assert row["measurement_status"] == "unknown"


def test_codex_records_raw_events_and_does_not_double_count_cache(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch, provider="codex")
    def run(argv, **kw):
        Path(argv[argv.index("-o") + 1]).write_text("done")
        return procutil.ProcResult(argv, 0, (FIXTURES / "codex_usage.jsonl").read_text(), "", 3)
    monkeypatch.setattr(procutil, "run", run)
    result = runner.run(call)
    row, = records(tmp_path)
    assert result.ok and result.tokens["input_tokens"] == 140
    assert row["tokens"]["total_tokens"] == 170 and row["measured_cost_usd"] is None
    assert len(row["raw_usage"]) == 2


def test_permanent_error_is_one_attempt(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch)
    payload = json.loads((FIXTURES / "claude_usage.json").read_text())[0]
    payload.update(errors=["unauthorized"], api_error_status=401)
    responses(monkeypatch, [payload])
    result = agents.run_with_retry(runner, call, retries=3)
    assert not result.ok and not result.retryable
    row, = records(tmp_path)
    assert row["measured_cost_usd"] == 0.125


def test_retry_followed_by_json_repair_merges_three_attempts_once(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch, schema=schemas.REVIEW_SCHEMA)
    first, second = json.loads((FIXTURES / "claude_usage.json").read_text())
    responses(monkeypatch, [first, second, {**second, "structured_output": {
        "summary": "done", "verdict": "APPROVE", "findings": [], "disputes": [],
        "acceptance": [], "residual_risks": []}}])
    result = agents.run_with_retry(runner, call)
    assert result.ok and result.cost_usd == 0.875 and result.duration_s == 6
    assert result.tokens["input_tokens"] == 50 and len(result.attempt_ids) == 3
    rows = records(tmp_path)
    assert [r["kind"] for r in rows] == ["initial", "retry", "json_repair"]


def test_same_logical_name_preserves_attempt_artifacts_and_latest_paths(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch)
    first, second = json.loads((FIXTURES / "claude_usage.json").read_text())
    responses(monkeypatch, [first, second])
    runner.run(call)
    row, = records(tmp_path)
    original = {path: (tmp_path / path).read_bytes() for path in row["artifacts"].values()}
    new_call, _ = call_and_runner(tmp_path, monkeypatch)
    runner.run(new_call)
    assert (tmp_path / "agents" / "review.prompt.md").read_text() == "p"
    assert json.loads((tmp_path / "agents" / "review.result.json").read_text())["ok"] is True
    for path, content in original.items():
        assert (tmp_path / path).read_bytes() == content


@pytest.mark.parametrize("value", [None, "0.1", -1, float("nan"), float("inf"), True])
def test_invalid_cost_does_not_crash_adapter_or_pollute_json(tmp_path, monkeypatch, value):
    call, runner = call_and_runner(tmp_path, monkeypatch)
    responses(monkeypatch, [{"subtype": "success", "total_cost_usd": value,
                            "usage": {"input_tokens": float("nan"), "output_tokens": True}}])
    assert runner.run(call).ok
    row, = records(tmp_path)
    assert row["measured_cost_usd"] is None and row["measurement_status"] == "unknown"
    for path in (tmp_path / "attempts").glob("*.json"):
        json.loads(path.read_text(), parse_constant=lambda x: pytest.fail(f"nonstandard JSON: {x}"))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_missing_binary_creates_no_provider_attempt(tmp_path, monkeypatch, provider):
    call, runner = call_and_runner(tmp_path, monkeypatch, provider=provider)
    monkeypatch.setattr(runner, "binary", lambda: procutil.resolve_command(str(tmp_path / "missing-provider")))
    with pytest.raises(procutil.CommandNotFound):
        runner.run(call)
    assert records(tmp_path) == [] and call.attempt_ids == []


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_truncated_valid_json_never_succeeds_or_retries(tmp_path, monkeypatch, provider):
    call, runner = call_and_runner(tmp_path, monkeypatch, provider=provider, schema=schemas.REVIEW_SCHEMA)
    payload = {'type': 'result', 'subtype': 'success', 'session_id': 's',
               'structured_output': {'summary': 'done', 'verdict': 'APPROVE', 'findings': [],
                                     'disputes': [], 'acceptance': [], 'residual_risks': []}}
    def run(argv, **kw):
        if provider == 'codex':
            Path(argv[argv.index('-o')+1]).write_text(json.dumps(payload['structured_output']))
        return procutil.ProcResult(argv, 0, json.dumps(payload), '', 1,
                                   termination_reason='output_limit', output_complete=False,
                                   stdout_bytes=1024, max_output_bytes=1024)
    monkeypatch.setattr(procutil, 'run', run)
    result = agents.run_with_retry(runner, call, retries=3)
    assert not result.ok and not result.retryable and result.data is None
    assert result.termination_reason == 'output_limit' and not result.output_complete
    row, = records(tmp_path)
    assert row['termination_reason'] == 'output_limit' and row['output_complete'] is False
    assert row['measurement_status'] == 'unknown'


def test_codex_last_message_read_is_bounded(tmp_path, monkeypatch):
    call, runner = call_and_runner(tmp_path, monkeypatch, provider='codex')
    runner.cfg.data['process']['max_output_bytes'] = 100
    def run(argv, **kwargs):
        Path(argv[argv.index('-o')+1]).write_bytes(b'x'*101)
        return procutil.ProcResult(argv, 0, '', '', 0, max_output_bytes=100)
    monkeypatch.setattr(procutil, 'run', run)
    result = agents.run_with_retry(runner, call, retries=3)
    assert not result.ok and result.termination_reason == 'output_limit'
    assert not result.output_complete and len(records(tmp_path)) == 1
