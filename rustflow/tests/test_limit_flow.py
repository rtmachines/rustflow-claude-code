"""Stops retain their phase and cannot reach fixer, degraded approval or baseline acceptance."""
import json

from rustflow import procutil, gates, runs
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator
from conftest import read_log


def state(repo):
    return runs.load_state(runs.find_run(repo, 'latest'))


def test_incomplete_reviewer_blocks_other_reviewer_approval(rust_repo, monkeypatch):
    original = procutil.run
    def run(argv, **kwargs):
        if kwargs.get('env', {}).get('RUSTFLOW_ROLE') == 'reviewer_b':
            return procutil.ProcResult(argv, 0, '{}', '', 0,
                                      termination_reason='output_limit', output_complete=False)
        return original(argv, **kwargs)
    monkeypatch.setattr(procutil, 'run', run)
    rc = Orchestrator(rust_repo, Config.load(rust_repo), Options()).start('Add parse_kv')
    s = state(rust_repo)
    assert rc == 2 and s['status'] == 'incomplete' and s['phase'] == 'review'
    assert s['termination_reason'] == 'output_limit' and not s['output_complete']
    assert not s.get('degraded_review') and not s['rounds']
    assert not any(r['role'] == 'fixer' for r in read_log(rust_repo))


def test_incomplete_baseline_cannot_be_accepted(rust_repo, monkeypatch):
    monkeypatch.setattr(gates, 'run_gates', lambda *a, **kw: gates.GateResult(
        'full', False, None, steps=[gates.Step('tests', False, termination_reason='output_limit', output_complete=False)]))
    rc = Orchestrator(rust_repo, Config.load(rust_repo), Options(accept_red_baseline=True)).start('task')
    s = state(rust_repo)
    assert rc == 2 and s['status'] == 'incomplete' and s['phase'] == 'preflight'
    assert not s.get('baseline_signatures') and not read_log(rust_repo)


def test_entire_review_group_must_fit_before_first_provider_call(rust_repo):
    with (rust_repo / 'src/lib.rs').open('a') as fh:
        fh.write('\npub fn value() -> u8 { 1 }\n')
    rc = Orchestrator(rust_repo, Config.load(rust_repo),
                      Options(mode='review', max_agent_calls=1)).start('review change')
    s = state(rust_repo)
    assert rc == 2 and s['status'] == 'budget' and s['phase'] == 'review'
    assert s['termination_reason'] == 'agent_call_limit'
    assert s['execution_budget']['agent_calls_used'] == 0 and not read_log(rust_repo)


def test_active_time_stops_gates_without_fixer_and_resumes_with_larger_limit(rust_repo, monkeypatch):
    original = gates.GateRunner._exec
    def slow(self, name, argv, timeout=3600):
        import sys
        return original(self, name, [sys.executable, '-c', 'import time; time.sleep(2)'], timeout)
    monkeypatch.setattr(gates.GateRunner, '_exec', slow)
    o = Orchestrator(rust_repo, Config.load(rust_repo), Options(max_active_seconds=.1))
    assert o.start('task') == 2
    s = state(rust_repo)
    assert s['status'] == 'budget' and s['termination_reason'] == 'active_time_limit'
    assert s['execution_budget']['active_seconds_used'] < 2
    assert not s['execution_budget']['session_open'] and not read_log(rust_repo)
    monkeypatch.setattr(gates.GateRunner, '_exec', original)
    monkeypatch.setenv('FAKE_SCENARIO', 'happy')
    resumed = Orchestrator(rust_repo, Config.load(rust_repo), Options(max_active_seconds=120))
    assert resumed.resume(o.run_dir) == 0
    assert state(rust_repo)['status'].startswith('approved')
