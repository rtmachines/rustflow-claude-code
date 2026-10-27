"""`rustflow doctor --selftest`: replay the hook scenarios exactly the way Claude Code runs them.

A throwaway crate gets a fresh rustflow install with the SAME interpreter your repo's hooks use; each
hook is then spawned in exec form (command + args from settings.json, ${CLAUDE_PROJECT_DIR}
substituted, UTF-8 JSON on stdin) and its exit code / output is checked. This is the scenario table
of rust-feedback-loop's README, automated - and the way to verify an install on Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from rustflow.installer import Installer

GOOD = "//! probe\n\n/// Adds one.\npub fn inc(x: i32) -> i32 {\n    x + 1\n}\n"
TYPE_ERROR = "//! probe\n\n/// Adds one.\npub fn inc(x: i32) -> i32 {\n    \"x\"\n}\n"
CLIPPY_RED = "//! probe\n\n/// Adds one.\npub fn inc(x: i32) -> i32 {\n    return x + 1;\n}\n"
UNFORMATTED = "//! probe\n\n/// Adds one.\npub fn inc( x:i32 )->i32{x+1}\n"
BAD_REPORT = "## Findings\n- [P1] oops\n## Verdict: APPROVE\n"


def registered_interpreter(repo: Path) -> str | None:
    for name in ("settings.json", "settings.local.json"):
        try:
            data = json.loads((repo / ".claude" / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for group in (data.get("hooks") or {}).get("Stop", []):
            for h in group.get("hooks", []):
                args = h.get("args") or []
                for i, a in enumerate(args):
                    if ".rustflow/hooks.py" in a:
                        cmd = h["command"]
                        return cmd if not args[:i] else " ".join([cmd, *args[:i]])
    return None


class SelfTest:
    def __init__(self, repo: Path | None, out=print):
        self.repo = repo
        self.out = out
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        self.out(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))

    # exec-form hook invocation, as Claude Code does it
    def hook_argv(self, event: str, sub: str) -> list[str] | None:
        data = json.loads((self.proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for group in (data.get("hooks") or {}).get(event, []):
            for h in group.get("hooks", []):
                args = h.get("args") or []
                if sub in args:
                    exe = shutil.which(h["command"]) or h["command"]
                    return [exe, *[a.replace("${CLAUDE_PROJECT_DIR}", str(self.proj)) for a in args]]
        return None

    def call(self, argv: list[str], payload: dict | None, role: str | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("RUSTFLOW_ROLE", "CLAUDECODE")}
        env["CLAUDE_PROJECT_DIR"] = str(self.proj)
        if role:
            env["RUSTFLOW_ROLE"] = role
        stdin = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        return subprocess.run(argv, input=stdin, capture_output=True, cwd=self.proj, env=env, timeout=900)

    def write_lib(self, text: str) -> None:
        (self.proj / "src" / "lib.rs").write_text(text, encoding="utf-8")

    def run(self) -> int:
        py = registered_interpreter(self.repo) if self.repo else None
        self.out(f"rustflow selftest (hook interpreter: {py or 'auto'})")
        tmp = Path(tempfile.mkdtemp(prefix="rustflow-selftest-"))
        self.proj = tmp / "probe"
        try:
            return self._run(py)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, py: str | None) -> int:
        subprocess.run(["cargo", "new", "--lib", "--vcs", "none", "-q", str(self.proj)], check=True)
        self.write_lib(GOOD)
        for args in (["init", "-q"], ["config", "user.email", "selftest@localhost"],
                     ["config", "user.name", "selftest"]):
            subprocess.run(["git", *args], cwd=self.proj, check=True, capture_output=True)
        (self.proj / ".gitignore").write_text("target/\n", encoding="utf-8")
        Installer(self.proj, python_cmd=py, ci=False, with_rust_skills=False, out=lambda *_: None).run()
        subprocess.run(["git", "add", "-A"], cwd=self.proj, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "probe"], cwd=self.proj, check=True, capture_output=True)

        stop = self.hook_argv("Stop", "stop")
        edit = self.hook_argv("PostToolUse", "post-edit")
        guard = self.hook_argv("PreToolUse", "guard-bash")
        route = self.hook_argv("UserPromptSubmit", "route-review")
        if not (stop and edit and guard):
            self.check("hooks registered in settings.json", False, "missing entries")
            return 1
        exe = Path(stop[0])
        runnable = exe.exists() and exe.suffix.lower() not in (".cmd", ".bat")
        self.check("hook command is a real executable (exec form)", runnable,
                   f"'{stop[0]}' not found or a .cmd/.bat shim - Claude Code cannot start it, so EVERY hook is "
                   "silently off. Reinstall with `rustflow init --python-cmd <path to python.exe or py>`")
        if not runnable:
            self.out("\nstopping: nothing else can run with this interpreter")
            return 1
        prefix = stop[:stop.index(next(a for a in stop if a.endswith("hooks.py")))]
        r = subprocess.run(prefix + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True)
        ver = r.stdout.strip()
        ok = r.returncode == 0 and tuple(int(x) for x in (ver or "0.0").split(".")) >= (3, 9)
        self.check(f"hook interpreter runs (Python {ver or '?'})", ok, r.stderr.strip()[:200])

        self.write_lib(TYPE_ERROR)
        r = self.call(edit, {"tool_input": {"file_path": str(self.proj / "src" / "lib.rs")}})
        err = r.stderr.decode("utf-8", "replace")
        self.check("post-edit: compile error goes back to the model (exit 2)", r.returncode == 2 and "E0308" in err,
                   f"exit {r.returncode}: {err[-300:]}")

        self.write_lib(CLIPPY_RED)
        r = self.call(stop, {"session_id": "selftest", "hook_event_name": "Stop"})
        out = r.stdout.decode("utf-8", "replace")
        try:
            blocked = json.loads(out).get("decision") == "block"
        except json.JSONDecodeError:
            blocked = False
        self.check("stop: red clippy blocks the end of the turn", blocked, out[-300:] or r.stderr.decode()[-300:])

        self.write_lib(GOOD)
        r = self.call(stop, {"session_id": "selftest", "hook_event_name": "Stop"})
        self.check("stop: green gates let the agent finish", r.returncode == 0 and not r.stdout.strip(),
                   r.stdout.decode("utf-8", "replace")[-300:])

        def decision(cmd: str, role: str | None) -> str | None:
            r = self.call(guard, {"tool_input": {"command": cmd}}, role)
            try:
                return json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"]
            except (json.JSONDecodeError, KeyError):
                return None
        self.check("guard: reviewer may run `git diff`", decision("git diff HEAD", "reviewer_a") == "allow")
        self.check("guard: reviewer may not commit", decision("git commit -am x", "reviewer_a") == "deny")
        self.check("guard: implementer may not `git -C . commit`", decision("git -C . commit -m x", "implementer") == "deny")
        self.check("guard: UTF-8 payload (zażółć) is decoded", decision("cat plik_zażółć.rs > x", "reviewer_a") == "deny")
        self.check("guard: no sqlx migrate revert on a remote DB",
                   decision("sqlx migrate revert --database-url postgres://u@prod.example.com/app", None) == "deny")

        if route:
            r = self.call(route, {"prompt": "zrób review tej gałęzi"})
            self.check("route-review steers to /rustflow-pr-review", b"rustflow-pr-review" in r.stdout)
        report = [a for a in stop if a.endswith("hooks.py")]
        r = self.call(prefix + report + ["check-report"], {"session_id": "st", "last_assistant_message": BAD_REPORT})
        self.check("check-report rejects an invalid review report (exit 2)", r.returncode == 2,
                   r.stderr.decode("utf-8", "replace")[-200:])
        r = self.call(prefix + report + ["review-context"], None)
        self.check("review-context (skill `!` command) exits 0", r.returncode == 0 and b"BASE_SHA=" in r.stdout)

        self.write_lib(UNFORMATTED)
        gates_py = str(self.proj / ".rustflow" / "gates.py")
        self.call(prefix + [gates_py, "run", "--profile", "full", "--check"], None)
        self.check("gates --check never modifies the tree",
                   (self.proj / "src" / "lib.rs").read_text(encoding="utf-8") == UNFORMATTED)

        failed = [n for n, ok, _ in self.results if not ok]
        self.out(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed")
        return 1 if failed else 0
