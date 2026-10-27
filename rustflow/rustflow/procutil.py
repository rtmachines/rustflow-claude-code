"""Cross-platform subprocess helpers (Windows + POSIX).

- resolve_command(): finds an executable; on Windows unwraps npm .cmd shims to `node script.js`
  so arguments (JSON schemas, quotes) never pass through cmd.exe.
- run(): stdin as UTF-8 text, hard timeout that kills the whole process tree.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

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


# Public compatibility names share the standalone executor and active process registry.
from rustflow.process import ProcessResult as ProcResult, kill_tree, kill_all, run_process


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
    max_output_bytes: int | None = None,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> ProcResult:
    """Run a command; never raises on non-zero exit. Kills the process tree on timeout.
    isolate_session=True drops parent agent-session variables (see SESSION_ENV)."""
    full_env = clean_env() if isolate_session else os.environ.copy()
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        full_env.update(env)
    return run_process(argv, cwd=str(cwd) if cwd else None, stdin_text=stdin_text, env=full_env,
                       timeout_s=timeout_s, stdout_path=stdout_path, max_output_bytes=max_output_bytes,
                       deadline=deadline, cancel=cancel)


def which(name: str) -> str | None:
    try:
        return resolve_command(name)[-1]
    except CommandNotFound:
        return None
