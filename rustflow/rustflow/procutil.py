"""Cross-platform subprocess helpers (Windows + POSIX).

- resolve_command(): finds an executable; on Windows unwraps npm .cmd shims to `node script.js`
  so arguments (JSON schemas, quotes) never pass through cmd.exe.
- run(): stdin as UTF-8 text, hard timeout that kills the whole process tree.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Children run in their own process group (so a timeout can kill the whole tree), which also means
# they do not receive Ctrl+C. Track them so the orchestrator can kill everything on interrupt.
_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def kill_all() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        kill_tree(p)

_NPM_SHIM_RE = re.compile(r'"%(?:~?dp0|_prog)%\\?([^"%]+?\.(?:c?js|mjs))"', re.IGNORECASE)
_NPM_SHIM_RE2 = re.compile(r'%~?dp0%?\\([^"\s]+?\.(?:c?js|mjs))', re.IGNORECASE)


class CommandNotFound(RuntimeError):
    pass


def resolve_command(name_or_path: str) -> list[str]:
    """Return argv prefix for an executable name or path."""
    expanded = os.path.expanduser(name_or_path)
    candidate = expanded if (os.sep in expanded or "/" in expanded) else shutil.which(expanded)
    if not candidate or not Path(candidate).exists():
        raise CommandNotFound(f"executable not found: {name_or_path}")
    path = Path(candidate)
    if IS_WINDOWS and path.suffix.lower() in (".cmd", ".bat"):
        script = _unwrap_npm_shim(path)
        if script:
            node = shutil.which("node")
            if node:
                return [node, str(script)]
        # Fallback: run the shim itself (works for simple arguments).
    if path.suffix.lower() in (".py",):
        return [sys.executable, str(path)]
    return [str(path)]


def _unwrap_npm_shim(shim: Path) -> Path | None:
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for rx in (_NPM_SHIM_RE, _NPM_SHIM_RE2):
        m = rx.search(text)
        if m:
            script = (shim.parent / m.group(1).replace("\\", os.sep)).resolve()
            if script.is_file():
                return script
    return None


# Session-identity variables of a parent Claude Code / Codex session. A child agent that inherits
# them can attach to (and write into) the parent's session, or skip work as a "nested" session.
# Auth/provider variables (ANTHROPIC_*, CLAUDE_CODE_USE_BEDROCK, OPENAI_*, ...) are kept.
SESSION_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SESSION_ORIGIN", "CLAUDE_PID",
    "CLAUDE_EFFORT", "CLAUDE_PROJECT_DIR", "CLAUDE_AFTER_LAST_COMPACT", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
    "RUSTFLOW_ROLE", "RUSTFLOW_PRIVATE_CONSOLE",
)


def clean_env(extra_scrub: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    env = os.environ.copy()
    for key in (*SESSION_ENV, *extra_scrub):
        env.pop(key, None)
    return env


@dataclass
class ProcResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _communicate_polled(proc: subprocess.Popen, stdin_text: str | None,
                        timeout_s: float | None) -> tuple[str | None, str | None]:
    """Let Python handle interrupts between Windows pipe-reader joins (Python 3.11/3.13)."""
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    while True:
        remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0.2
        try:
            return proc.communicate(input=stdin_text, timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                raise
            # communicate retains its buffers across timeouts; input must be sent only once.
            stdin_text = None


def run(
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
    stdout_path: Path | None = None,
    isolate_session: bool = False,
) -> ProcResult:
    """Run a command; never raises on non-zero exit. Kills the process tree on timeout.
    isolate_session=True drops parent agent-session variables (see SESSION_ENV)."""
    full_env = clean_env() if isolate_session else os.environ.copy()
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        full_env.update(env)
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    out_fh = stdout_path.open("w", encoding="utf-8", errors="replace") if stdout_path else None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=out_fh if out_fh else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except FileNotFoundError as exc:
        if out_fh:
            out_fh.close()
        return ProcResult(argv, 127, "", str(exc), 0.0)
    timed_out = False
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    try:
        if IS_WINDOWS:
            stdout, stderr = _communicate_polled(proc, stdin_text, timeout_s)
        else:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # a grandchild escaped the tree and still holds the pipe
            stdout, stderr = "", "process tree did not exit after kill"
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
        if out_fh:
            out_fh.close()
    if stdout_path:
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    return ProcResult(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )


def which(name: str) -> str | None:
    try:
        return resolve_command(name)[-1]
    except CommandNotFound:
        return None
