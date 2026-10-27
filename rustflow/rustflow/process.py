"""Bounded binary process capture, shared by the package and standalone gates (Python 3.9+).

Readers keep only a shared byte allowance, never an unbounded queue. Pipe polling
allows cleanup even when a descendant escapes the process group holding a pipe.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
CHUNK = 8192
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_POLICY = contextvars.ContextVar('rustflow_process_policy', default={})


@contextlib.contextmanager
def execution_scope(**policy):
    token = _POLICY.set({**_POLICY.get(), **policy})
    try:
        yield
    finally:
        _POLICY.reset(token)


@dataclass
class ProcessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    termination_reason: str = 'exited'
    output_complete: bool = True
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self):
        # Compatibility with callers constructing legacy timeout results.
        if self.timed_out:
            self.termination_reason = 'timeout'
            self.output_complete = False

    @property
    def ok(self):
        return self.returncode == 0 and self.output_complete and self.termination_reason == 'exited'

    def __iter__(self):
        # Historical gates.run_tree tuple interface.
        yield self.returncode if self.ok or self.returncode else 124
        yield self.stdout
        yield self.stderr


def kill_tree(proc):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
        else:
            # The leader may already have exited while its children still hold pipes.
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def kill_all():
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for proc in procs:
        kill_tree(proc)


def _available(pipe):
    if os.name != 'nt':
        return CHUNK if select.select([pipe], [], [], .05)[0] else 0
    import ctypes
    import msvcrt
    available = ctypes.c_ulong()
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    peek = kernel32.PeekNamedPipe
    peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    peek.restype = ctypes.c_int
    if not peek(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None):
        return -1  # closed/broken pipe
    if not available.value:
        time.sleep(.02)
    return min(CHUNK, available.value)


def run_process(argv, *, cwd=None, env=None, stdin_text=None, timeout_s=None,
                stdout_path=None, max_output_bytes=None, deadline=None, cancel=None):
    policy = _POLICY.get()
    limit = max_output_bytes if max_output_bytes is not None else policy.get('max_output_bytes', DEFAULT_MAX_OUTPUT_BYTES)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError('max_output_bytes must be a positive integer')
    deadline = deadline if deadline is not None else policy.get('deadline')
    cancel = cancel if cancel is not None else policy.get('cancel')
    start = time.monotonic()
    local_deadline = start + timeout_s if timeout_s is not None else None
    if deadline is not None and (local_deadline is None or deadline <= local_deadline):
        local_deadline, timeout_reason = deadline, 'active_time_limit'
    else:
        timeout_reason = 'timeout'
    def stopped():
        if cancel is not None and cancel.is_set():
            return 'cancelled'
        if local_deadline is not None and time.monotonic() >= local_deadline:
            return timeout_reason
        return ''
    reason = stopped()
    if reason:
        return ProcessResult(argv, 124, '', '', 0, termination_reason=reason, output_complete=False,
                             max_output_bytes=limit)
    kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **kwargs)
    except OSError as exc:
        return ProcessResult(argv, 127, '', str(exc), time.monotonic()-start, termination_reason='spawn_error',
                             max_output_bytes=limit)
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    buffers = [bytearray(), bytearray()]
    lock = threading.Lock()
    stop = threading.Event()
    overflow = threading.Event()
    io_error = threading.Event()
    done = [threading.Event(), threading.Event()]
    def read(pipe, index):
        try:
            while not stop.is_set():
                ready = _available(pipe)
                if ready < 0:
                    break
                if not ready:
                    continue
                chunk = os.read(pipe.fileno(), ready)
                if not chunk:
                    break
                with lock:
                    left = limit - len(buffers[0]) - len(buffers[1])
                    buffers[index].extend(chunk[:left])
                    if len(chunk) > left:
                        overflow.set()
                        break
        except (OSError, ValueError):
            if not stop.is_set():
                io_error.set()
        finally:
            done[index].set()
    def write():
        try:
            # Encode in bounded pieces too; the caller already owns the input string.
            for offset in range(0, len(stdin_text), CHUNK):
                data = stdin_text[offset:offset+CHUNK].encode('utf-8')
                while data and not stop.is_set():
                    count = os.write(proc.stdin.fileno(), data[:CHUNK])
                    data = data[count:]
        except (OSError, ValueError):
            pass
        finally:
            proc.stdin.close()
    readers = [threading.Thread(target=read, args=(pipe, i), daemon=True)
               for i, pipe in enumerate((proc.stdout, proc.stderr))]
    writer = threading.Thread(target=write, daemon=True) if stdin_text is not None else None
    try:
        for reader in readers:
            reader.start()
        if writer:
            writer.start()
        exited_at = None
        while True:
            if overflow.is_set():
                reason = 'output_limit'
                break
            reason = stopped()
            if reason:
                break
            if io_error.is_set():
                reason = 'pipe_error'
                break
            if proc.poll() is not None:
                if all(d.is_set() for d in done):
                    reason = 'exited'
                    break
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at >= .5:
                    reason = 'pipe_timeout'
                    break
            time.sleep(.01)
        if reason != 'exited':
            kill_tree(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=.2)
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe is not None:
                pipe.close()
        if writer:
            writer.join(timeout=.2)
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    if overflow.is_set():
        reason = "output_limit"
    with lock:
        out, err = buffers
        if stdout_path:
            with open(stdout_path, 'wb') as fh:
                fh.write(out)
        return ProcessResult(argv, proc.returncode if proc.returncode is not None else -1,
                             out.decode('utf-8', errors='replace'), err.decode('utf-8', errors='replace'),
                             time.monotonic()-start, timed_out=reason == 'timeout', termination_reason=reason,
                             output_complete=reason == 'exited', stdout_bytes=len(out), stderr_bytes=len(err),
                             max_output_bytes=limit)
