"""Run control plane for agents driving rustflow: detached start, wait, cancel, compact summaries,
and `next_actions` - a small decision protocol so a driving agent knows what to do with each outcome.

Exit codes of `rustflow wait` / `status`: 0 approved, 2 finished not approved, 3 crashed,
75 still running (EX_TEMPFAIL - call `wait` again).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from rustflow import gates

RUNS = Path(".rustflow") / "runs"
APPROVED = ("approved", "approved_with_advisories")
STILL_RUNNING = 75
MAX_BRIEF_FINDINGS = 10


class RunLocked(RuntimeError):
    pass


# ── lock ─────────────────────────────────────────────────────────────────────
def acquire(repo: Path, state: dict) -> None:
    act = gates.read_active(repo)
    if act and act.get("run_id") != state["run_id"]:
        raise RunLocked(f"run {act['run_id']} is still active in this repo (pid {act['pid']}). "
                        f"Wait: `rustflow wait {act['run_id']}`, stop: `rustflow cancel {act['run_id']}`.")
    path = gates.active_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": state["run_id"], "pid": os.getpid(), "workdir": state.get("workdir", str(repo)),
        "isolation": state.get("isolation", ""), "branch": state.get("branch", ""),
        "started": _dt.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")


def update_lock(repo: Path, state: dict) -> None:
    path = gates.active_lock_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if data.get("run_id") == state["run_id"]:
        data.update(workdir=state.get("workdir", data.get("workdir")), isolation=state.get("isolation", ""),
                    branch=state.get("branch", ""), phase=state.get("phase", ""))
        path.write_text(json.dumps(data), encoding="utf-8")


def release(repo: Path, run_id: str) -> None:
    path = gates.active_lock_path(repo)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("run_id") == run_id:
            path.unlink()
    except (OSError, json.JSONDecodeError):
        pass


# ── detached start ───────────────────────────────────────────────────────────
def new_run_id(task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:32].strip("-") or "task"
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug


def spawn_detached(repo: Path, cli_args: list[str], task: str | None = None, run_id: str | None = None,
                   wait_start_s: float = 20.0) -> dict:
    """Start `rustflow <cli_args>` in the background; returns once the run owns the lock (or failed).
    With `task`: a new run (task written to the run dir, passed as -f). Without: cli_args must name the
    run (resume)."""
    act = gates.read_active(repo)
    if act:
        raise RunLocked(f"run {act['run_id']} is still active in this repo. "
                        f"Wait: `rustflow wait {act['run_id']}`, stop: `rustflow cancel {act['run_id']}`.")
    if run_id is None and task is None:
        raise ValueError("run_id or task required")
    run_id = run_id or new_run_id(task or "")
    run_dir = repo / RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "rustflow", *cli_args]
    if task is not None:
        (run_dir / "task.md").write_text(task, encoding="utf-8")
        argv += ["--run-id", run_id, "-f", str(run_dir / "task.md")]
    else:
        try:  # resume: wait for this process, not the old state.json
            (run_dir / "state.json").touch()
        except OSError:
            pass
    log = (run_dir / "console.log").open("a", encoding="utf-8")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                                   | subprocess.DETACHED_PROCESS)      # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            env=env, close_fds=True, **kwargs)
    log.close()
    deadline = time.monotonic() + wait_start_s
    while time.monotonic() < deadline:
        state_file = run_dir / "state.json"
        if state_file.is_file() and proc.poll() is None:
            try:
                if task is not None or int(json.loads(state_file.read_text(encoding="utf-8")).get("pid") or 0) == proc.pid:
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    info = {"run_id": run_id, "pid": proc.pid, "run_dir": str(run_dir), "console": str(run_dir / "console.log")}
    if proc.poll() is not None and not (run_dir / "state.json").is_file():
        info["error"] = (run_dir / "console.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        info["status"] = "failed_to_start"
    else:
        info["status"] = "started" if proc.poll() is None else "finished_immediately"
        info["next"] = f"rustflow wait {run_id} --timeout 540"
    return info


# ── reading runs ─────────────────────────────────────────────────────────────
def find_run(repo: Path, ref: str | None) -> Path:
    runs = repo / RUNS
    if not ref or ref == "latest":
        latest = runs / "LATEST"
        if latest.is_file():
            ref = latest.read_text(encoding="utf-8").strip()
        else:
            cands = sorted((p for p in runs.glob("*") if (p / "state.json").is_file()), key=lambda p: p.stat().st_mtime)
            if not cands:
                raise FileNotFoundError("no runs found")
            return cands[-1]
    path = runs / ref
    if not (path / "state.json").is_file():
        matches = [p for p in runs.glob(f"*{ref}*") if (p / "state.json").is_file()]
        if len(matches) != 1:
            raise FileNotFoundError(f"run '{ref}' not found")
        path = matches[0]
    return path


def load_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8"))


def effective_status(state: dict) -> str:
    st = state.get("status", "running")
    if st == "running" and not gates.pid_alive(int(state.get("pid") or 0)):
        return "crashed"  # the orchestrator process is gone without writing a final status
    return st


# ── decision protocol ────────────────────────────────────────────────────────
def next_actions(state: dict) -> list[dict]:
    st = effective_status(state)
    rid = state.get("run_id", "?")
    branch, orig = state.get("branch"), state.get("original_branch", "main")
    base = (state.get("base_sha") or "")[:12]
    open_ = [f for f in state.get("findings", []) if f.get("status") == "open"]
    disputed = [f for f in state.get("findings", []) if f.get("status") == "disputed"]
    acts: list[dict] = []
    # Another fix round continues THIS run: same branch/worktree, checkpoints and finding ids.
    more = f"rustflow resume {rid} --extra-rounds 2 --detach"
    if st == "running":
        return [{"action": "wait", "command": f"rustflow wait {rid} --timeout 540",
                 "note": "do not edit files or change git state in this checkout meanwhile"}]
    if st in APPROVED:
        acts.append({"action": "present_result", "note": "summarize the change and the report for the user"})
        if disputed:
            acts.append({"action": "ask_user", "question": "Reviewers and fixer disagreed on: " + "; ".join(
                f"[{f['id']}] {f['title']}" for f in disputed) + ". Accept as is or fix?"})
        if state.get("escalate"):
            acts.append({"action": "offer_audit", "command": "/rust-review:rust-review threat model REMOTE, "
                         "use sonnet, high severity only", "needs_user": True,
                         "note": "unsafe/FFI/atomics changed - soundness audit recommended before merge"})
        if branch and state.get("isolation") == "branch":
            acts.append({"action": "merge", "needs_user": True,
                         "command": f"git switch {orig} && git merge --squash {branch}"})
        elif branch and state.get("isolation") == "worktree":
            acts.append({"action": "merge", "needs_user": True, "command": f"git merge --squash {branch}",
                         "note": f"in the main checkout ({orig} is checked out there); afterwards "
                                 f"`git worktree remove {state.get('workdir', '')}`"})
        acts.append({"action": "comment_pr", "command": f"rustflow comment {rid}", "needs_user": True})
        return acts
    if st == "changes_requested":
        return [{"action": "ask_user", "question": f"{len(open_)} blocking finding(s). Run the fix loop?",
                 "command": f"rustflow review --fix --base {base} --detach"}]
    if st == "criteria_unmet":
        missing = [m for m in state.get("mechanical", []) if m.get("status") != "met"]
        return [{"action": "ask_user", "question": "Review approved, but required tests are not proven: "
                 + "; ".join(f"`{m['test']}` ({m.get('detail', '')})" for m in missing[:5])
                 + ". Add/fix them in another fix round, fix by hand, or drop the criterion?",
                 "command": more}]
    if st in ("max_rounds", "stalled", "final_gates_red"):
        return [{"action": "ask_user", "question": (f"Run ended '{st}' with {len(open_)} blocking finding(s): "
                 + "; ".join(f"[{f['id']}] {f['title']}" for f in open_[:5]) + ". Another fix round, fix by "
                 "hand, or stop?"), "command": more}]
    if st == "gates_red":
        return [{"action": "inspect", "command": "rustflow gates --profile full",
                 "note": "the code does not pass the gates even after gate-fix attempts"},
                {"action": "ask_user", "question": "Gates stay red - fix manually, or retry with a stronger model "
                 "(--difficulty hard)?"}]
    if st == "baseline_red":
        return [{"action": "ask_user", "question": "The base branch is already red. Fix it first, or continue "
                 "with --accept-red-baseline (only new failures block)?"}]
    if st in ("budget", "interrupted", "crashed", "tree_changed", "agent_failed"):
        failed = state.get("failed_agent") or {}
        hint = {"budget": "raise [budget].max_total_usd in rustflow.toml, then",
                "interrupted": "", "crashed": "check console.log / stop_reason, then",
                "tree_changed": "files changed during the review; stop editing this checkout, then",
                "agent_failed": (f"{failed.get('name', 'an agent')} failed, nothing changed - likely transient "
                                 "(overloaded/network); retry" if failed.get("retryable", True) else
                                 f"{failed.get('name', 'an agent')} failed permanently - fix it first "
                                 "(`rustflow doctor`: login, model names), then")}[st]
        return [{"action": "resume", "command": f"rustflow resume {rid} --detach", "note": hint.strip()}]
    if st == "review_failed":
        return [{"action": "inspect", "command": "rustflow doctor", "note": "no reviewer returned a result - "
                 "CLI auth/model names?"}]
    if st == "no_changes":
        return [{"action": "ask_user", "question": "The implementer made no changes - clarify the task?"}]
    return [{"action": "inspect", "note": state.get("stop_reason", "")[:500]}]


def summary(state: dict, *, findings_limit: int = MAX_BRIEF_FINDINGS) -> dict:
    st = effective_status(state)
    fs = state.get("findings", [])
    def brief(f: dict) -> dict:
        return {"id": f["id"], "severity": f["severity"],
                "where": f"{f['file']}:{f['line']}" if f.get("file") else "", "title": f["title"][:160]}
    return {
        "run_id": state.get("run_id"), "status": st, "phase": state.get("phase"), "round": state.get("round"),
        "difficulty": state.get("difficulty"), "cost_usd": state.get("cost_usd", 0.0),
        "isolation": state.get("isolation"), "branch": state.get("branch"), "workdir": state.get("workdir"),
        "blocking": [brief(f) for f in fs if f.get("status") == "open"][:findings_limit],
        "disputed": [brief(f) for f in fs if f.get("status") == "disputed"][:findings_limit],
        "advisory_count": sum(1 for f in fs if f.get("status") == "advisory"),
        "escalate": bool(state.get("escalate")), "audit_report": (state.get("audit") or {}).get("report"),
        "final_gates": state.get("final_gates"), "stop_reason": (state.get("stop_reason") or "")[:600],
        "report": str(Path(state.get("repo", ".")) / RUNS / state.get("run_id", "") / "report.md"),
        "next_actions": next_actions(state),
    }


def brief_markdown(s: dict) -> str:
    lines = [f"rustflow {s['run_id']}: **{s['status']}** (phase {s['phase']}, round {s['round']}, "
             f"{s['difficulty']}, ${s['cost_usd']:.2f})"]
    if s.get("branch"):
        lines.append(f"branch: {s['branch']} ({s['isolation']})")
    if s.get("final_gates"):
        lines.append(f"final gates: {s['final_gates']}")
    for key, label in (("blocking", "blocking"), ("disputed", "disputed")):
        if s[key]:
            lines.append(f"{label}:")
            lines += [f"  [{f['id']}] {f['severity']} {f['where']} {f['title']}" for f in s[key]]
    if s["advisory_count"]:
        lines.append(f"advisory: {s['advisory_count']}")
    if s["escalate"]:
        lines.append("ESCALATE: soundness audit recommended" + (f" (report: {s['audit_report']})"
                                                               if s.get("audit_report") else ""))
    if s.get("stop_reason") and s["status"] not in APPROVED:
        lines.append("reason: " + s["stop_reason"].splitlines()[0][:300])
    lines.append("next:")
    for a in s["next_actions"]:
        lines.append("  - " + a["action"] + (f": {a['command']}" if a.get("command") else "")
                     + (f" - {a['question']}" if a.get("question") else "")
                     + (f" ({a['note']})" if a.get("note") else "") + (" [needs user]" if a.get("needs_user") else ""))
    lines.append(f"full report: {s['report']}")
    return "\n".join(lines)


def exit_code(status: str) -> int:
    if status in APPROVED:
        return 0
    if status == "running":
        return STILL_RUNNING
    if status == "crashed":
        return 3
    return 2


# ── wait / cancel ────────────────────────────────────────────────────────────
def wait(run_dir: Path, timeout_s: float, out=print, poll_s: float = 2.0) -> dict:
    """Block until the run finishes or timeout; stream only NEW orchestrator log lines (compact)."""
    log = run_dir / "rustflow.log"
    offset_file = run_dir / ".wait-offset"
    try:
        offset = int(offset_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        offset = 0
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
        except OSError:
            chunk = ""
        for line in chunk.splitlines():
            out(line)
        offset_file.write_text(str(offset), encoding="utf-8")
        state = load_state(run_dir)
        status = effective_status(state)
        if status != "running" or time.monotonic() >= deadline:
            return summary(state)
        time.sleep(poll_s)


def cancel(repo: Path, run_dir: Path, grace_s: float = 20.0) -> dict:
    """Graceful stop: the orchestrator gets an interrupt, kills its agents, saves state (resumable)."""
    state = load_state(run_dir)
    pid = int(state.get("pid") or 0)
    if not gates.pid_alive(pid):
        return {"run_id": state.get("run_id"), "status": effective_status(state), "note": "not running"}
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.kill(pid, signal.SIGINT)
    except OSError:
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and gates.pid_alive(pid):
        time.sleep(0.5)
    forced = False
    if gates.pid_alive(pid):
        forced = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        release(repo, state.get("run_id", ""))
    state = load_state(run_dir)
    return {"run_id": state.get("run_id"), "status": effective_status(state), "forced": forced,
            "next": f"rustflow resume {state.get('run_id')} --detach"}
