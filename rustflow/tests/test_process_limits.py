"""Real producers exercise byte limits and pipe/process cleanup in both adapters."""
import os
import sys
import time

import pytest

from rustflow import gates, procutil


@pytest.mark.parametrize('extra', [0, 1])
@pytest.mark.parametrize('adapter', ['proc', 'gates'])
def test_shared_raw_byte_boundary(tmp_path, adapter, extra):
    argv = [sys.executable, '-c', f'import os; os.write(1, b"a"*700); os.write(2, b"b"*{324+extra})']
    if adapter == 'proc':
        r = procutil.run(argv, max_output_bytes=1024)
    else:
        r = gates.run_tree(argv, tmp_path, os.environ.copy(), 5, max_output_bytes=1024)
    assert r.output_complete is (extra == 0)
    assert r.termination_reason == ('output_limit' if extra else 'exited')
    assert r.stdout_bytes + r.stderr_bytes == 1024
    assert len(r.stdout.encode()) + len(r.stderr.encode()) == 1024
    assert r.ok is (extra == 0)


def test_large_stdin_is_sent_while_output_is_drained(tmp_path):
    r = procutil.run([sys.executable, '-c',
        'import os,sys; os.write(1,b"x"*100000); data=sys.stdin.buffer.read(); print(len(data))'],
        stdin_text='z'*1000000, timeout_s=5, max_output_bytes=110000)
    assert r.ok and r.stdout.endswith('1000000\n')


def test_infinite_output_and_stdout_file_are_bounded(tmp_path):
    path = tmp_path / 'out'
    start = time.monotonic()
    r = procutil.run([sys.executable, '-c', 'import os\nwhile True: os.write(1,b"x"*8192)'],
                     stdout_path=path, max_output_bytes=10000, timeout_s=5)
    assert time.monotonic() - start < 4
    assert r.termination_reason == 'output_limit' and not r.ok
    assert r.stdout_bytes == 10000 and path.stat().st_size == 10000


def test_utf8_cut_counts_bytes_not_decoded_characters():
    r = procutil.run([sys.executable, '-c', 'import os; os.write(1,"ą".encode()*3)'], max_output_bytes=3)
    assert r.stdout_bytes == 3 and r.stdout == 'ą�'
    assert not r.output_complete


@pytest.mark.skipif(os.name == 'nt', reason='POSIX escaped process group')
def test_descendant_holding_pipe_cannot_hang_parent():
    start = time.monotonic()
    r = procutil.run([sys.executable, '-c',
        'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(4)"],start_new_session=True)'],
        timeout_s=.2)
    assert time.monotonic() - start < 3
    assert not r.output_complete


def test_incomplete_gate_cannot_prove_test_or_enter_cache(tmp_path, monkeypatch):
    from rustflow import criteria
    monkeypatch.setattr(gates, 'tree_fingerprint', lambda _: 'tree')
    runner = gates.GateRunner(tmp_path, {'features': [], 'tests': 'cargo', 'tree_stable': False,
                                        'max_output_bytes': 30})
    monkeypatch.setattr(gates, 'run_tree', lambda *a, **kw: procutil.ProcResult(
        ['cargo'], 0, 'test required ... ok\n', '', 0,
        termination_reason='output_limit', output_complete=False, stdout_bytes=30, max_output_bytes=30))
    monkeypatch.setattr(gates, 'GateRunner', lambda *a, **kw: runner)
    result = gates.run_gates(tmp_path, 'edit')
    assert not result.ok and not result.steps[0].output_complete
    assert not gates.cache_lookup(tmp_path, 'edit', 'tree')
    steps = [gates.Step('tests', False, passed_tests=['required'], output_complete=False)]
    checked = criteria.evaluate([criteria.Criterion('C1', 'must pass', 'required')], steps)
    assert checked[0]['status'] == 'not_met'


def test_expired_deadline_does_not_spawn_and_cancel_interrupts_process(tmp_path):
    import threading
    marker = tmp_path / 'spawned'
    argv = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
    r = procutil.run(argv, deadline=time.monotonic()-1)
    assert r.termination_reason == 'active_time_limit' and not marker.exists()
    cancel = threading.Event()
    timer = threading.Timer(.1, cancel.set)
    timer.start()
    try:
        r = procutil.run([sys.executable, '-c', 'import time; time.sleep(30)'], cancel=cancel)
    finally:
        timer.join()
    assert r.termination_reason == 'cancelled' and r.duration_s < 3


def test_completed_gate_reports_actual_captured_byte_counts(tmp_path, monkeypatch):
    runner = gates.GateRunner(tmp_path, {'features': [], 'fmt_fix_scope': 'all', 'max_output_bytes': 100})
    monkeypatch.setattr(gates, 'run_tree', lambda *a, **kw: procutil.ProcResult(
        ['cargo'], 0, 'hello', 'warning', .1, stdout_bytes=5, stderr_bytes=7, max_output_bytes=100))
    step = runner.run('edit')[0]
    assert step.ok and step.output_complete
    assert (step.stdout_bytes, step.stderr_bytes, step.max_output_bytes) == (5, 7, 100)
