"""`rustflow mcp`: rustflow as an MCP server (stdio, JSON-RPC 2.0, stdlib only) for a driving agent.

Why: typed tool inputs instead of shell strings, no Bash permission prompts, and long runs that never
hit the Bash tool timeout (start is detached, wait is bounded). Registered by `rustflow init --with-mcp`
in `.mcp.json`. Every tool answers with compact text plus `structuredContent`. Claude Code shows the model the
`structuredContent` (measured on 2.1.308), so everything a driver must read belongs there; the text is for
other clients.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from rustflow import __version__, gates, gitutil, runs, triage
from rustflow.config import DIFFICULTIES, Config

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
# Claude Code warns above 10k tokens, caps MCP output at 25k tokens (MAX_MCP_OUTPUT_TOKENS) and saves text
# results over 50,000 characters to a file instead of showing them: stay well below.
MAX_TEXT_CHARS = 40_000
# The Bash tool stops at 10 min, an MCP call does not (MCP_TOOL_TIMEOUT defaults to ~28 h): longer waits
# mean fewer driver turns, each of which re-reads the whole conversation.
MAX_WAIT_S = 1800
INSTRUCTIONS = (
    "rustflow runs a full Rust change workflow (triage -> plan -> implement -> gates -> 2 independent reviews "
    "-> fix loop -> final gates) with Claude Code/Codex agents. Use rustflow_triage to see difficulty, model "
    "routing and estimated cost; rustflow_start returns immediately with a run_id; poll with rustflow_wait "
    "(bounded, repeat while status is 'running'); act on next_actions - each step names its MCP tool call. While a "
    "run is active do not edit files or change git state in its checkout.")


def _repo() -> Path:
    start = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    return gitutil.toplevel(start) if gitutil.is_repo(start) else start


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


RUN_REF = {"type": "string", "description": "run id (or unique part of it); default: the latest run"}

LIMIT_PROPERTIES = {k: {"type": "integer", "minimum": 1,
                         "description": "execution limit; resume only permits an increase"}
                    for k in ("max_agent_calls", "max_active_seconds", "max_output_bytes")}

TOOLS: list[dict] = [
    {"name": "rustflow_triage",
     "description": "Difficulty, per-role model routing and estimated cost (from this repo's run history) for a "
                    "Rust task. No agents are started. Use before rustflow_start to decide/ask the user.",
     "inputSchema": _obj({"task": {"type": "string"}}, ["task"])},
    {"name": "rustflow_start",
     "description": "Start a rustflow run in the background and return its run_id at once. mode=run implements "
                    "`task` on a new branch; mode=review reviews existing changes against `base` (fix=true also "
                    "fixes them in place). Fails if another run is active in this repo.",
     "inputSchema": _obj({
         **LIMIT_PROPERTIES,
         "mode": {"type": "string", "enum": ["run", "review"], "default": "run"},
         "task": {"type": "string", "description": "task (run) or intent/context (review)"},
         "base": {"type": "string"}, "difficulty": {"type": "string", "enum": list(DIFFICULTIES)},
         "isolation": {"type": "string", "enum": ["branch", "worktree", "none"],
                       "description": "run only. worktree keeps your checkout untouched (recommended when you keep "
                                      "working); review always works in place"},
         "fix": {"type": "boolean", "description": "review only: also run the fix loop"},
         "max_rounds": {"type": "integer", "minimum": 1},
         "allow_dirty": {"type": "boolean", "description": "run only (review accepts a dirty tree anyway)"},
         "accept_red_baseline": {"type": "boolean", "description": "run only"},
         "no_plan": {"type": "boolean", "description": "run only"}, "dry_run": {"type": "boolean"},
         "criteria": {"type": "string", "description": "path to a TOML file with [[criteria]] text/test; a named "
                      "test must actually run and pass (checked by rustflow, not by a model)"}}, ["mode"])},
    {"name": "rustflow_wait",
     "description": f"Wait until the run finishes or timeout_s elapses (max {MAX_WAIT_S}). Returns new progress "
                    "lines and a compact summary with next_actions. Call again while status is 'running'.",
     "inputSchema": _obj({"run": RUN_REF, "timeout_s": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_S,
                                                         "default": 600}})},
    {"name": "rustflow_status", "description": "Compact summary of a run (status, blocking findings, next_actions).",
     "inputSchema": _obj({"run": RUN_REF})},
    {"name": "rustflow_report",
     "description": "Run report: brief (default, ~30 lines), json (findings, compact), or full markdown. Long "
                    "output is truncated to fit the MCP limit and says how to get the rest.",
     "inputSchema": _obj({"run": RUN_REF, "format": {"type": "string", "enum": ["brief", "json", "full"],
                                                     "default": "brief"}})},
    {"name": "rustflow_cancel", "description": "Stop a running run gracefully; it stays resumable.",
     "inputSchema": _obj({"run": RUN_REF})},
    {"name": "rustflow_resume",
     "description": "Resume an interrupted / budget-stopped / crashed / agent_failed run in the background. With "
                    "extra_rounds: continue a run that ended max_rounds / stalled / final_gates_red / criteria_unmet "
                    "with up to that many more fix rounds on its own branch or worktree.",
     "inputSchema": _obj({**LIMIT_PROPERTIES, "run": RUN_REF, "extra_rounds": {"type": "integer", "minimum": 1, "maximum": 5}})},
    {"name": "rustflow_gates",
     "description": "Run the deterministic gates (fmt, clippy -D warnings, tests, doc tests, deny/semver/sqlx when "
                    "relevant). check_only=true never modifies files.",
     "inputSchema": _obj({"profile": {"type": "string", "enum": ["edit", "stop", "full"], "default": "full"},
                          "check_only": {"type": "boolean", "default": True}})},
]


# ── tool implementations ─────────────────────────────────────────────────────
def t_triage(a: dict) -> tuple[str, dict]:
    repo = _repo()
    cfg = Config.load(repo)
    res = triage.heuristic(a["task"], repo, cfg)
    routing = {r: str(cfg.spec_for(r, res.difficulty)) for r in ("implementer", "reviewer_a", "reviewer_b", "fixer")}
    if cfg.get("roles.planner.when") == "always" or (cfg.get("roles.planner.when") == "hard" and res.difficulty == "hard"):
        routing["planner"] = str(cfg.spec_for("planner", res.difficulty))
    est = triage.estimate_cost(repo, routing, res.difficulty)
    data = {**res.to_dict(), "routing": routing, "estimate": est}
    text = (f"difficulty {res.difficulty} (score {res.score}: {', '.join(res.reasons)}); routing "
            + ", ".join(f"{k}={v}" for k, v in routing.items()) + "; " + est["text"])
    return text, data


def t_start(a: dict) -> tuple[str, dict]:
    repo = _repo()
    mode = a.get("mode", "run")
    task = a.get("task") or ""
    if mode == "run" and not task.strip():
        raise ValueError("mode=run needs a task")
    if mode == "review" and not task:
        task = ("Review the current changes against the base. Judge correctness, robustness, tests and idioms; "
                "infer the intent from the diff and commit messages.")
    args = [mode]
    valued = {"base": "--base", "difficulty": "--difficulty", "max_rounds": "--max-rounds", "criteria": "--criteria"}
    valued.update({k: "--" + k.replace("_", "-") for k in LIMIT_PROPERTIES})
    if a.get("criteria"):
        crit = Path(a["criteria"])
        a = {**a, "criteria": str(crit if crit.is_absolute() else (repo / crit).resolve())}
    flags = {"dry_run": "--dry-run"}
    if mode == "run":
        valued["isolation"] = "--isolation"
        flags.update(allow_dirty="--allow-dirty", accept_red_baseline="--accept-red-baseline", no_plan="--no-plan")
    else:
        flags["fix"] = "--fix"   # review works in place on the current (possibly dirty) tree
    ignored = sorted(k for k in a if k not in (*valued, *flags, "mode", "task") and a.get(k) not in (None, False))
    for key, flag in valued.items():
        if a.get(key) is not None:
            args += [flag, str(a[key])]
    for key, flag in flags.items():
        if a.get(key):
            args.append(flag)
    info = runs.spawn_detached(repo, args, task)
    if ignored:
        info["ignored_for_mode"] = ignored
    text = json.dumps(info, ensure_ascii=False)
    if info.get("status") == "started":
        text = (f"started run {info['run_id']} (pid {info['pid']}). Poll with rustflow_wait; do not edit this "
                "checkout meanwhile.")
    return text, info


def t_wait(a: dict) -> tuple[str, dict]:
    repo = _repo()
    lines: list[str] = []
    s = runs.wait(runs.find_run(repo, a.get("run")), min(float(a.get("timeout_s") or 600), float(MAX_WAIT_S)),
                  out=lines.append, channel="mcp")
    s["progress"] = lines[-40:]
    return ("\n".join(lines[-40:]) + "\n\n" if lines else "") + runs.brief_markdown(s, mcp=True), s


def t_status(a: dict) -> tuple[str, dict]:
    s = runs.summary(runs.load_state(runs.find_run(_repo(), a.get("run"))))
    return runs.brief_markdown(s, mcp=True), s


def t_report(a: dict) -> tuple[str, dict]:
    run_dir = runs.find_run(_repo(), a.get("run"))
    state = runs.load_state(run_dir)
    fmt = a.get("format", "brief")
    s = runs.summary(state, findings_limit=1000 if fmt == "json" else runs.MAX_BRIEF_FINDINGS)
    if fmt == "json":
        return _json_report(s, state.get("findings", []))
    if fmt == "full":
        rpt = run_dir / "report.md"
        text = rpt.read_text(encoding="utf-8") if rpt.is_file() else "(no report yet)"
        if len(text) > MAX_TEXT_CHARS:
            text = (text[:MAX_TEXT_CHARS - 300] + f"\n\n... (truncated: the report has {len(text)} characters; "
                    f"read {rpt} or run `rustflow report --full` for all of it)")
        return text, {"run_id": s["run_id"], "status": s["status"], "report": text}
    return runs.brief_markdown(s, mcp=True), s


def _compact(f: dict) -> dict:
    """A finding without the bulk (duplicate descriptions, long prose) - what a driver needs to decide."""
    out = {k: f.get(k) for k in ("id", "severity", "category", "status", "confidence", "resolution_status")}
    out["where"] = f"{f['file']}:{f['line']}" if f.get("file") else ""
    out["reviewers"] = f.get("reviewers", [])
    for k, n in (("title", 200), ("trigger", 400), ("impact", 400), ("fix", 400), ("resolution", 300)):
        out[k] = str(f.get(k) or "")[:n]
    out["also_count"] = len(f.get("also") or [])
    return out


def _json_report(s: dict, findings: list[dict]) -> tuple[str, dict]:
    data = {**s, "findings": [_compact(f) for f in findings], "findings_total": len(findings),
            "findings_truncated": False}
    text = json.dumps(data, ensure_ascii=False)
    while len(text) > MAX_TEXT_CHARS and data["findings"]:   # findings come most important first
        keep = max(0, int(len(data["findings"]) * MAX_TEXT_CHARS / len(text)) - 1)
        data["findings"] = data["findings"][:keep]
        data["findings_truncated"] = True
        data["note"] = (f"truncated: {len(data['findings'])} of {len(findings)} findings shown; all of them: "
                        "`rustflow report --json` (CLI) or the run's state.json")
        text = json.dumps(data, ensure_ascii=False)
    return text, data


def t_cancel(a: dict) -> tuple[str, dict]:
    repo = _repo()
    res = runs.cancel(repo, runs.find_run(repo, a.get("run")))
    return json.dumps(res), res


def t_resume(a: dict) -> tuple[str, dict]:
    repo = _repo()
    run_dir = runs.find_run(repo, a.get("run"))
    more = ["--extra-rounds", str(int(a["extra_rounds"]))] if a.get("extra_rounds") else []
    for key in LIMIT_PROPERTIES:
        if a.get(key) is not None:
            more += ["--" + key.replace("_", "-"), str(a[key])]
    info = runs.spawn_detached(repo, ["resume", run_dir.name, *more], None, run_id=run_dir.name)
    return json.dumps(info), info


def t_gates(a: dict) -> tuple[str, dict]:
    repo = _repo()
    res = gates.run_gates(repo, a.get("profile", "full"), strict=bool(a.get("check_only", True)))
    data = {"ok": res.ok, "summary": res.summary_line(), "feedback": "" if res.ok else res.feedback()}
    return res.summary_line() + ("" if res.ok else "\n\n" + res.feedback(max_chars=6000)), data


HANDLERS: dict[str, Callable[[dict], tuple[str, dict]]] = {
    "rustflow_triage": t_triage, "rustflow_start": t_start, "rustflow_wait": t_wait,
    "rustflow_status": t_status, "rustflow_report": t_report, "rustflow_cancel": t_cancel,
    "rustflow_resume": t_resume, "rustflow_gates": t_gates,
}


# ── JSON-RPC over stdio ──────────────────────────────────────────────────────
class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self.stdout.write(data)
            self.stdout.flush()

    def reply(self, mid: Any, result: dict | None = None, error: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self.send(msg)

    def call_tool(self, mid: Any, params: dict) -> None:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            self.reply(mid, error={"code": -32602, "message": f"unknown tool {name}"})
            return
        try:
            text, data = handler(args)
            self.reply(mid, {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False})
        except Exception as exc:  # tool errors are results, not protocol errors
            print(traceback.format_exc(), file=sys.stderr)
            self.reply(mid, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True})

    def handle(self, msg: dict) -> None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:
            return  # a response to something we never send
        if mid is None:
            return  # notification (initialized, cancelled, ...)
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.reply(mid, {"protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "rustflow", "version": __version__},
                             "instructions": INSTRUCTIONS})
        elif method == "tools/list":
            self.reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            t = threading.Thread(target=self.call_tool, args=(mid, params), daemon=True)
            self.threads.append(t)
            t.start()
        elif method == "ping":
            self.reply(mid, {})
        else:
            self.reply(mid, error={"code": -32601, "message": f"method not found: {method}"})

    def serve(self, join_timeout: float | None = 5.0) -> int:
        """Until stdin closes. On EOF the client is gone: in-flight calls get `join_timeout` to finish."""
        for raw in self.stdin:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                self.handle(m)
        for t in self.threads:
            t.join(timeout=join_timeout)
        return 0


def main() -> int:
    return Server().serve()
