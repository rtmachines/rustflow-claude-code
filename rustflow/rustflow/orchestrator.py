"""The rustflow state machine.

preflight -> triage -> plan? -> implement -> gates -> review(1) -> [fix -> gates -> review(n)]* -> final

Every phase transition is persisted in <run>/state.json so `rustflow resume` can continue.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
import threading
from collections import Counter
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rustflow import criteria as crit_mod
from rustflow import findings as fnd
from rustflow import gates, gitutil, integrity, procutil, prompts, report, runs, schemas, triage, usage
from rustflow.agents import AgentCall, AgentResult, make_runners, run_with_retry
from rustflow.config import AgentSpec, Config, bump, gates_command, max_difficulty
from rustflow.budget import BudgetManager, BudgetExceeded, DEFAULT_LIMITS, validate_limits
from rustflow.process import execution_scope

RUNS_DIR = Path(".rustflow") / "runs"
RUNTIME_GITIGNORE = "runs/\ncache/\nwt/\n__pycache__/\n"
RESUMABLE = ("interrupted", "budget", "crashed", "tree_changed", "agent_failed", "incomplete")
# Finished, but another fix round can still help: `rustflow resume <run> --extra-rounds N`.
CONTINUABLE = ("max_rounds", "stalled", "final_gates_red", "criteria_unmet", "gate_changes_pending")


@dataclass
class Options:
    mode: str = "run"                 # run | review
    difficulty: str | None = None
    base: str | None = None
    isolation: str | None = None
    allow_dirty: bool = False
    accept_red_baseline: bool = False
    max_rounds: int | None = None
    dry_run: bool = False
    no_plan: bool = False
    fix: bool = True                  # review mode: run the fix loop?
    verbose: bool = False
    max_agent_calls: int | None = None
    max_active_seconds: float | None = None
    max_output_bytes: int | None = None
    criteria_file: str | None = None  # TOML with [[criteria]] text/test (mechanical acceptance criteria)


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())[:32].strip("-")
    return s or "task"


def _file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


class Abort(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Orchestrator:
    def __init__(self, repo: Path, cfg: Config, opts: Options, out=None):
        self.repo = repo.resolve()
        self.cfg = cfg
        self.opts = opts
        self.out = out or sys.stdout
        self.runners = make_runners(cfg, dry_run=opts.dry_run)
        prompts.GATES = gates_command(cfg)
        self.state: dict[str, Any] = {}
        self.run_dir: Path = Path()
        self._state_lock = threading.RLock()
        self._attempts: dict[str, dict] = {}
        self.budget = None

    # ── logging / state ──────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line, file=self.out, flush=True)
        try:
            with (self.run_dir / "rustflow.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def event(self, kind: str, **data: Any) -> None:
        with self._state_lock:
            self.state.setdefault("events", []).append({"at": _now(), "kind": kind, **data})

    def save(self) -> None:
        with self._state_lock:
            if self.budget:
                self.budget.checkpoint()
            tmp = self.run_dir / "state.json.tmp"
            tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.run_dir / "state.json")
            runs.update_lock(self.repo, self.state)

    def _prepare_usage(self, *, resume: bool = False) -> None:
        with self._state_lock:
            self.state.setdefault("usage_baseline", {"cost_usd": self.state.get("cost_usd", 0.0),
                                                     "tokens": deepcopy(self.state.get("tokens", {})),
                                                     "history_complete": False})
            self._attempts = {r["attempt_id"]: r for r in usage.load_attempts(self.run_dir / "attempts",
                                                                           interrupt_started=resume)}
            self._refresh_usage()

    def _prepare_budget(self, *, resume=False):
        limits = {k: self.cfg.get(("process." if k == "max_output_bytes" else "budget.") + k, v)
                  for k, v in DEFAULT_LIMITS.items()}
        changes = {k: getattr(self.opts, k) for k in DEFAULT_LIMITS if getattr(self.opts, k) is not None}
        # Validate decreases before changing any persisted session state.
        validate_limits(changes)
        saved = self.state.get("execution_budget", {}).get("limits", {})
        for key, value in changes.items():
            if resume and key in saved and value < saved[key]:
                raise ValueError(f"cannot lower {key} on resume ({value} < {saved[key]})")
        if not resume:
            limits.update(changes)
        self.budget = BudgetManager(self.state, limits, attempts=self._attempts.values(),
                                    history_complete=not resume,
                                    lock=self._state_lock, persist=self.save, event=self.event)
        if resume:
            self.budget.raise_limits(changes)

    def _check_budget(self):
        if self.budget:
            self.budget.check()

    def _incomplete_abort(self, reason, **metadata):
        self.state["termination_reason"] = reason
        self.state["output_complete"] = False
        self.event("incomplete_output", reason=reason, **metadata)
        self.save()
        status = "budget" if reason in ("agent_call_limit", "active_time_limit") else "incomplete"
        return Abort(status, f"incomplete execution: {reason}; inspect diagnostics, raise the applicable limit and resume")

    def _incomplete(self, reason, **metadata):
        raise self._incomplete_abort(reason, **metadata)

    def _refresh_usage(self) -> None:
        """Recompute from the ledger, making replay after a crash/resume idempotent. Lock held by caller."""
        baseline = self.state["usage_baseline"]
        rows = list(self._attempts.values())
        self.state["usage_summary"] = usage.summarize(rows, history_complete=baseline["history_complete"])
        if not baseline["history_complete"]:
            self.state["usage_summary"]["legacy_baseline"] = deepcopy(baseline)
        self.state["cost_usd"] = round(baseline["cost_usd"] + sum(
            (r.get("measured_cost_usd") or 0) + (r.get("estimated_cost_usd") or 0)
            for r in rows if r["provider"] == "claude"), 4)
        tokens = deepcopy(baseline["tokens"])
        for row in rows:
            for key, value in usage.raw_tokens(row["raw_usage"]).items():
                tokens[key] = tokens.get(key, 0) + value
        self.state["tokens"] = tokens

    def _record_attempt(self, record: dict) -> None:
        with self._state_lock:
            self._attempts[record["attempt_id"]] = deepcopy(record)
            self._refresh_usage()
            self.save()

    @property
    def workdir(self) -> Path:
        return Path(self.state["workdir"])

    @property
    def language(self) -> str:
        return str(self.cfg.get("project.language", "pl"))

    # ── entry points ─────────────────────────────────────────────────────
    def start(self, task: str, run_id: str | None = None) -> int:
        criteria: list[crit_mod.Criterion] = []
        if self.opts.criteria_file:
            try:
                criteria = crit_mod.load_file(Path(self.opts.criteria_file))
            except crit_mod.CriteriaError as exc:
                print(f"error: {exc}", file=self.out)
                return 2
        run_id = run_id or (_dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _slug(task))
        self.run_dir = self.repo / RUNS_DIR / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_runtime_ignore()
        self.state = {
            "run_id": run_id, "created": _now(), "task": task, "mode": self.opts.mode,
            "repo": str(self.repo), "workdir": str(self.repo), "phase": "preflight", "round": 0,
            "gate_fix_attempts": 0, "findings": [], "gate_changes": [], "id_counter": 0, "rounds": [], "agents": [],
            "cost_usd": 0.0, "tokens": {}, "status": "running", "dry_run": self.opts.dry_run,
            "options": self.opts.__dict__.copy(), "pid": os.getpid(),
            "criteria": [c.to_dict() for c in criteria], "mechanical": [], "seen_trees": [],
            "usage_baseline": {"cost_usd": 0.0, "tokens": {}, "history_complete": True},
        }
        try:
            runs.acquire(self.repo, self.state)
        except runs.RunLocked as exc:
            print(f"error: {exc}", file=self.out)
            return 2
        (self.run_dir / "task.md").write_text(task, encoding="utf-8")
        (self.repo / RUNS_DIR / "LATEST").write_text(run_id, encoding="utf-8")
        self._prepare_usage()
        self._prepare_budget()
        self.save()
        self.log(f"run {run_id}  ({self.run_dir.relative_to(self.repo).as_posix()})")
        return self.loop()

    def resume(self, run_dir: Path, extra_rounds: int = 0) -> int:
        self.run_dir = run_dir
        self.state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        saved = self.state.get("options", {})
        for k, v in saved.items():
            if hasattr(self.opts, k) and k not in ("dry_run", "verbose", *DEFAULT_LIMITS):
                setattr(self.opts, k, v)
        status = self.state.get("status")
        if extra_rounds:
            if status not in CONTINUABLE and not (status in runs.APPROVED and runs.degraded(self.state)):
                self.log(f"--extra-rounds continues a run that ended {'/'.join(CONTINUABLE)}; this one is {status}")
                return 2
        elif status not in ("running", *RESUMABLE):
            self.log(f"run already finished with status {status}")
            return 0
        if self.state.get("dry_run") and not self.opts.dry_run:
            self.log("this run was a --dry-run (no branch was created); start a real run instead")
            return 2
        if self.state.get("isolation") == "branch" and self.state.get("branch"):
            current = gitutil.head_branch(self.repo)
            if current != self.state["branch"]:
                self.log(f"refusing to resume: checked out '{current}', the run works on "
                         f"'{self.state['branch']}'. Run: git switch {self.state['branch']}")
                return 2
        if self.state.get("isolation") == "worktree" and not self.workdir.is_dir():
            if not (self.state.get("worktree_removed") and self.state.get("branch")):
                self.log(f"refusing to resume: worktree {self.workdir} no longer exists")
                return 2
            # Removed by rustflow after the run finished clean: the work is on the branch, check it out again.
            gitutil.checkout_worktree(self.repo, self.workdir, self.state["branch"])
            self.state["worktree_removed"] = False
            self.log(f"worktree re-created from {self.state['branch']}: {self.workdir}")
        try:
            runs.acquire(self.repo, self.state)
        except runs.RunLocked as exc:
            self.log(f"error: {exc}")
            return 2
        try:
            # A failed previous resume may already have re-created this checkout.
            self._prepare_usage(resume=True)
            try:
                self._prepare_budget(resume=True)
            except ValueError as exc:
                self.log(str(exc))
                return 2
            self.state.pop("termination_reason", None)
            self.state.pop("output_complete", None)
            if "gates_cfg" not in self.state and self.state.get("base_sha"):
                try:
                    self._gate_cfg()
                except gates.GateConfigError as exc:
                    self.log(f"refusing to resume: cannot read the gate configuration from the base commit ({exc})")
                    return 2
            if self.state.get("isolation") == "worktree":
                self.state["worktree_removed"] = False
            try:
                self._write_baseline()
            except OSError as exc:
                self.log(f"refusing to resume: could not restore baseline ({exc})")
                return 2
            self.event("resumed", previous_status=status, reason=(self.state.pop("stop_reason", "") or "")[:500],
                       extra_rounds=extra_rounds)
            self.state.pop("failed_agent", None)
            if extra_rounds:
                self._continue(extra_rounds)
            self.state["status"] = "running"
            self.state["pid"] = os.getpid()
            self.save()
            self.log(f"resuming {self.state['run_id']} at phase {self.state['phase']} (round {self.state['round']})")
            return self.loop()
        finally:
            runs.release(self.repo, self.state["run_id"])

    def _continue(self, extra_rounds: int) -> None:
        """Reopen a finished run for more fix rounds on the SAME branch/worktree (checkpoints, run dir and
        finding ids continue) instead of a new `review --fix` run in some other checkout."""
        limit = int(self.state.get("round", 0)) + extra_rounds
        self.state.setdefault("options", {})["max_rounds"] = limit
        self.opts.max_rounds = limit
        if self.state.get("mode") == "review":
            self.state["options"]["fix"] = self.opts.fix = True
        for key in ("outcome", "finished", "final_gates", "final_gates_ok", "final_gates_feedback"):
            self.state.pop(key, None)
        self.state["gate_fix_attempts"] = 0
        open_f = [f for f in self.state.get("findings", []) if f.get("status") == "open"]
        # Gate-change findings are recomputed from the tree: a human may have reverted them by hand, so
        # re-gate (refresh -> review) instead of sending the fixer after a change that may be gone.
        if open_f and not all(f.get("category") == "gate-integrity" for f in open_f):
            self.state["phase"] = "fix"
        else:  # final gates red / required tests missing: gates (+ gate fixer) -> review recomputes findings
            self.state["phase"], self.state["gates_after"] = "gates", "continue"
        self.log(f"continuing with up to {extra_rounds} more round(s) (max_rounds {limit})")

    def loop(self) -> int:
        if self.budget is None:
            self._prepare_usage()
            self._prepare_budget(resume="execution_budget" in self.state)
        _install_interrupt_handlers()
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(1):
                self.save()
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            with execution_scope(max_output_bytes=self.budget.limits["max_output_bytes"],
                                 deadline=self.budget.deadline, cancel=self.budget.cancel):
                return self._loop()
        finally:
            self.budget.cancel.set()
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            try:
                self.budget.close()
                self._write_report()
                if self.state.get("status") not in ("running", *RESUMABLE):
                    self._remove_finished_worktree()
            finally:
                runs.release(self.repo, self.state["run_id"])

    def _remove_finished_worktree(self) -> None:
        """A finished worktree run leaves its work on the branch (checkpoint commits), so the checkout under
        .rustflow/wt/ is only clutter - unless it holds uncommitted work (checkpoint_commits = false)."""
        wt = self.workdir
        if self.state.get("isolation") != "worktree" or self.opts.dry_run or not wt.is_dir() or wt == self.repo:
            return
        try:
            if gitutil.git(wt, "status", "--porcelain", "--untracked-files=all").strip():
                return
            self._archive_worktree_audit()
            gitutil.remove_worktree(self.repo, wt, force=False, check=True)
        except (gitutil.GitError, OSError) as exc:
            self.log(f"worktree kept ({exc})")
            return
        if not wt.is_dir():
            self.state["worktree_removed"] = True
            self.log(f"worktree removed (work is on {self.state.get('branch')}): {wt}")
            self.save()

    def _archive_worktree_audit(self) -> None:
        """Persist complete audit artifacts and their report path before deleting the checkout."""
        source = self.workdir / ".rust-review-results"
        if not source.exists():
            return
        archive = (self.run_dir / "audit" / "results").resolve()
        shutil.copytree(source, archive, dirs_exist_ok=True)
        audit = self.state.get("audit") or {}
        if audit.get("report"):
            rpt = Path(audit["report"])
            if not rpt.is_absolute():
                rpt = self.workdir / rpt
            try:
                relative = rpt.relative_to(source)
            except ValueError:
                pass  # Already archived by a previous cleanup, or stored outside the worktree.
            else:
                archived_report = archive / relative
                if not archived_report.is_file():
                    raise OSError(f"archived audit report missing: {archived_report}")
                audit["report"] = str(archived_report)
        self.save()
        self._write_report()

    def _write_baseline(self) -> None:
        """Restore the Stop hook's per-run accepted failures, without exposing partial JSON."""
        signatures = self.state.get("baseline_signatures")
        if not signatures:
            return
        cache = self.workdir / ".rustflow" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / "baseline.json.tmp"
        try:
            tmp.write_text(json.dumps({"run": self.state["run_id"], "signatures": signatures}), encoding="utf-8")
            os.replace(tmp, cache / "baseline.json")
        finally:
            tmp.unlink(missing_ok=True)

    def _loop(self) -> int:
        try:
            while self.state["phase"] != "done":
                self._check_budget()
                phase = self.state["phase"]
                getattr(self, f"phase_{phase}")()
                self.save()
            self._check_budget()
        except gates.IncompleteGate as exc:
            abort = self._incomplete_abort(exc.step.termination_reason, step=exc.step.name)
            self.state.update(status=abort.status, stop_reason=abort.message)
            self._write_report()
        except BudgetExceeded as exc:
            if self.state["phase"] == "done":
                self.state["phase"] = phase
            self.state.update(status="budget", termination_reason=exc.reason, output_complete=False,
                              stop_reason=f"execution budget exhausted: {exc.reason}")
            self._write_report()
        except Abort as exc:
            self.log(f"STOP: {exc.message}")
            self.state["status"] = exc.status
            self.state["stop_reason"] = exc.message
            if exc.status not in RESUMABLE:      # budget stops keep the phase so `resume` can continue
                self.state["phase"] = "done"
            self._write_report()
        except KeyboardInterrupt:
            if self.budget:
                self.budget.cancel.set()
            procutil.kill_all()
            self.state["status"] = "interrupted"
            self.save()
            self.log(f"interrupted - continue with: rustflow resume {self.state['run_id']}")
            return 130
        except Exception as exc:  # never lose agent work/cost bookkeeping on an unexpected error
            if self.budget:
                self.budget.cancel.set()
            procutil.kill_all()
            self.state["status"] = "crashed"
            self.state["stop_reason"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"
            self.save()
            self.log(f"CRASH in phase {self.state.get('phase')}: {exc} - fix the cause, then: "
                     f"rustflow resume {self.state['run_id']}")
            try:
                self._write_report()
            except Exception:
                pass
            return 3
        self.save()
        status = self.state["status"]
        self.log(f"finished: {status}  report: {(self.run_dir / 'report.md').relative_to(self.repo).as_posix()}")
        return 0 if status in ("approved", "approved_with_advisories") else 2

    def _ensure_runtime_ignore(self) -> None:
        gi = self.repo / ".rustflow" / ".gitignore"
        if not gi.exists():
            gi.parent.mkdir(parents=True, exist_ok=True)
            gi.write_text(RUNTIME_GITIGNORE, encoding="utf-8")

    # ── agent plumbing ───────────────────────────────────────────────────
    def call_agent(self, role: str, name: str, spec: AgentSpec, prompt: str, *, write: bool,
                   schema: dict | None, skill: str | None, retry: bool = True,
                   tools: list[str] | None = None, reservation: str | None = None,
                   defer_limits: bool = False) -> AgentResult:
        self._check_budget()
        budget_total = float(self.cfg.get("budget.max_total_usd", 0) or 0)
        with self._state_lock:
            if "usage_baseline" not in self.state:
                self._prepare_usage()
            if budget_total and self.state["cost_usd"] >= budget_total:
                raise Abort("budget", f"total budget ${budget_total:.2f} exhausted")
        self.cfg.check_spec(spec, f"{role}")
        cfg_role = "auditor" if role in ("auditor", "tob_audit") else role
        call = AgentCall(
            role=role, name=name, spec=spec, prompt=prompt, write=write, schema=schema,
            cwd=self.workdir, log_dir=self.run_dir / "agents", skill=skill,
            timeout_s=float(self.cfg.role_opt(cfg_role, "timeout_min", 30)) * 60,
            max_budget_usd=self.cfg.role_opt(cfg_role, "max_budget_usd") if spec.provider == "claude" else None,
            env=self._agent_env(), allowed_tools=tools, extra_skills=self._extra_skills(role, spec.provider),
            on_attempt=self._record_attempt, budget=self.budget, reservation=reservation,
        )
        self.log(f"> {name}: {spec}{'  [write]' if write else ''}")
        runner = self.runners[spec.provider]
        # A write role is retried only if the failed attempt left the tree as it was (e.g. API overloaded
        # before the first edit); otherwise its partial work goes on to the gates like any other result.
        before = self.snapshot() if write and retry and not self.opts.dry_run else None
        res = run_with_retry(runner, call, retries=1 if retry else 0,
                             may_retry=(lambda: self.snapshot() == before) if before is not None else None)
        if res.model_mismatch:
            self.log(f"WARNING {name}: {res.model_mismatch} (model fallback/reroute?)")
            self.event("model_mismatch", agent=name, detail=res.model_mismatch)
        if res.permission_denials:
            # Denied silently (--permission-prompts none): each one cost the agent a turn.
            self.log(f"WARNING {name}: {len(res.permission_denials)} denied tool call(s), e.g. "
                     + ", ".join(res.permission_denials[:3]))
        if res.cost_estimated:
            self.log(f"WARNING {name}: no cost reported (timeout) - booked the role cap ${res.estimated_cost_usd:.2f}")
        entry = {k: v for k, v in res.to_dict().items() if k not in ("data", "text", "argv", "raw_usage")}
        entry["skills"] = ([skill] if skill else []) + [p.parent.name for p in call.extra_skills]
        with self._state_lock:
            self.state["agents"].append(entry)
            self.save()
        cost = f" ${res.cost_usd:.2f}" if res.cost_usd else ""
        self.log(f"< {name}: {'ok' if res.ok else 'FAILED: ' + res.error[:300]} ({res.duration_s:.0f}s{cost})")
        if not res.output_complete and not defer_limits:
            self._incomplete(res.termination_reason, agent=name)
        return res

    def _agent_failed(self, res: AgentResult, who: str) -> Abort:
        """An agent call failed and left the tree unchanged: resumable, the phase stays as it is."""
        self.state["failed_agent"] = {"name": res.name, "error": res.error[:500], "retryable": res.retryable}
        hint = ("looks transient (overloaded/network) - resume when the API is healthy" if res.retryable else
                "fix the cause first (`rustflow doctor`: CLI login, model names, flags), then resume")
        return Abort("agent_failed", f"{who} failed without changing the tree: {res.error[:500]} - {hint}")

    def _extra_skills(self, role: str, provider: str) -> list[Path]:
        from rustflow import skills as sk
        try:
            return sk.resolve(self.cfg.section("skills"), role, self.workdir, provider)
        except sk.SkillError as exc:
            raise Abort("error", str(exc)) from exc

    def _check_skills(self) -> None:
        """Fail at preflight (not mid-run) if an always-on skill is missing for any role/provider in use."""
        from rustflow import skills as sk
        used: dict[str, set[str]] = {}
        for role in ("planner", "implementer", "reviewer_a", "reviewer_b", "fixer"):
            for d in ("easy", "medium", "hard"):
                used.setdefault(role, set()).add(self.cfg.spec_for(role, d).provider)
        lines = []
        for role, providers in used.items():
            for prov in sorted(providers):
                names = [p.parent.name for p in self._extra_skills(role, prov)]
                if names:
                    lines.append(f"{role}/{prov}: {', '.join(names)}")
        if lines:
            self.log("always-on skills: " + "; ".join(lines))
        self.state["extra_skills"] = lines

    def _agent_env(self) -> dict[str, str]:
        env = {"RUSTFLOW_RUN": self.state["run_id"], "RUSTFLOW_BASE": self.state.get("base_sha", ""),
               # rust-review-kit's Stop/commit gate: rustflow's gates are authoritative under the
               # orchestrator; without this the kit re-runs cargo in every session, read-only
               # reviewers included, and can block a reviewer that cannot fix anything.
               "RUST_GATE_SKIP": "1"}
        if self.state.get("cargo_target_dir"):
            env["CARGO_TARGET_DIR"] = self.state["cargo_target_dir"]
        return env

    def spec(self, role: str, difficulty: str | None = None) -> AgentSpec:
        return self.cfg.spec_for(role, difficulty or self.state["difficulty"])

    def _gate_cfg(self) -> dict:
        """Gate configuration frozen from the base commit (never the work tree the agents edit)."""
        if "gates_cfg" not in self.state:
            self.state["gates_cfg"] = gates.load_gate_config_at(self.repo, self.state["base_sha"])
        cfg = dict(self.state["gates_cfg"])
        cfg["max_output_bytes"] = (self.budget.limits["max_output_bytes"] if self.budget
                                   else self.cfg.get("process.max_output_bytes", 16777216))
        return cfg

    def run_gates(self, label: str, *, strict: bool = False, fix_fmt: bool | None = None,
                  use_cache: bool = True) -> gates.GateResult:
        self._check_budget()
        if self.opts.dry_run:
            self.log(f"gates [{label}]: skipped (dry run)")
            return gates.GateResult("full", True, None)
        self.log(f"gates [{label}] ...")
        overrides = {"RUSTFLOW_BASE": self.state.get("base_sha", "")}
        if self.state.get("cargo_target_dir"):  # worktree: share the main checkout's build cache
            overrides["CARGO_TARGET_DIR"] = self.state["cargo_target_dir"]
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update(overrides)
        gate_cfg = self._gate_cfg()
        try:
            res = gates.run_gates(self.workdir, "full", cfg=gate_cfg, strict=strict, use_cache=use_cache and not strict,
                                  log_dir=self.run_dir / "gates" / label, fix_fmt=fix_fmt)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        (self.run_dir / "gates").mkdir(exist_ok=True)
        (self.run_dir / "gates" / f"{label}.json").write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
        self.log(f"gates [{label}]: {res.summary_line()}")
        incomplete = next((s for s in res.steps if not s.output_complete), None)
        if incomplete:
            self._incomplete(incomplete.termination_reason, gate=label, step=incomplete.name)
        self._check_budget()
        return res

    def snapshot(self) -> str:
        return gitutil.snapshot(self.workdir)

    def checkpoint(self, message: str) -> None:
        if self.state.get("checkpoints") and not self.opts.dry_run:
            sha = gitutil.checkpoint(self.workdir, f"rustflow({self.state['run_id']}): {message}")
            if sha:
                self.event("checkpoint", sha=sha, message=message)

    # ── acceptance criteria / progress helpers ──────────────────────────
    @property
    def criteria(self) -> list[crit_mod.Criterion]:
        return crit_mod.load_state(self.state)

    def _mechanical_criteria(self) -> list[crit_mod.Criterion]:
        return [c for c in self.criteria if c.test]

    def _evaluate_mechanical(self, res: gates.GateResult) -> None:
        """Decide mechanical criteria from this gate run's test output (never from a model)."""
        mech = self._mechanical_criteria()
        if not mech or self.opts.dry_run:
            return
        results = crit_mod.evaluate(mech, res.steps)
        self.state["mechanical"] = results
        bad = crit_mod.unmet(results)
        self.log(f"required tests: {len(results) - len(bad)}/{len(results)} passed"
                 + (" - missing: " + ", ".join(r["test"] for r in bad) if bad else ""))

    def _mechanical_findings(self, rnd: int, prev_open: list[fnd.Finding]) -> list[fnd.Finding]:
        was_open = {f.id for f in prev_open}
        out = []
        for r in crit_mod.unmet(self.state.get("mechanical") or []):
            fid = f"T-{r['id']}"
            out.append(fnd.Finding(
                id=fid, severity="P1", category="acceptance", file="", line=0,
                title=f"Required test `{r['test']}` ({r['id']}): {r['detail']}", trigger=r["text"],
                impact="the acceptance criterion is not proven on this tree",
                rule="mechanical acceptance criterion, checked by the orchestrator from the test output",
                fix=f"create or fix a test named exactly `{r['test']}` that exercises the criterion and passes",
                confidence="high", reviewers=["orchestrator"], previous_id=fid if fid in was_open else None,
                round=rnd))
        return out

    def _refresh_gate_changes(self, label: str) -> None:
        """Scan base..tree for gate-affecting changes, prove the provable ones harmless, record the verdicts."""
        if self.opts.dry_run:
            return
        head = self.snapshot()
        cfg = self._gate_cfg()
        changes = integrity.scan(self.workdir, self.state["base_sha"], head, cfg, self.state.get("ignored_config"))
        entries = integrity.reconcile(self.state.get("gate_changes") or [], changes)
        for e in entries:   # a proof is about one tree: other code may now be hidden by the same config
            if e["provable"] and e["proof"] and e["proof_tree"] != head:
                e["proof"] = e["proof_tree"] = ""
        if any(e["provable"] and not e["proof"] for e in entries):
            result = integrity.prove(self.workdir, self.state["base_sha"], head, changes, "full", cfg,
                                     log_dir=self.run_dir / "gates" / f"proof-{label}",
                                     target_dir=self.state.get("cargo_target_dir"))
            self.log(f"gate-change proof [{label}]: {result.status} - {result.summary} {result.error}".rstrip())
            integrity.apply_proof(entries, result, head)
            self._check_budget()
        integrity.decide(entries)
        self.state["gate_changes"] = entries
        for e in entries:
            self.log(f"   gate change [{e['id']}] {e['status']}"
                     + (f" (by {e['accepted_by']})" if e["accepted_by"] else "") + f": {e['detail'][:100]}")

    def _votable_gate_changes(self) -> str:
        """Pending changes the reviewers may vote on (not provable, not unknown). Everything implementer- or
        fixer-controlled (paths, details, summary, justification) is sanitized and bounded."""
        safe = gates.display_safe
        impl = safe(((self.state.get("implementation") or {}).get("summary") or "").strip(), 500)
        out = []
        for e in self.state.get("gate_changes") or []:
            if e.get("provable") or e["kind"] == "unknown" or e["status"] != "pending":
                continue
            lines = [f"- [{safe(e['id'], 200)}] {safe(e['kind'], 40)} in `{safe(e['path'], 200)}`: {safe(e['detail'], 300)}"]
            if e.get("justification"):
                lines.append(f"  fixer's justification (claim): {safe(e['justification'], 500)}")
            out.append("\n".join(lines))
        if out and impl:
            out.append(f"implementer's summary (claim, once for all changes above): {impl}")
        return "\n".join(out)

    def _gate_findings(self, rnd: int, prev_open: list[fnd.Finding]) -> list[fnd.Finding]:
        was_open = {f.id for f in prev_open}
        out = []
        for e in self.state.get("gate_changes") or []:
            fid = e["id"]
            accepted = e["status"] == "accepted"
            out.append(fnd.Finding(
                id=fid, severity="P1", category="gate-integrity",
                file=e["path"] if (self.workdir / e["path"]).exists() else "", line=0,
                title=f"Gate-affecting change ({gates.display_safe(e['kind'], 40)}): {gates.display_safe(e['detail'], 200)}",
                trigger="the change alters what the gates check or how strictly (detected by the orchestrator)",
                impact="a gate can pass because of this change instead of because the code is correct",
                rule="gate-affecting changes need a proof that they hide nothing (or reviewer consent)",
                fix="revert the change unless the task explicitly requires it; if it does, reject with the justification",
                confidence="high", reviewers=["orchestrator"], previous_id=fid if fid in was_open else None,
                status="accepted_gate_change" if accepted else "open", round=rnd))
        return out

    def _seen_tree(self, tree: str) -> bool:
        """Remember a tree produced by a write agent; True if it was produced before (oscillation)."""
        seen = self.state.setdefault("seen_trees", [])
        if tree in seen:
            return True
        seen.append(tree)
        return False

    # ── phases ───────────────────────────────────────────────────────────
    def phase_preflight(self) -> None:
        repo = self.repo
        if not gitutil.is_repo(repo):
            raise Abort("error", "not a git repository (rustflow needs git for snapshots and diffs)")
        if not (repo / self.cfg.get("gates.cargo_dir", ".") / "Cargo.toml").is_file():
            raise Abort("error", "no Cargo.toml found")
        review_mode = self.opts.mode == "review"
        isolation = self.opts.isolation or ("none" if review_mode else self.cfg.get("project.isolation", "branch"))
        artifacts = self.state.get("preflight_artifacts", {})
        dirty = [path for path in gitutil.dirty_files(repo)
                 if not (artifacts.get(path) and _file_sha256(repo / path) == artifacts[path])]
        if dirty and not (self.opts.allow_dirty or review_mode) and isolation != "none":
            raise Abort("error", f"working tree is dirty ({len(dirty)} files, e.g. {dirty[0]}). "
                                 "Commit/stash first, or pass --allow-dirty.")
        base_ref = self.opts.base or self.cfg.get("project.base", "HEAD")
        base_sha = gitutil.rev_parse(repo, base_ref)
        if review_mode and base_ref != "HEAD":
            base_sha = gitutil.git(repo, "merge-base", "HEAD", base_sha).strip()
        self.state.update(base_ref=base_ref, base_sha=base_sha, base_tree=gitutil.tree_of(repo, base_sha),
                          original_branch=gitutil.head_branch(repo), isolation=isolation,
                          checkpoints=bool(self.cfg.get("project.checkpoint_commits", True)) and isolation != "none")
        try:
            self.state["gates_cfg"] = gates.load_gate_config_at(repo, base_sha)
        except gates.GateConfigError as exc:
            raise Abort("error", f"cannot read the gate configuration from the base commit: {exc}") from exc
        branch = f"rustflow/{self.state['run_id']}"
        if not self.opts.dry_run and isolation in ("branch", "worktree"):
            # Never roll back a branch that existed before this preflight.
            if gitutil.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip():
                raise Abort("error", f"isolation branch already exists: {branch}")
        baseline_lock = None
        lock_was_present = True
        try:
            if self.opts.dry_run:
                self.log(f"isolation: {isolation} (dry run: not creating {branch})")
            elif isolation == "branch":
                self.state["branch"] = branch
                gitutil.create_branch(repo, branch)
                self.log(f"isolation: branch {branch} (from {self.state['original_branch']})")
            elif isolation == "worktree":
                wt = repo / ".rustflow" / "wt" / self.state["run_id"]
                self.state.update(branch=branch, workdir=str(wt), cargo_target_dir=str(repo / "target"))
                gitutil.add_worktree(repo, wt, branch, "HEAD")
                self.log(f"isolation: worktree {wt} (branch {branch}, shared target/)")
            else:
                self.log("isolation: none (working in place, no checkpoint commits)")
            if isolation == "branch":
                baseline_lock = (self.workdir / self.cfg.get("gates.cargo_dir", ".") / "Cargo.lock").resolve()
                lock_was_present = baseline_lock.exists()
            self._preflight_checks(review_mode)
            # Cargo.lock from baseline is part of the starting point, not agent work.
            self.state["start_tree"] = self.snapshot()
            try:   # gitignored config files already in place are part of the environment, not a change
                self.state["ignored_config"] = gates.ignored_config_files(
                    self.workdir, list(self.state["gates_cfg"].get("integrity_extra_paths") or []))
            except (OSError, RuntimeError, gates.IncompleteGate) as exc:
                raise Abort("error", f"cannot list gitignored config files: {exc}") from exc
        except (Abort, BudgetExceeded, gates.IncompleteGate, gitutil.GitError, KeyboardInterrupt):
            # Cleanup gets its own bounded grace period after the run deadline.
            with execution_scope(deadline=time.monotonic() + 3, cancel=None,
                                 max_output_bytes=DEFAULT_LIMITS["max_output_bytes"]):
                # A lockfile generated by baseline cargo commands belongs to the
                # starting tree. Preserve it, and trust it on resume only while
                # its contents still match the stopped preflight's artifact.
                if baseline_lock is not None and not lock_was_present and baseline_lock.is_relative_to(repo):
                    digest = _file_sha256(baseline_lock)
                    if digest:
                        self.state.setdefault("preflight_artifacts", {})[
                            baseline_lock.relative_to(repo).as_posix()] = digest
                self._undo_isolation()
            raise
        self.state["phase"] = "triage"

    def _undo_isolation(self) -> None:
        """A stop before any work: leave the checkout as it was, not on an empty `rustflow/<run>` branch."""
        branch = self.state.pop("branch", None)
        if not branch or self.opts.dry_run:
            return
        try:
            if self.state.get("isolation") == "worktree":
                gitutil.remove_worktree(self.repo, self.workdir)
                self.state["workdir"] = str(self.repo)
            else:
                orig = self.state.get("original_branch", "HEAD")
                gitutil.git(self.repo, "switch", *(["--detach", self.state["base_sha"]] if orig == "HEAD" else [orig]))
            gitutil.git(self.repo, "branch", "-D", branch)
            self.log(f"isolation undone: {branch} removed, checkout unchanged")
        except (gitutil.GitError, gates.IncompleteGate) as exc:
            self.log(f"could not undo isolation ({exc}); remove {branch} by hand")

    def _preflight_checks(self, review_mode: bool) -> None:
        self._check_skills()
        if self.cfg.get("project.baseline_gates", True) and not review_mode:
            # Same flags as the final gates (check-only, --locked) so failure signatures are comparable.
            res = self.run_gates("baseline", strict=True, fix_fmt=False)
            if not res.ok:
                self.state["baseline_red"] = res.feedback()
                self.state["baseline_signatures"] = sorted(res.signature_counts().elements())
                self._write_baseline()
                if not self.opts.accept_red_baseline:
                    raise Abort("baseline_red", "gates are already RED on the base. Fix the base first or "
                                                "rerun with --accept-red-baseline.\n" + res.feedback()[:3000])
                self.log("baseline is RED - accepted; only NEW failures will block")

    def phase_triage(self) -> None:
        task = self.state["task"]
        if self.opts.difficulty:
            result = triage.TriageResult(self.opts.difficulty, 0, ["set on the command line"], "cli")
        else:
            result = triage.heuristic(task, self.workdir, self.cfg)
            if self.opts.mode == "review":
                bonus = self._diff_bonus()
                if bonus:
                    result.score += bonus[0]
                    result.reasons.append(bonus[1])
                    result.difficulty = self._score_to_diff(result.score)
            mode = self.cfg.get("triage.mode", "heuristic")
            if mode in ("llm", "hybrid"):
                spec = AgentSpec.parse(self.cfg.get("triage.model"))
                res = self.call_agent("triage", "triage", spec, triage.TRIAGE_PROMPT + task, write=False,
                                      schema=triage.TRIAGE_SCHEMA, skill=None)
                if res.ok and res.data:
                    llm_diff = res.data["difficulty"]
                    result.llm = res.data
                    if mode == "llm":
                        result.difficulty, result.source = llm_diff, "llm"
                    else:
                        result.difficulty = max_difficulty(result.difficulty, llm_diff)
                        result.source = "hybrid"
        self.state["difficulty"] = result.difficulty
        self.state["triage"] = result.to_dict()
        self.log(f"triage: {result.difficulty} (score {result.score}; {', '.join(result.reasons)})")
        roles = ["planner", "implementer", "reviewer_a", "reviewer_b", "fixer"]
        if self.opts.mode == "review":
            roles = ["reviewer_a", "reviewer_b"] + (["fixer"] if self.opts.fix else [])
        for role in roles:
            if role == "planner" and not self._plan_needed():
                continue
            self.log(f"  {role:<12} -> {self.spec(role)}")
        if self.opts.mode == "review":
            self.state["phase"] = "gates"
            self.state["gates_after"] = "review_start"
        else:
            self.state["phase"] = "plan" if self._plan_needed() else "implement"

    def _score_to_diff(self, score: int) -> str:
        if score <= int(self.cfg.get("triage.easy_max", 1)):
            return "easy"
        return "medium" if score <= int(self.cfg.get("triage.medium_max", 4)) else "hard"

    def _diff_bonus(self) -> tuple[int, str] | None:
        d = gitutil.diff(self.workdir, self.state["base_tree"], self.state["start_tree"])
        changed = sum(1 for l in d.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))
        sig = gitutil.risk_signals(d)
        bonus, why = 0, []
        if changed > 1500:
            bonus, why = 2, [f"{changed} changed lines (+2)"]
        elif changed > 400:
            bonus, why = 1, [f"{changed} changed lines (+1)"]
        if sig.escalation:
            bonus += 3
            why.append("unsafe/FFI/atomics in diff (+3)")
        return (bonus, ", ".join(why)) if bonus else None

    def _plan_needed(self) -> bool:
        if self.opts.no_plan or self.opts.mode == "review":
            return False
        when = self.cfg.get("roles.planner.when", "hard")
        return when == "always" or (when == "hard" and self.state["difficulty"] == "hard")

    def phase_plan(self) -> None:
        res = self.call_agent("planner", "plan", self.spec("planner"),
                              prompts.plan_prompt(self.state["task"], self.language),
                              write=False, schema=schemas.PLAN_SCHEMA, skill="rustflow-plan")
        if res.ok and res.data:
            self.state["plan"] = res.data
            (self.run_dir / "plan.json").write_text(json.dumps(res.data, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
            added = crit_mod.from_plan(res.data, self.criteria)
            self.state["criteria"] = [c.to_dict() for c in self.criteria + added]
            self.log(f"plan: {len(res.data.get('steps', []))} steps, {len(added)} acceptance criteria "
                     f"({sum(1 for c in added if c.test)} with a required test)")
        else:
            self.log("plan failed - continuing without a plan")
        self.state["phase"] = "implement"

    def phase_implement(self) -> None:
        prompt = prompts.implement_prompt(self.state["task"], self.language, self.state.get("plan"),
                                          self.state.get("baseline_red"), self.criteria)
        # No output schema here: the implementer's report is informational (diff + gates are the truth),
        # and structured output on a long write session is the most fragile part of `claude -p`.
        res = self.call_agent("implementer", "implement", self.spec("implementer"), prompt,
                              write=True, schema=None, skill="rustflow-implement")
        self.state["implementation"] = {"summary": res.text.strip()[:4000]} if res.ok else {"error": res.error}
        tree = self.snapshot()
        if tree == self.state["start_tree"] and not self.opts.dry_run:
            if not res.ok:
                raise self._agent_failed(res, "the implementer")
            raise Abort("no_changes", "the implementer made no changes")
        self._seen_tree(self.state["start_tree"])
        self._seen_tree(tree)
        self.checkpoint("implement")
        self.state["phase"] = "gates"
        self.state["gates_after"] = "implement"
        self.state["gate_fix_attempts"] = 0

    def _gates_ok(self, res: gates.GateResult) -> bool:
        if res.ok:
            return True
        base = Counter(self.state.get("baseline_signatures") or [])
        # Occurrences are counted; "step|*" (an unitemized failure) can never be proven pre-existing.
        if base and not gates.new_failures(res.signature_counts(), base):
            self.log("gates: only pre-existing baseline failures remain - treated as green")
            return True
        return False

    def phase_gates(self) -> None:
        label = f"after-{self.state['gates_after']}-r{self.state['round']}-a{self.state['gate_fix_attempts']}"
        report_only = self.opts.mode == "review" and not self.opts.fix
        # Report-only review must never touch the user's checkout: no `cargo fmt` rewrite, no fixer.
        res = self.run_gates(label, fix_fmt=False if report_only else None,
                             use_cache=not self._mechanical_criteria())
        self._evaluate_mechanical(res)
        if report_only and not self._gates_ok(res):
            self.log("gates are RED - report-only review continues; reviewers get the gate output")
            self.state["last_gates"] = res.summary_line() + "\n\n" + res.feedback()[:4000]
            self.state["round"] += 1
            self.state["phase"] = "review"
            return
        if self._gates_ok(res):
            self.state["last_gates"] = res.summary_line()
            self.state["last_gates_feedback"] = ""
            self._refresh_gate_changes(label)
            self.state["round"] += 1
            self.state["phase"] = "review"
            return
        self.state["last_gates"] = res.summary_line()
        self.state["last_gates_feedback"] = res.feedback()
        attempts = self.state["gate_fix_attempts"]
        max_attempts = int(self.cfg.get("loop.max_gate_fix_attempts", 2))
        if attempts >= max_attempts:
            raise Abort("gates_red", f"gates still RED after {attempts} gate-fix attempts; not sending "
                                     "broken code to review.\n" + res.feedback()[:3000])
        difficulty = self.state["difficulty"] if attempts == 0 else bump(self.state["difficulty"])
        prompt = prompts.gate_fix_prompt(self.state["task"], self.language, res.feedback(),
                                         attempts + 1, self.state.get("baseline_red"))
        before = self.snapshot()
        # No output schema: the next gate run judges the result, a JSON report would only add turns.
        fix = self.call_agent("fixer", f"gatefix-r{self.state['round']}-a{attempts + 1}",
                              self.spec("fixer", difficulty), prompt, write=True, schema=None, skill="rustflow-fix")
        after = self.snapshot()
        self.state["gate_fix_attempts"] = attempts + 1
        if not fix.ok and after == before and not self.opts.dry_run:
            self.state["gate_fix_attempts"] = attempts      # resume repeats this attempt
            raise self._agent_failed(fix, "the gate fixer")
        if after != before and self._seen_tree(after) and not self.opts.dry_run:
            raise Abort("stalled", "the gate fixer returned the tree to an earlier state (oscillation); "
                                   "a human has to decide between the competing versions")
        self.checkpoint(f"gate fix {attempts + 1}")

    def phase_review(self) -> None:
        rnd = self.state["round"]
        head = self.snapshot()
        base_tree = self.state["base_tree"]
        diff_text = gitutil.diff(self.workdir, base_tree, head)
        diff_stat = gitutil.diff(self.workdir, base_tree, head, stat=True)
        if not diff_text.strip() and not self.opts.dry_run:
            raise Abort("no_changes", "nothing to review (empty diff against base)")
        max_kb = int(self.cfg.get("review.inline_diff_max_kb", 80))
        inline = diff_text if len(diff_text.encode("utf-8")) <= max_kb * 1024 else None
        risk = gitutil.risk_signals(diff_text)
        ranges = gitutil.changed_line_ranges(diff_text)
        advisory = self._advisory(ranges) if rnd == 1 or self.cfg.get("review.advisory_every_round") else []
        all_prev = [fnd.Finding.from_dict(f) for f in self.state.get("findings", [])]
        prev_blocking = [f for f in all_prev if f.status == "open"]
        # Mechanical findings are recomputed from the gates every round; reviewers never carry them.
        prev_findings = [f for f in all_prev if not f.mechanical]
        ctx = prompts.ReviewContext(
            task=self.state["task"], base=base_tree, head_tree=head, diff_stat=diff_stat,
            diff_inline=inline, gates_summary=self.state.get("last_gates", "n/a"),
            advisory=advisory, risk=risk.describe(),
            acceptance=[c.for_prompt() for c in self.criteria], round_no=rnd,
            gate_changes=self._votable_gate_changes(),
        )
        if rnd > 1 and self.state.get("last_review_tree"):
            prev_tree = self.state["last_review_tree"]
            delta = gitutil.diff(self.workdir, prev_tree, head)
            ctx.prev_tree = prev_tree
            ctx.delta_stat = gitutil.diff(self.workdir, prev_tree, head, stat=True)
            ctx.delta_inline = delta if len(delta.encode("utf-8")) <= max_kb * 1024 else None
            # The delta is what changed since the last review; the whole diff again only while it is small.
            full_kb = int(self.cfg.get("review.rereview_full_diff_max_kb", 16))
            if ctx.delta_inline is not None and len(diff_text.encode("utf-8")) > full_kb * 1024:
                ctx.diff_inline = None
            ctx.previous = "\n".join(f.for_prompt() for f in prev_blocking if not f.mechanical)

        difficulty = self.state["difficulty"]
        if risk.escalation:
            difficulty = "hard"
        reviewers = ["reviewer_a", "reviewer_b"]
        if rnd > 1 and self.cfg.get("loop.rereview") == "first":
            reviewers = ["reviewer_a"]
        jobs: list[tuple[str, AgentSpec, str]] = []
        for r in reviewers:
            lens = self.cfg.role_opt(r, "lens", "correctness")
            jobs.append((r, self.spec(r, difficulty), prompts.review_prompt(ctx, lens, self.language, r)))
        auditor = self.cfg.get("roles.auditor.spec", "")
        if auditor and risk.escalation and rnd == 1 and self.cfg.get("roles.auditor.mode", "llm") == "llm":
            jobs.append(("auditor", AgentSpec.parse(auditor), prompts.audit_prompt(ctx, self.language)))

        tickets = self.budget.reserve_group([name for name, _, _ in jobs]) if self.budget and not self.opts.dry_run else {}
        pool = ThreadPoolExecutor(max_workers=len(jobs))
        futures = {}
        try:
            futures = {name: pool.submit(self.call_agent, name, f"r{rnd}-{name}", spec, prompt, write=False,
                                         schema=schemas.REVIEW_SCHEMA, skill="rustflow-review",
                                         reservation=tickets.get(name), defer_limits=True)
                       for name, spec, prompt in jobs}
            # Observe failures as soon as any worker finishes. Polling also keeps
            # Windows lock waits interruptible and checks the shared deadline.
            pending = set(futures.values())
            while pending:
                done, pending = wait_futures(pending, timeout=.1, return_when=FIRST_COMPLETED)
                self._check_budget()
                for future in done:
                    result = future.result()
                    if not result.output_complete and result.termination_reason != "agent_call_limit":
                        self._incomplete(result.termination_reason, agent=result.name)
            results = {name: fut.result() for name, fut in futures.items()}
        except BaseException:
            # Reviewers run in their own process groups and do not see Ctrl+C: kill them now instead
            # of waiting up to timeout_min for the threads to finish.
            if self.budget:
                self.budget.cancel.set()  # wake retry waits and prevent any later admission
            procutil.kill_all()
            pool.shutdown(wait=False, cancel_futures=True)
            wait_futures(futures.values(), timeout=3)
            raise
        finally:
            if self.budget:
                self.budget.release_group(tickets.values())
        pool.shutdown(wait=True)
        for name, result in results.items():
            if not result.output_complete:
                self._incomplete(result.termination_reason, agent=name)
        self._check_budget()
        if not self.opts.dry_run and self.snapshot() != head:
            # Findings, anchors and the verdict describe `head`; the files are no longer that tree.
            self.state["round"] = rnd - 1
            self.state["phase"] = "gates"
            self.state["gates_after"] = "tree_change"
            raise Abort("tree_changed", "files changed while the reviewers were working (reviewers are read-only, "
                                        "so someone or something else edited the checkout). The review results "
                                        "are discarded; `rustflow resume` re-runs gates and review on the "
                                        "current tree.")
        reviews = {name: r.data for name, r in results.items() if r.ok and r.data}
        failed = [name for name, r in results.items() if not (r.ok and r.data)]
        if not reviews:
            worst = next((results[n] for n in failed if not results[n].retryable), results[failed[0]])
            self.state["failed_agent"] = {"name": ", ".join(failed), "retryable": worst.retryable,
                                          "error": "; ".join(f"{n}: {results[n].error[:200]}" for n in failed)}
            raise Abort("agent_failed", "all reviewers failed (resume re-runs this review round): "
                        + self.state["failed_agent"]["error"])
        if failed:
            self.log(f"WARNING: degraded review - failed: {', '.join(failed)}")
            # Reaches the driving agent (summary/brief/next_actions), not just this log: an approval by one
            # of two planned reviewers is not the independent double review the user was promised.
            self.state["degraded_review"] = {"round": rnd, "failed": failed, "reviewed": sorted(reviews),
                                             "errors": {n: results[n].error[:300] for n in failed}}
        else:
            self.state.pop("degraded_review", None)

        entries = self.state.get("gate_changes") or []
        for name, data in reviews.items():
            if name in integrity.REVIEWERS:
                integrity.apply_votes(entries, name, data.get("gate_changes") or [])
        integrity.decide(entries)
        ids = fnd.IdAllocator(int(self.state.get("id_counter", 0)))
        agg = fnd.aggregate(
            reviews, root=self.workdir, round_no=rnd, previous=prev_findings, ids=ids,
            block_on=list(self.cfg.get("loop.block_on", ["P0", "P1"])),
            min_confidence_single=self.cfg.get("loop.min_confidence_single", "medium"),
            escalation_signals=risk.escalation,
        )
        self.state["id_counter"] = ids.n
        agg.findings[:0] = self._mechanical_findings(rnd, prev_blocking) + self._gate_findings(rnd, prev_blocking)
        self.state["findings"] = [f.to_dict() for f in agg.findings]
        self.state["last_review_tree"] = head
        self.state["escalate"] = bool(self.state.get("escalate")) or agg.escalate
        record = {"round": rnd, "tree": head, "verdicts": agg.verdicts, "failed_reviewers": failed,
                  "findings": [f.to_dict() for f in agg.findings], "dropped": agg.dropped,
                  "acceptance": agg.acceptance, "residual_risks": agg.residual_risks,
                  "mechanical": self.state.get("mechanical", []),
                  "risk": risk.describe(), "summaries": {n: d.get("summary", "") for n, d in reviews.items()}}
        self.state["rounds"].append(record)
        if (self.state["escalate"] and self.cfg.get("roles.auditor.mode", "llm") == "tob"
                and "audit" not in self.state):
            self._tob_audit(diff_text)
        (self.run_dir / f"round-{rnd}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False),
                                                        encoding="utf-8")
        blocking = agg.blocking()
        self.log(f"review r{rnd}: verdicts {agg.verdicts}; blocking {len(blocking)}, advisory "
                 f"{len(agg.advisory())}, disputed {len(agg.disputed())}, dropped {len(agg.dropped)}")
        for f in blocking:
            self.log(f"   [{f.id}] {f.severity} {f.anchor()} {f.title[:100]}")

        if not blocking:
            self.state["outcome"] = "approved"
            self.state["phase"] = "final"
            return
        max_rounds = int(self.opts.max_rounds or self.cfg.get("loop.max_review_rounds", 3))
        if self.opts.mode == "review" and not self.opts.fix:
            self.state["outcome"] = "changes_requested"
            self.state["phase"] = "final"
            return
        by_id = {e["id"]: e for e in self.state.get("gate_changes") or []}
        if all(f.category == "gate-integrity" and by_id.get(f.id, {}).get("fix_rounds", 0) >= 1 for f in blocking):
            # Only gate changes are left and the fixer has already seen them: a human decides.
            self.state["outcome"] = "gate_changes_pending"
            self.log("only unaccepted gate-affecting changes remain after a fix round")
            self.state["phase"] = "final"
            return
        if rnd >= max_rounds:
            self.state["outcome"] = "max_rounds"
            self.state["phase"] = "final"
            return
        prev_disputes = {f.id: f.dispute_count for f in prev_blocking}
        prev_ids = set(prev_disputes)
        # A finding whose dispute count rose is progressing through the dispute protocol, not stalled.
        now_ids = {f.id for f in blocking if f.dispute_count <= prev_disputes.get(f.id, f.dispute_count)}
        if rnd > 1 and now_ids and now_ids <= prev_ids and len(now_ids) == len(prev_ids) == len(blocking):
            self.state["outcome"] = "stalled"
            self.log("no progress: the same blocking findings survived the last fix round")
            self.state["phase"] = "final"
            return
        self.state["phase"] = "fix"

    def _tob_audit(self, diff_text: str) -> None:
        """Trail of Bits `rust-review` plugin (heavy, multi-agent) on the files with risk signals.
        Runs once per run; its REPORT.md is attached to the rustflow report for the human."""
        risky = sorted({f for f, n in gates.risk_files(diff_text).items() if n})
        scope = os.path.commonpath([str(Path(f).parent) or "." for f in risky]).replace("\\", "/") if risky else "."
        scope = scope or "."
        args = self.cfg.get("roles.auditor.tob_args", "threat model REMOTE, use sonnet, high severity only")
        prompt = (f"/rust-review:rust-review {args}, scope {scope}\n\n"
                  "Non-interactive run launched by the rustflow orchestrator: do not ask questions - every "
                  "required parameter is given above. Do not modify source files or git state; write only "
                  "the plugin's own results directory.")
        results = self.workdir / ".rust-review-results"
        before = self.snapshot()
        started = time.time()
        res = self.call_agent("tob_audit", "audit-tob", AgentSpec.parse(self.cfg.get("roles.auditor.spec")),
                              prompt, write=True, schema=None, skill=None, retry=False,
                              tools=list(self.cfg.get("roles.auditor.tob_allowed_tools", [])))
        reports = sorted((p for p in results.glob("*/REPORT.md") if p.stat().st_mtime >= started - 1),
                         key=lambda p: p.stat().st_mtime) if results.is_dir() else []
        audit: dict[str, Any] = {"scope": scope, "files": risky, "ok": res.ok, "error": res.error[:500]}
        if reports:
            rpt = reports[-1]
            audit["report"] = rpt.relative_to(self.workdir).as_posix()
            audit["excerpt"] = rpt.read_text(encoding="utf-8", errors="replace")[:6000]
            self.log(f"audit (Trail of Bits): {audit['report']}")
        else:
            audit["excerpt"] = res.text[:3000]
            self.log("audit (Trail of Bits): no REPORT.md produced - see the agent log")
        self.state["audit"] = audit
        if self.snapshot() != before and not self.opts.dry_run:
            raise Abort("audit_modified_tree", "the soundness audit changed tracked/untracked source files "
                                               "(only .rust-review-results/ is allowed). Inspect `git status`.")

    def _advisory(self, ranges: dict[str, list[tuple[int, int]]]) -> list[str]:
        out: list[str] = []
        if self.opts.dry_run:
            return out
        lints = list(self.cfg.get("review.advisory_clippy", []) or [])
        if lints:
            try:
                diags = gates.advisory_clippy(self.workdir, lints, ranges, cfg=self._gate_cfg())
                out += [d.short() for d in diags]
            except gates.IncompleteGate as exc:
                self._incomplete(exc.step.termination_reason, gate="advisory")
            except Exception as exc:  # advisory only - never fail the run
                self.log(f"advisory clippy failed: {exc}")
        for tool in self.cfg.get("review.advisory", []) or []:
            if not tool.get("enabled"):
                continue
            from rustflow import procutil
            try:
                argv = procutil.resolve_command(tool["cmd"][0]) + list(tool["cmd"][1:])
            except procutil.CommandNotFound:
                self.log(f"advisory tool not found: {tool['cmd'][0]}")
                continue
            r = procutil.run(argv, cwd=self.workdir, timeout_s=float(tool.get("timeout_s", 600)))
            if not r.output_complete:
                self._incomplete(r.termination_reason, gate="advisory")
            text = (r.stdout + "\n" + r.stderr).strip()
            hits = [l for l in text.splitlines() if any(f in l for f in ranges)][:40]
            if hits:
                out.append(f"{tool.get('name', 'tool')}:\n" + "\n".join("    " + h for h in hits))
        return out

    def phase_fix(self) -> None:
        rnd = self.state["round"]
        all_f = [fnd.Finding.from_dict(f) for f in self.state["findings"]]
        blocking = [f for f in all_f if f.status == "open"]
        advisory = [f for f in all_f if f.status == "advisory"]
        difficulty = self.state["difficulty"]
        if self.cfg.get("roles.fixer.escalate_on_repeat", True) and any(f.previous_id for f in blocking):
            difficulty = bump(difficulty)
            self.log(f"findings persisted from an earlier round -> fixer escalated to {difficulty}")
        prompt = prompts.fix_prompt(self.state["task"], self.language,
                                    "\n".join(f.for_prompt() for f in blocking), rnd,
                                    bool(self.cfg.get("loop.allow_reject", True)),
                                    "\n".join(f.for_prompt() for f in advisory[:15]))
        before = self.snapshot()
        res = self.call_agent("fixer", f"fix-r{rnd}", self.spec("fixer", difficulty), prompt, write=True,
                              schema=schemas.FIX_SCHEMA, skill="rustflow-fix")
        if res.ok and res.data:
            fnd.apply_resolutions(all_f, res.data.get("resolutions", []), bool(self.cfg.get("loop.allow_reject", True)))
            self.state["findings"] = [f.to_dict() for f in all_f]
            self.state["rounds"][-1]["fix"] = res.data
        after = self.snapshot()
        if after != before and self._seen_tree(after) and not self.opts.dry_run:
            self.state["outcome"] = "stalled"
            self.log("the fixer returned the tree to an earlier state (oscillation) - stopping")
            self.state["phase"] = "final"
            return
        if not res.ok and after == before and not self.opts.dry_run:
            raise self._agent_failed(res, "the fixer")
        rejected_all = res.ok and res.data and all(
            r.get("status") == "rejected" for r in res.data.get("resolutions", [])) and res.data.get("resolutions")
        if after == before and not rejected_all and not self.opts.dry_run:
            self.state["outcome"] = "stalled"
            self.log("fixer changed nothing and did not argue the findings - stopping")
            self.state["phase"] = "final"
            return
        notes = {r.get("id"): r.get("note", "") for r in (res.data or {}).get("resolutions", [])} if res.ok else {}
        seen = {f.id for f in blocking if f.category == "gate-integrity"}
        for e in self.state.get("gate_changes") or []:
            if e["id"] in seen:
                e["fix_rounds"] = int(e.get("fix_rounds", 0)) + 1
                if notes.get(e["id"]):
                    e["justification"] = notes[e["id"]]
        self.checkpoint(f"fix round {rnd}")
        self.state["gate_fix_attempts"] = 0
        self.state["gates_after"] = "fix"
        self.state["phase"] = "gates"

    def phase_final(self) -> None:
        res = self.run_gates("final", strict=True, fix_fmt=False, use_cache=False)
        self.state["final_gates"] = res.summary_line()
        self.state["final_gates_ok"] = self._gates_ok(res)
        if self.state["final_gates_ok"]:
            self._refresh_gate_changes("final")
        self._evaluate_mechanical(res)
        missing = crit_mod.unmet(self.state.get("mechanical") or [])
        if not self.state["final_gates_ok"]:
            self.state["final_gates_feedback"] = res.feedback()
        outcome = self.state.get("outcome", "approved")
        findings = [fnd.Finding.from_dict(f) for f in self.state.get("findings", [])]
        entries = self.state.get("gate_changes") or []
        clean = outcome in ("approved", "gate_changes_pending")
        if clean and self.state["final_gates_ok"] and missing:
            status = "criteria_unmet"
            self.log("required tests not proven on the final tree: " + ", ".join(r["test"] for r in missing))
        elif clean and self.state["final_gates_ok"] and integrity.pending(entries):
            status = "gate_changes_pending"
            self.log("gate-affecting changes without acceptance: " + ", ".join(e["id"] for e in integrity.pending(entries)))
        elif clean and self.state["final_gates_ok"]:
            has_adv = bool(entries) or any(f.status in ("advisory", "disputed") for f in findings)
            status = "approved_with_advisories" if has_adv else "approved"
        elif clean:
            status = "final_gates_red"
        else:
            status = outcome
        self.state["status"] = status
        self.state["finished"] = _now()
        self.state["phase"] = "done"
        self._write_report()

    def _write_report(self) -> None:
        text = report.render(self.state, self.repo)
        (self.run_dir / "report.md").write_text(text, encoding="utf-8")
        latest = self.repo / RUNS_DIR / "LATEST"
        try:
            latest.write_text(self.state["run_id"], encoding="utf-8")
        except OSError:
            pass
        self.save()


def _install_interrupt_handlers() -> None:
    """`rustflow cancel` sends SIGINT / Windows console signals; SIGTERM from a supervisor too.
    All become KeyboardInterrupt -> agents are killed, state saved as `interrupted` (resumable)."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    if os.name == "nt" and os.environ.get("RUSTFLOW_PRIVATE_CONSOLE") == "1":
        import ctypes
        # A parent's "ignore Ctrl+C" console attribute is inherited even by a new console.
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
        signal.signal(signal.SIGINT, handler)
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def find_run(repo: Path, ref: str | None) -> Path:
    return runs.find_run(repo, ref)


def elapsed(start: float) -> str:
    return f"{time.monotonic() - start:.0f}s"
