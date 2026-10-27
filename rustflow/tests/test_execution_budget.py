"""Budgets count durable physical attempts and wall time, including across resume."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rustflow.config import Config, ConfigError, load_defaults


def manager(tmp_path, limit=3, seconds=10, state=None, clock=None, attempts=()):
    from rustflow.budget import BudgetManager
    return BudgetManager(state if state is not None else {},
                         {'max_agent_calls': limit, 'max_active_seconds': seconds, 'max_output_bytes': 100},
                         attempts=attempts, clock=clock or (lambda: 0), history_complete=True)


def test_reservations_protect_every_initial_reviewer_from_fast_retries(tmp_path):
    from rustflow.budget import BudgetExceeded
    b = manager(tmp_path)
    tickets = b.reserve_group(['a', 'b'])
    committed = []
    b.start_attempt({'attempt_id': 'a1'}, lambda: committed.append('a1'), tickets['a'])
    b.start_attempt({'attempt_id': 'a2'}, lambda: committed.append('a2'))
    with pytest.raises(BudgetExceeded):
        b.start_attempt({'attempt_id': 'a3'}, lambda: committed.append('a3'))
    b.start_attempt({'attempt_id': 'b1'}, lambda: committed.append('b1'), tickets['b'])
    assert committed == ['a1', 'a2', 'b1']
    assert b.snapshot()['agent_calls_used'] == 3


def test_atomic_parallel_calls_never_exceed_limit(tmp_path):
    from rustflow.budget import BudgetExceeded
    b = manager(tmp_path, limit=3)
    barrier = threading.Barrier(8)
    committed = []
    def attempt(i):
        barrier.wait()
        try:
            b.start_attempt({'attempt_id': str(i)}, lambda: committed.append(i))
        except BudgetExceeded:
            pass
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(8)))
    assert len(committed) == 3 and b.snapshot()['agent_calls_remaining'] == 0


def test_group_refused_before_any_attempt_and_unused_tickets_released(tmp_path):
    from rustflow.budget import BudgetExceeded
    b = manager(tmp_path, limit=1)
    with pytest.raises(BudgetExceeded):
        b.reserve_group(['a', 'b'])
    assert b.snapshot()['agent_calls_used'] == 0
    tickets = b.reserve_group(['a'])
    b.release_group(tickets.values())
    b.start_attempt({'attempt_id': 'c'}, lambda: None)
    assert b.snapshot()['agent_calls_used'] == 1


def test_resume_counts_unique_started_records_and_only_active_time(tmp_path):
    now = [100.0]
    state = {}
    b = manager(tmp_path, state=state, clock=lambda: now[0])
    b.start_attempt({'attempt_id': 'one'}, lambda: None)
    now[0] += 4
    b.close()
    now[0] += 10000  # offline time is excluded
    b2 = manager(tmp_path, state=state, clock=lambda: now[0], attempts=[{'attempt_id': 'one'}]*2)
    assert b2.snapshot()['agent_calls_used'] == 1
    assert b2.snapshot()['active_seconds_used'] == 4
    now[0] += 3
    assert b2.snapshot()['active_seconds_used'] == 7
    assert b2.deadline == 10110


def test_crash_charges_one_heartbeat_and_preserves_estimate(tmp_path):
    state = {}
    now = [0.0]
    b = manager(tmp_path, state=state, clock=lambda: now[0])
    now[0] = 3
    b.checkpoint()  # last persisted heartbeat
    now[0] = 1000
    b2 = manager(tmp_path, state=state, clock=lambda: now[0])
    assert b2.snapshot()['active_seconds_used'] == 4
    assert b2.snapshot()['crash_estimate_seconds'] == 1


def test_deadline_wait_and_limit_changes(tmp_path):
    from rustflow.budget import BudgetExceeded
    now = [0.0]
    b = manager(tmp_path, clock=lambda: now[0])
    now[0] = 10
    with pytest.raises(BudgetExceeded):
        b.check()
    with pytest.raises(ValueError):
        b.raise_limits({'max_agent_calls': 2})
    b.raise_limits({'max_active_seconds': 20})
    assert b.deadline == 20
    b.check()


@pytest.mark.parametrize('key,value', [('max_agent_calls', 0), ('max_active_seconds', -1),
                                       ('max_output_bytes', 0), ('max_agent_calls', True)])
def test_invalid_limits_are_rejected(key, value):
    data = load_defaults()
    data.setdefault('process' if key == 'max_output_bytes' else 'budget', {})[key] = value
    with pytest.raises(ConfigError):
        Config(data)


def test_retry_wait_obeys_deadline_and_cancellation(tmp_path):
    import time
    from rustflow.budget import BudgetManager, BudgetExceeded
    b = BudgetManager({}, {'max_agent_calls': 2, 'max_active_seconds': .05, 'max_output_bytes': 100})
    start = time.monotonic()
    with pytest.raises(BudgetExceeded) as caught:
        b.wait(5)
    assert caught.value.reason == 'active_time_limit' and time.monotonic() - start < 1
    b2 = manager(tmp_path)
    b2.cancel.set()
    with pytest.raises(BudgetExceeded) as caught:
        b2.wait(5)
    assert caught.value.reason == 'cancelled'
