"""One run's durable attempt allowance and monotonic wall-clock deadline.

Reservations protect initial parallel calls; only durable ledger records spend slots.
The orchestrator supplies its state lock and persistence callback, so ledger admission
and state updates share the same critical section as the heartbeat.
"""
from __future__ import annotations

import math
import threading
import time
from uuid import uuid4

DEFAULT_LIMITS = {'max_agent_calls': 40, 'max_active_seconds': 7200, 'max_output_bytes': 16*1024*1024}
HEARTBEAT_SECONDS = 1


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def validate_limits(limits):
    for key, value in limits.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value <= 0 or (key != 'max_active_seconds' and not isinstance(value, int))):
            raise ValueError(f'{key} must be positive' + (' integer' if key != 'max_active_seconds' else ''))


class BudgetManager:
    def __init__(self, state, limits, *, attempts=(), history_complete=False,
                 lock=None, persist=None, event=None, clock=time.monotonic):
        validate_limits(limits)
        self.state = state
        self.lock = lock or threading.RLock()
        self.persist = persist or (lambda: None)
        self.event = event or (lambda *a, **kw: None)
        self.clock = clock
        self.cancel = threading.Event()
        self.ids = {a['attempt_id'] for a in attempts}
        self.reserved = set()
        self.data = state.setdefault('execution_budget', {
            'limits': dict(limits), 'active_seconds_used': 0.0, 'crash_estimate_seconds': 0.0,
            'history_complete': history_complete,
        })
        self.limits = self.data['limits']
        validate_limits(self.limits)
        if self.data.get('session_open'):
            self.data['active_seconds_used'] += HEARTBEAT_SECONDS
            self.data['crash_estimate_seconds'] = self.data.get('crash_estimate_seconds', 0) + HEARTBEAT_SECONDS
            self.event('active_time_estimated', seconds=HEARTBEAT_SECONDS)
        self.base_seconds = self.data['active_seconds_used']
        self.started = clock()
        self.closed = False
        self.data['session_open'] = True
        self.checkpoint()

    @property
    def deadline(self):
        return self.started + self.limits['max_active_seconds'] - self.base_seconds

    def checkpoint(self):
        with self.lock:
            if not self.closed:
                self.data['active_seconds_used'] = self.base_seconds + max(0, self.clock()-self.started)
            self.data['agent_calls_used'] = len(self.ids)
            self.data['agent_calls_remaining'] = max(0, self.limits['max_agent_calls']-len(self.ids))
            self.data['active_seconds_remaining'] = max(0, self.limits['max_active_seconds']-self.data['active_seconds_used'])
            return self.data

    def snapshot(self):
        with self.lock:
            return {**self.checkpoint(), 'limits': dict(self.limits)}

    def _deny(self, reason):
        self.event('budget_denied', reason=reason, budget=self.snapshot())
        self.persist()
        raise BudgetExceeded(reason)

    def check(self):
        with self.lock:
            if self.closed or self.cancel.is_set():
                self._deny('cancelled')
            if self.clock() >= self.deadline:
                self._deny('active_time_limit')

    def reserve_group(self, names):
        with self.lock:
            self.check()
            if len(self.ids) + len(self.reserved) + len(names) > self.limits['max_agent_calls']:
                self._deny('agent_call_limit')
            tickets = {name: uuid4().hex for name in names}
            self.reserved.update(tickets.values())
            return tickets

    def release_group(self, tickets):
        with self.lock:
            self.reserved.difference_update(tickets)

    def start_attempt(self, record, persist_started, reservation=None):
        with self.lock:
            self.check()
            reserved = reservation in self.reserved if reservation else False
            if not reserved and len(self.ids) + len(self.reserved) >= self.limits['max_agent_calls']:
                self._deny('agent_call_limit')
            # Once this succeeds, even a crash before the callback cannot refund the
            # ledger record: resume reconstructs ids from the files, including started.
            persist_started()
            self.ids.add(record['attempt_id'])
            if reserved:
                self.reserved.remove(reservation)
            self.checkpoint()
            self.persist()

    def raise_limits(self, changes):
        validate_limits(changes)
        with self.lock:
            for key, value in changes.items():
                if value < self.limits[key]:
                    raise ValueError(f'cannot lower {key} on resume ({value} < {self.limits[key]})')
            for key, value in changes.items():
                if value != self.limits[key]:
                    old = self.limits[key]
                    self.limits[key] = value
                    self.event('budget_limit_raised', limit=key, previous=old, value=value)
            self.checkpoint()
            self.persist()

    def wait(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            self.check()
            self.cancel.wait(min(.1, max(0, end-self.clock()), max(0, self.deadline-self.clock())))
        self.check()

    def close(self):
        self.cancel.set()
        with self.lock:
            self.checkpoint()
            self.closed = True
            self.data['session_open'] = False
            self.persist()
