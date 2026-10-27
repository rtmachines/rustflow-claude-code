"""Regression coverage for interruption and admission boundaries discovered in final review."""
import io
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rustflow import agents, gates, runs, procutil, gitutil
from rustflow.budget import BudgetManager, BudgetExceeded
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, Abort


def test_closing_budget_interrupts_retry_and_rejects_late_admission(tmp_path):
    budget = BudgetManager({}, {'max_agent_calls': 3, 'max_active_seconds': 30, 'max_output_bytes': 100})
    entered = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    def retry_wait():
        entered.set()
        budget.wait(5)
        budget.start_attempt({'attempt_id': 'late'}, lambda: None)
    future = pool.submit(retry_wait)
    assert entered.wait(1)
    try:
        budget.close()
        with pytest.raises(BudgetExceeded):
            future.result(timeout=1)
        budget.cancel.clear()  # closed is an independent admission guard
        with pytest.raises(BudgetExceeded):
            budget.start_attempt({'attempt_id': 'later'}, lambda: None)
    finally:
        budget.cancel.set()
        pool.shutdown(wait=True)


def test_parallel_interrupt_cancels_waiting_reviewer_before_run_exit(rust_repo, monkeypatch):
    (rust_repo / 'src/lib.rs').write_text('pub fn value() -> u8 { 1 }\n')
    o = Orchestrator(rust_repo, Config.load(rust_repo), Options(mode='review'), out=io.StringIO())
    entered, cancelled = threading.Event(), threading.Event()
    late = []
    def call(role, *a, **kw):
        if role == 'reviewer_a':
            entered.set()
            try:
                o.budget.wait(5)
                late.append(role)
            except BudgetExceeded:
                cancelled.set()
                raise
        else:
            assert entered.wait(2)
            raise KeyboardInterrupt
    monkeypatch.setattr(o, 'call_agent', call)
    try:
        assert o.start('review change') == 130
        assert cancelled.wait(1)
        assert not late
        assert runs.load_state(o.run_dir)['status'] == 'interrupted'
    finally:
        o.budget.cancel.set()


@pytest.mark.parametrize('where', ['checks', 'snapshot', 'isolation', 'lockfile'])
@pytest.mark.parametrize('isolation', ['branch', 'worktree'])
def test_preflight_stop_cleans_isolation_and_can_resume(rust_repo, monkeypatch, where, isolation):
    o = Orchestrator(rust_repo, Config.load(rust_repo), Options(isolation=isolation), out=io.StringIO())
    def fail(*a, **kw):
        if where == 'lockfile':
            subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=o.workdir,
                           check=True, capture_output=True)
            raise BudgetExceeded('active_time_limit')
        if where == 'checks':
            raise BudgetExceeded('active_time_limit')
        raise gates.IncompleteGate('snapshot', procutil.ProcResult(
            ['git'], 124, '', '', 0, termination_reason='active_time_limit', output_complete=False))
    with monkeypatch.context() as patch:
        if where == 'isolation':
            method = 'create_branch' if isolation == 'branch' else 'add_worktree'
            original_create = getattr(gitutil, method)
            def interrupted_creation(*a, **kw):
                original_create(*a, **kw)
                fail()
            patch.setattr(gitutil, method, interrupted_creation)
        patch.setattr(o, '_preflight_checks', fail if where in ('checks', 'lockfile') else lambda _: None)
        if where == 'snapshot':
            patch.setattr(o, 'snapshot', fail)
        assert o.start('task') == 2
    s = runs.load_state(o.run_dir)
    assert s['status'] == 'budget' and s['phase'] == 'preflight'
    # Rerunning preflight must not fail because its branch/worktree already exists.
    monkeypatch.setattr(o, '_preflight_checks', lambda _: None)
    def stop_at_triage():
        raise Abort('agent_failed', 'test boundary reached')
    monkeypatch.setattr(o, 'phase_triage', stop_at_triage)
    assert o.resume(o.run_dir) == 2
    assert runs.load_state(o.run_dir)['phase'] == 'triage'


def test_changed_generated_lockfile_remains_protected_on_resume(rust_repo, monkeypatch):
    o = Orchestrator(rust_repo, Config.load(rust_repo), Options(), out=io.StringIO())
    def stopped_baseline(*a):
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=o.workdir,
                       check=True, capture_output=True)
        raise BudgetExceeded('active_time_limit')
    monkeypatch.setattr(o, '_preflight_checks', stopped_baseline)
    assert o.start('task') == 2
    lock = rust_repo / 'Cargo.lock'
    lock.write_text(lock.read_text() + '\n# user edit\n')
    expected = lock.read_bytes()
    assert o.resume(o.run_dir) == 2
    assert o.state['status'] == 'error' and 'dirty' in o.state['stop_reason']
    assert lock.read_bytes() == expected


def test_denied_gate_fix_does_not_spend_repair_allowance(rust_repo, monkeypatch):
    o = Orchestrator(rust_repo, Config.load(rust_repo), Options(), out=io.StringIO())
    monkeypatch.setattr(o, 'loop', lambda: 0)
    o.start('task')
    o.state.update(phase='gates', difficulty='easy', gate_fix_attempts=1, gates_after='implement')
    o.cfg.data['loop']['max_gate_fix_attempts'] = 2
    result = gates.GateResult('full', False, 'tree', steps=[gates.Step('tests', False)])
    monkeypatch.setattr(o, 'run_gates', lambda *a, **kw: result)
    monkeypatch.setattr(o, 'snapshot', lambda: 'same-tree')
    def denied(*a, **kw):
        raise Abort('budget', 'agent_call_limit')
    monkeypatch.setattr(o, 'call_agent', denied)
    with pytest.raises(Abort):
        o.phase_gates()
    assert o.state['gate_fix_attempts'] == 1
    reached = []
    def invoked(*a, **kw):
        reached.append(True)
        return agents.AgentResult('fix', 'fixer', 's', ok=True)
    monkeypatch.setattr(o, 'call_agent', invoked)
    o.phase_gates()
    assert reached == [True]
