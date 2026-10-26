"""Agent runners for Claude Code (`claude -p`) and Codex (`codex exec`).

Prompts always go through stdin (Windows command lines are limited to 8-32 KB and cmd.exe mangles
quotes). Structured output: Claude `--json-schema` -> `structured_output`; Codex `--output-schema`
+ `-o last_message`. Both are re-validated locally.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from rustflow import procutil, schemas
from rustflow.config import AgentSpec, Config, gates_command

IS_WINDOWS = os.name == "nt"

ROLE_SYSTEM = {
    "planner": "You are the PLANNER in an automated Rust workflow. You do not edit files.",
    "implementer": "You are the IMPLEMENTER in an automated Rust workflow orchestrated by rustflow.",
    "reviewer_a": "You are an independent, read-only Rust REVIEWER in an automated workflow. Never edit files.",
    "reviewer_b": "You are an independent, read-only Rust REVIEWER in an automated workflow. Never edit files.",
    "auditor": "You are a read-only Rust SOUNDNESS AUDITOR (unsafe/FFI/concurrency). Never edit files.",
    "fixer": "You are the FIXER in an automated Rust workflow orchestrated by rustflow.",
    "triage": "You classify task difficulty. Never edit files.",
    "tob_audit": "You run a Rust soundness/security audit plugin non-interactively. Never edit source files.",
}


@dataclass
class AgentCall:
    role: str
    name: str                 # unique per run, used for log file names (e.g. "r2-reviewer_b")
    spec: AgentSpec
    prompt: str
    write: bool               # may the agent modify the working tree?
    schema: dict | None
    cwd: Path
    log_dir: Path
    skill: str | None = None
    timeout_s: float = 3600
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] | None = None   # overrides the provider's write/read tool set
    extra_skills: list[Path] = field(default_factory=list)  # always-on skills (SKILL.md paths)


@dataclass
class AgentResult:
    name: str
    role: str
    spec: str
    ok: bool
    data: Any = None
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens: dict = field(default_factory=dict)
    duration_s: float = 0.0
    session_id: str = ""
    argv: list[str] = field(default_factory=list)
    retryable: bool = True            # False: retrying cannot help (auth, unknown model, bad CLI flag)
    observed_model: str = ""          # model that did most of the work, as reported by the CLI
    model_mismatch: str = ""          # non-empty when it is not the requested model
    permission_denials: list[str] = field(default_factory=list)  # tool calls denied without a prompt
    num_turns: int = 0
    cost_estimated: bool = False      # cost_usd is the role's budget cap, not a reported cost (timeout)
    json_repair: bool = False         # structured output came from a follow-up turn in the same session

    def to_dict(self) -> dict:
        return asdict(self)


def with_rtk_twins(tools: list[str], enabled: bool = True) -> list[str]:
    """Output optimizers such as rtk rewrite Bash commands in a PreToolUse hook (`git status` ->
    `rtk git status`) and, for rules they cannot see (CLI --allowedTools), leave the permission check
    to Claude Code - which then checks the REWRITTEN command. Allow the `rtk`-prefixed form too."""
    out = list(tools)
    if enabled:
        for t in tools:
            if t.startswith("Bash(") and not t.startswith("Bash(rtk "):
                out.append("Bash(rtk " + t[5:])
    return list(dict.fromkeys(out))


def builtin_tools(tools: list[str], windows: bool | None = None) -> list[str]:
    """Built-in tool set for `--tools`: PowerShell is the shell tool on Windows."""
    out = list(tools)
    if (IS_WINDOWS if windows is None else windows) and "Bash" in out and "PowerShell" not in out:
        out.append("PowerShell")
    return out


def denial_summary(items: Any) -> list[str]:
    """`permission_denials` of a `claude -p` result as `Tool(command or path)` strings."""
    out = []
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict):
            continue
        inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        detail = str(inp.get("command") or inp.get("file_path") or inp.get("pattern") or "").strip()
        name = str(d.get("tool_name") or "?")
        out.append(f"{name}({detail[:120]})" if detail else name)
    return out[:50]


JSON_REPAIR_PROMPT = ("Your last answer did not produce the required structured output. Do not use tools and do not "
                      "change files: reply now with ONLY the final JSON object described by the schema, for the "
                      "work you already did in this session.")

ORCHESTRATED_NOTE = (" You were launched by the rustflow orchestrator: this prompt and its skill take precedence "
                     "over interactive-session rules in CLAUDE.md/AGENTS.md (e.g. 'use /rustflow-pr-review').")


def skill_exists(cwd: Path, provider: str, skill: str) -> Path | None:
    base = ".claude/skills" if provider == "claude" else ".agents/skills"
    path = cwd / base / skill / "SKILL.md"
    return path if path.is_file() else None


def _skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def compose_prompt(call: AgentCall, invocation: str, extras_inline: bool = False) -> str:
    """Attach the role skill natively (/skill or $skill) or inline, with a path hint as backup.
    extras_inline: also put the always-on extra skills at the top (Codex, or Claude in inline mode)."""
    if extras_inline and call.extra_skills:
        from rustflow import skills as sk
        inner = AgentCall(**{**call.__dict__, "extra_skills": []})
        return (sk.bundle(call.extra_skills, call.cwd) + "\n\n" + compose_prompt(inner, invocation))
    if not call.skill:
        return call.prompt
    path = skill_exists(call.cwd, call.spec.provider, call.skill)
    if path is None:
        return call.prompt
    rel = path.relative_to(call.cwd).as_posix()
    if invocation == "inline":
        return f"<skill name=\"{call.skill}\">\n{_skill_body(path)}\n</skill>\n\n{call.prompt}"
    head = f"/{call.skill}" if call.spec.provider == "claude" else f"${call.skill}"
    return (f"{head}\n\nFollow the `{call.skill}` skill ({rel}); read it first if it is not loaded.\n\n"
            f"{call.prompt}")


# Failures a retry cannot fix: credentials, model access/names, CLI flags the installed version lacks.
_PERMANENT = re.compile(
    r"invalid api key|authenticat|not logged in|please run /login|unauthori[sz]ed|forbidden|"
    r"model[^\n]{0,80}(not found|not available|does not exist|not supported|unsupported|invalid)|"
    r"(unknown|invalid|unsupported) model|not_found_error|permission_error|"
    r"unknown option|unknown argument|unexpected argument|unrecognized (option|argument|arguments)|"
    r"error: (unknown|invalid) (option|flag|value)", re.I)


def classify_permanent(text: str, http_status: int | None = None) -> bool:
    if http_status in (400, 401, 403, 404):
        return True
    return bool(_PERMANENT.search(text or ""))


def observed_claude_model(payload: dict) -> str:
    """`modelUsage` lists every model the session used (small helper models included): take the one
    that produced the most output."""
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return ""
    def out_tokens(item: tuple[str, Any]) -> int:
        v = item[1] if isinstance(item[1], dict) else {}
        return int(v.get("outputTokens") or 0)
    name, data = max(usage.items(), key=out_tokens)
    return str((data or {}).get("canonicalModel") or name) if isinstance(data, dict) else str(name)


def model_matches(requested: str, observed: str) -> bool:
    """`opus` matches `claude-opus-5-5`; a full id must match as a prefix (date suffixes allowed)."""
    r, o = requested.lower(), observed.lower()
    if not o:
        return True  # unknown -> nothing to compare
    return o.startswith(r) or r.startswith(o) or bool(re.search(rf"(^|-){re.escape(r)}(-|$)", o))


class BaseRunner:
    provider = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pcfg = cfg.section(f"providers.{self.provider}")

    def binary(self) -> list[str]:
        return procutil.resolve_command(self.pcfg.get("bin", self.provider))

    def _write(self, call: AgentCall, suffix: str, content: str) -> Path:
        call.log_dir.mkdir(parents=True, exist_ok=True)
        path = call.log_dir / f"{call.name}.{suffix}"
        path.write_text(content, encoding="utf-8")
        return path

    def run(self, call: AgentCall) -> AgentResult:
        raise NotImplementedError

    def _finish(self, call: AgentCall, res: AgentResult) -> AgentResult:
        if res.ok and call.schema is not None:
            try:
                raw = res.data if res.data is not None else schemas.extract_json(res.text)
                res.data = schemas.validate(raw, call.schema)
            except schemas.SchemaError as exc:
                res.ok = False
                res.error = f"invalid structured output: {exc}"
        self._write(call, "result.json", json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return res


class ClaudeRunner(BaseRunner):
    provider = "claude"

    def build_argv(self, call: AgentCall) -> list[str]:
        p = self.pcfg
        argv = [*self.binary(), "-p", "--output-format", "json",
                "--model", call.spec.model, "--effort", call.spec.effort,
                "--permission-prompts", "none"]
        system = ROLE_SYSTEM.get(call.role, "") + ORCHESTRATED_NOTE
        if call.extra_skills and p.get("extra_skills_via", "system") == "system":
            # One file per role: identical across calls -> stays in the prompt cache; no argv length limit.
            from rustflow import skills as sk
            text = system + "\n\n# Always-on project skills (follow them)\n\n" + sk.bundle(call.extra_skills, call.cwd)
            f = call.log_dir / "skills" / f"{call.role}.system.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            if not f.is_file() or f.read_text(encoding="utf-8") != text:
                f.write_text(text, encoding="utf-8")
            argv += ["--append-system-prompt-file", str(f)]
        else:
            argv += ["--append-system-prompt", system]
        if call.write:
            gates_cmd = gates_command(self.cfg)
            tools = list(call.allowed_tools if call.allowed_tools is not None else p.get("allowed_tools_write", []))
            tools += [f"Bash({gates_cmd} *)", f"PowerShell({gates_cmd} *)", "PowerShell(cargo *)"]
            argv += ["--permission-mode", p.get("permission_mode_write", "acceptEdits"),
                     "--allowedTools", ",".join(with_rtk_twins(tools, p.get("rtk_compat", True)))]
        else:
            argv += ["--permission-mode", p.get("permission_mode_read", "dontAsk"),
                     "--allowedTools", ",".join(with_rtk_twins(list(p.get("allowed_tools_read", [])),
                                                               p.get("rtk_compat", True)))]
            if p.get("disallowed_tools_read"):
                argv += ["--disallowedTools", ",".join(p["disallowed_tools_read"])]
        if call.allowed_tools is None:
            # --allowedTools only pre-approves; --tools takes every other built-in tool definition out of the
            # prompt (measured on 2.1.308: 27 -> 5 tools, ~4x fewer input tokens on a cold start). The role
            # skill still loads: `/skill` in the prompt expands without the Skill tool.
            builtin = p.get("tools_write" if call.write else "tools_read", [])
            if builtin:
                argv += ["--tools", ",".join(builtin_tools(builtin))]
        if p.get("fallback_model"):
            argv += ["--fallback-model", str(p["fallback_model"]).replace(" ", "")]  # overloaded -> fallback
        if call.schema is not None:
            argv += ["--json-schema", json.dumps(call.schema, separators=(",", ":"))]
        if call.max_budget_usd:
            argv += ["--max-budget-usd", f"{call.max_budget_usd:.2f}"]
        sources = str(p.get("setting_sources", "") or "").replace(" ", "")
        if sources:
            argv += ["--setting-sources", sources]  # e.g. "project,local": no user hooks / ~/.claude/CLAUDE.md
        if p.get("strict_mcp", True):
            argv.append("--strict-mcp-config")  # no project MCP servers (incl. rustflow itself) in agents
        if p.get("exclude_dynamic_system_prompt", True):
            argv.append("--exclude-dynamic-system-prompt-sections")  # stable, cache-friendly system prompt
        argv += [str(a) for a in p.get("extra_args", [])]
        return argv

    def run(self, call: AgentCall) -> AgentResult:
        argv = self.build_argv(call)
        prompt = compose_prompt(call, self.pcfg.get("skill_invocation", "slash"),
                                extras_inline=self.pcfg.get("extra_skills_via", "system") == "inline")
        res = self._attempt(call, argv, prompt)
        if (call.schema is not None and not res.ok and res.session_id and "structured" in res.error
                and self.pcfg.get("repair_structured_output", True)):
            # One more turn in the SAME session with the SAME argv (tools, system prompt, schema): the
            # cached prefix holds, and a long write session is not redone just to get its JSON report.
            rep = AgentCall(**{**call.__dict__, "name": f"{call.name}-json", "timeout_s": min(call.timeout_s, 600)})
            fixed = self._attempt(rep, argv + ["--resume", res.session_id], JSON_REPAIR_PROMPT)
            fixed.name, fixed.json_repair = call.name, True
            fixed.cost_usd = round(res.cost_usd + fixed.cost_usd, 6)
            fixed.duration_s += res.duration_s
            fixed.permission_denials = res.permission_denials + fixed.permission_denials
            fixed.num_turns += res.num_turns
            for k, v in res.tokens.items():
                if isinstance(v, (int, float)):
                    fixed.tokens[k] = fixed.tokens.get(k, 0) + v
            if not fixed.ok:
                fixed.error = f"{res.error} | JSON repair failed: {fixed.error}"
            self._write(call, "result.json", json.dumps(fixed.to_dict(), indent=2, ensure_ascii=False))
            res = fixed
        return res

    def _attempt(self, call: AgentCall, argv: list[str], prompt: str) -> AgentResult:
        self._write(call, "prompt.md", prompt)
        self._write(call, "argv.json", json.dumps(argv, indent=2))
        env = {"RUSTFLOW_ROLE": call.role, **call.env}
        proc = procutil.run(argv, cwd=call.cwd, stdin_text=prompt, env=env, timeout_s=call.timeout_s,
                            isolate_session=True)
        self._write(call, "stdout.json", proc.stdout)
        if proc.stderr.strip():
            self._write(call, "stderr.log", proc.stderr)
        res = AgentResult(call.name, call.role, str(call.spec), ok=False, duration_s=proc.duration_s, argv=argv)
        if proc.timed_out:
            res.error = f"timeout after {call.timeout_s:.0f}s"
            if call.max_budget_usd:
                # A killed `claude -p` prints nothing, and even an interrupted one reports $0: book the
                # role's cap so [budget].max_total_usd stays an upper bound instead of silently undercounting.
                res.cost_usd, res.cost_estimated = float(call.max_budget_usd), True
            return self._finish(call, res)
        payload = _last_json_object(proc.stdout)
        if payload is None:
            res.error = (proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}")[-2000:]
            res.retryable = not classify_permanent(res.error)
            return self._finish(call, res)
        res.observed_model = observed_claude_model(payload)
        if not model_matches(call.spec.model, res.observed_model):
            res.model_mismatch = f"requested {call.spec.model}, CLI reports {res.observed_model}"
        res.text = str(payload.get("result") or "")
        res.cost_usd = float(payload.get("total_cost_usd") or 0.0)
        res.session_id = str(payload.get("session_id") or "")
        res.tokens = payload.get("usage") or {}
        res.data = payload.get("structured_output")
        res.permission_denials = denial_summary(payload.get("permission_denials"))
        res.num_turns = int(payload.get("num_turns") or 0)
        subtype = payload.get("subtype", "success")
        if payload.get("is_error") or subtype != "success" or proc.returncode != 0:
            detail = "; ".join(str(e) for e in payload.get("errors") or []) or res.text[-1500:]
            res.error = f"claude reported {subtype}: {detail}"
            status = payload.get("api_error_status")
            res.retryable = not classify_permanent(detail, status if isinstance(status, int) else None)
            # Budget/turn limits still leave useful work in the tree for write roles.
            return self._finish(call, res)
        res.ok = True
        return self._finish(call, res)


class CodexRunner(BaseRunner):
    provider = "codex"

    def build_argv(self, call: AgentCall, last_msg: Path, schema_path: Path | None) -> list[str]:
        p = self.pcfg
        argv = [*self.binary(), "exec", "-m", call.spec.model,
                "-c", f'model_reasoning_effort="{call.spec.effort}"',
                "-s", "workspace-write" if call.write else "read-only",
                "-C", str(call.cwd), "--json", "--color", "never", "-o", str(last_msg)]
        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]
        if call.write:
            if p.get("network"):
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
            dirs = [str(Path(d).expanduser()) for d in (p.get("add_dirs", []) or [])]
            if call.env.get("CARGO_TARGET_DIR"):  # worktree mode: shared target/ lives outside -C
                dirs.append(call.env["CARGO_TARGET_DIR"])
            for d in dict.fromkeys(dirs):
                argv += ["--add-dir", d]
        argv += [str(a) for a in p.get("extra_args", [])]
        argv.append("-")  # prompt from stdin
        return argv

    def run(self, call: AgentCall) -> AgentResult:
        last_msg = call.log_dir / f"{call.name}.last.txt"
        schema_path = None
        if call.schema is not None:
            schema_path = self._write(call, "schema.json", json.dumps(call.schema, indent=2))
        argv = self.build_argv(call, last_msg, schema_path)
        prompt = compose_prompt(call, self.pcfg.get("skill_invocation", "mention"), extras_inline=True)
        self._write(call, "prompt.md", prompt)
        self._write(call, "argv.json", json.dumps(argv, indent=2))
        env = {"RUSTFLOW_ROLE": call.role, **call.env}
        proc = procutil.run(argv, cwd=call.cwd, stdin_text=prompt, env=env, timeout_s=call.timeout_s,
                            isolate_session=True)
        self._write(call, "events.jsonl", proc.stdout)
        if proc.stderr.strip():
            self._write(call, "stderr.log", proc.stderr)
        res = AgentResult(call.name, call.role, str(call.spec), ok=False, duration_s=proc.duration_s, argv=argv)
        tokens: dict[str, int] = {}
        errors: list[str] = []
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type", "")
            if etype == "thread.started":
                res.session_id = str(ev.get("thread_id", ""))
            elif etype == "turn.completed":
                for k, v in (ev.get("usage") or {}).items():
                    if isinstance(v, int):
                        tokens[k] = tokens.get(k, 0) + v
            elif etype in ("error", "turn.failed"):
                errors.append(json.dumps(ev.get("error") or ev.get("message") or ev)[:500])
            item = ev.get("item")
            model = ev.get("model") or (item.get("model") if isinstance(item, dict) else None)
            if isinstance(model, str) and model:
                res.observed_model = model
        if res.observed_model and not model_matches(call.spec.model, res.observed_model):
            res.model_mismatch = f"requested {call.spec.model}, CLI reports {res.observed_model}"
        res.tokens = tokens
        if proc.timed_out:
            res.error = f"timeout after {call.timeout_s:.0f}s"
            return self._finish(call, res)
        res.text = last_msg.read_text(encoding="utf-8", errors="replace") if last_msg.is_file() else ""
        if proc.returncode != 0 or (errors and not res.text):
            res.error = ("; ".join(errors) or proc.stderr.strip() or f"exit code {proc.returncode}")[-2000:]
            res.retryable = not classify_permanent(res.error + "\n" + proc.stderr)
            return self._finish(call, res)
        res.ok = True
        return self._finish(call, res)


class DryRunner(BaseRunner):
    """Writes prompts/argv, never calls a model. Returns minimal valid outputs."""

    def __init__(self, cfg: Config, real: dict[str, BaseRunner]):
        super().__init__(cfg)
        self.real = real

    def run(self, call: AgentCall) -> AgentResult:
        runner = self.real[call.spec.provider]
        try:
            if isinstance(runner, CodexRunner):
                argv = runner.build_argv(call, call.log_dir / f"{call.name}.last.txt",
                                         call.log_dir / f"{call.name}.schema.json")
            else:
                argv = runner.build_argv(call)  # type: ignore[attr-defined]
        except procutil.CommandNotFound as exc:
            argv = [f"<{exc}>"]
        inv = runner.pcfg.get("skill_invocation", "slash")
        inline = isinstance(runner, CodexRunner) or runner.pcfg.get("extra_skills_via", "system") == "inline"
        self._write(call, "prompt.md", compose_prompt(call, inv, extras_inline=inline))
        self._write(call, "argv.json", json.dumps(argv, indent=2))
        data = _dry_output(call.schema)
        return AgentResult(call.name, call.role, str(call.spec), ok=True, data=data, text="(dry run)", argv=argv)


def _dry_output(schema: dict | None) -> Any:
    if schema is None:
        return None
    if schema is schemas.REVIEW_SCHEMA:
        return {"summary": "dry run", "verdict": "APPROVE", "findings": [], "disputes": [],
                "acceptance": [], "residual_risks": []}
    if schema is schemas.FIX_SCHEMA:
        return {"summary": "dry run", "resolutions": [], "tests_added": []}
    if schema is schemas.PLAN_SCHEMA:
        return {"summary": "dry run", "difficulty": "hard", "steps": [], "acceptance_criteria": [],
                "files_to_touch": [], "risks": [], "open_questions": []}
    if schema is schemas.IMPLEMENT_SCHEMA:
        return {"summary": "dry run", "files_changed": [], "tests_added": [], "gates_green": True, "notes": ""}
    return {"difficulty": "medium", "rationale": "dry run", "risk_areas": []}


def _last_json_object(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):  # some versions emit an array of messages
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def make_runners(cfg: Config, dry_run: bool = False) -> dict[str, BaseRunner]:
    real: dict[str, BaseRunner] = {"claude": ClaudeRunner(cfg), "codex": CodexRunner(cfg)}
    if dry_run:
        dry = DryRunner(cfg, real)
        return {"claude": dry, "codex": dry}
    return real


def run_with_retry(runner: BaseRunner, call: AgentCall, retries: int = 1,
                   may_retry: Callable[[], bool] | None = None) -> AgentResult:
    """Retry once on infrastructure failures (no output, crash, invalid structured output); never on
    timeouts, budget stops or permanent errors (auth, unknown model, CLI flag the installed version lacks).
    may_retry: extra condition checked after a failure (write roles: only if the tree is unchanged)."""
    res = runner.run(call)
    attempt = 0
    while (not res.ok and attempt < retries and res.retryable and "timeout" not in res.error
           and "max_budget" not in res.error and "error_max_budget" not in res.error
           and "error_max_turns" not in res.error and (may_retry is None or may_retry())):
        attempt += 1
        time.sleep(5 * attempt)
        call.name = f"{call.name}-retry{attempt}"
        res = runner.run(call)
    return res
