#!/usr/bin/env python3
"""Claude Code hooks for rustflow (stdlib only; Windows + Linux, no bash needed).

`rustflow init` copies this file to `.rustflow/hooks.py` next to `gates.py` and registers:

    PostToolUse(Edit|Write|MultiEdit)  -> hooks.py post-edit   cargo fmt + cargo check, errors back to Claude
    Stop                               -> hooks.py stop        clippy -D warnings + tests; block until green
    PreToolUse(Bash|PowerShell)        -> hooks.py guard-bash  role-aware command guard
    UserPromptSubmit                   -> hooks.py route-review steer "zrób review" to /rustflow-pr-review
    rustflow-reviewer subagent:  guard-bash --role reviewer, check-report (SubagentStop contract)
    /rustflow-pr-review skill:   hooks.py review-context [base]  (context injected into the skill)

Role comes from $RUSTFLOW_ROLE (set by the orchestrator). Without it (interactive session) the
implementer rules apply, plus a gate check on `git commit`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True  # no .rustflow/__pycache__ littering the working tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import gates  # type: ignore[import-not-found]   # .rustflow/gates.py (installed layout)
except ImportError:  # running from the package
    from rustflow import gates  # type: ignore[no-redef]

READ_ONLY_ROLES = {"reviewer_a", "reviewer_b", "auditor", "planner", "triage", "reviewer"}
ORCHESTRATED_WRITE_ROLES = {"implementer", "fixer"}
AUDIT_ROLES = {"tob_audit"}   # writes only its results dir: no gates, no git
RUST_RELEVANT = re.compile(r"(\.rs|Cargo\.toml|Cargo\.lock|\.sql|\.html|\.askama|\.jinja|build\.rs|"
                           r"rust-toolchain(\.toml)?|clippy\.toml|rustfmt\.toml|\.proto)$")


def _role() -> str:
    return os.environ.get("RUSTFLOW_ROLE", "").strip()


def _read_input() -> dict:
    # Claude Code sends UTF-8; sys.stdin would decode with the locale code page (cp1252 on Windows).
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except (AttributeError, OSError):
        raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _baseline_signatures(root: Path) -> Counter:
    """Failures accepted by the orchestrator on a red base (--accept-red-baseline) for this run.

    Stored as a list with duplicates (one entry per occurrence); a duplicate-free list counts each once.
    """
    run = os.environ.get("RUSTFLOW_RUN")
    if not run:
        return Counter()
    try:
        data = json.loads((root / ".rustflow" / "cache" / "baseline.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Counter()
    return Counter(data.get("signatures", [])) if data.get("run") == run else Counter()


def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    return 0


def _state_dir(root: Path) -> Path:
    d = root / ".rustflow" / "cache" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


_safe = gates.display_safe   # model-visible form of implementer-controlled strings


def _integrity():
    """Imported lazily: a missing/broken integrity module must fail closed in the integrity path only."""
    try:
        import integrity  # type: ignore[import-not-found]   # .rustflow/integrity.py (installed layout)
    except ImportError as first:
        try:  # running from the package
            from rustflow import integrity  # type: ignore[no-redef]
        except ImportError:
            raise RuntimeError(f"cannot import .rustflow/integrity.py ({first})") from None
    return integrity


def _head_sha(root: Path) -> str | None:
    """HEAD commit, None in a repo without commits. Git failures become GateConfigError (fail closed)."""
    try:
        r = gates._git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except (OSError, subprocess.SubprocessError, gates.IncompleteGate) as exc:
        raise gates.GateConfigError(f"git failed resolving HEAD: {exc}") from exc
    return r.stdout.strip() or None if r.returncode == 0 else None


def _hook_config(root: Path) -> dict:
    """Gate config the hooks enforce: never the (model-editable) working tree when a base exists.

    Orchestrated roles: the config at $RUSTFLOW_BASE (the run's base commit). Interactive: HEAD.
    GateConfigError propagates; callers turn it into block/deny.
    """
    if _role():
        base = os.environ.get("RUSTFLOW_BASE", "").strip()
        return gates.load_gate_config_at(root, base) if base else gates.load_gate_config(root)
    head = _head_sha(root)
    return gates.load_gate_config_at(root, head) if head else gates.load_gate_config(root)


def _proof_cache(root: Path) -> Path:
    return root / ".rustflow" / "cache" / "proof.json"


def _extra_paths(root: Path) -> list[str]:
    try:
        return list(_hook_config(root).get("integrity_extra_paths") or [])
    except gates.GateConfigError:
        return []


def _describe_changes(changes: list, limit: int = 5) -> str:
    items = [f"[{_safe(c.id, 120)}] {_safe(c.kind, 40)} {_safe(c.path, 120)} ({_safe(c.detail, 200)})"
             for c in changes[:limit]]
    more = f"; +{len(changes) - limit} more" if len(changes) > limit else ""
    return "; ".join(items) + more


def _integrity_verdict(root: Path, cfg: dict) -> tuple[bool, str, bool]:
    """Gate-integrity check of the working tree against HEAD (interactive sessions).

    (True, "", False) nothing changed; (True, note, False) proven harmless / nothing to compare;
    (False, why, False) gate-affecting changes not proven harmless; (False, why, True) the check itself
    could not run (internal/installation error). Fail-closed: any exception is (False, ..., True).
    """
    try:
        base = _head_sha(root)
        if not base:
            return True, "rustflow: no commit yet - gate integrity not checked", False
        integrity = _integrity()
        tree = gates.tree_fingerprint(root) or ""
        changes = integrity.scan(root, base, tree, cfg)
        if not changes:
            return True, "", False
        unprovable = [c for c in changes if not c.provable]
        if unprovable:
            return False, "not provably harmless: " + _describe_changes(unprovable), False
        # the same tree against another HEAD is another diff; ignored config files are not part of the tree
        ignored = gates.ignored_digest(gates.ignored_config_files(root, list(cfg.get("integrity_extra_paths") or [])))
        key = f"{base}:{tree}:{ignored}"
        try:
            cache = json.loads(_proof_cache(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        status = cache.get(key) if isinstance(cache, dict) else None
        detail = "cached result for this exact tree"
        if status not in ("passed", "failed"):
            res = integrity.prove(root, base, tree, changes, "stop", cfg)
            status = "passed" if res.status == "passed" else "failed"
            detail = "; ".join(x for x in (_safe(res.summary, 300), _safe(res.error or "", 300)) if x)
            if tree:
                try:
                    _proof_cache(root).parent.mkdir(parents=True, exist_ok=True)
                    _proof_cache(root).write_text(json.dumps({key: status}), encoding="utf-8")
                except OSError:
                    pass
        if status == "passed":
            return True, "rustflow: gate-affecting changes proven harmless: " + _describe_changes(changes, 3), False
        return False, "proof with the HEAD configuration failed (" + detail + "): " + _describe_changes(changes), False
    except Exception as exc:  # fail closed
        return False, (f"rustflow gate-integrity check could not run ({_safe(type(exc).__name__)}: "
                       f"{_safe(exc, 300)}); gate changes were not verified. Run `rustflow init` / `rustflow doctor`, "
                       "or ask the user to commit your changes himself."), True


def _integrity_text(why: str, internal: bool) -> str:
    if internal:
        return why
    return f"Gate-affecting changes: {why}. Revert them, or ask the user to commit them himself."


# ── post-edit ────────────────────────────────────────────────────────────────
def post_edit(data: dict) -> int:
    if _role() in READ_ONLY_ROLES | AUDIT_ROLES or os.environ.get("RUSTFLOW_HOOKS") == "0":
        return 0
    tool_input = data.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not file_path:
        return 0
    norm = file_path.replace("\\", "/")
    if "/target/" in norm or not RUST_RELEVANT.search(norm):
        return 0
    root = gates.find_root()
    try:
        cfg = _hook_config(root)
    except gates.GateConfigError as exc:
        sys.stderr.write(f"rustflow: cannot read the gate configuration: {_safe(exc, 300)}\n")
        return 2
    if not (root / cfg.get("cargo_dir", ".") / "Cargo.toml").is_file():
        return 0
    runner = gates.GateRunner(root, cfg)
    if norm.endswith(".rs"):  # syntax errors are reported by cargo check below
        mode = cfg.get("edit_fmt", "file")
        if mode == "all":
            runner.fmt(fix=True)
        elif mode == "file":
            path = Path(file_path) if Path(file_path).is_absolute() else root / file_path
            # A file that was not rustfmt-clean at the base stays as written: formatting it would put a
            # reformat of the whole file into the diff (see [gates].fmt_fix_scope).
            if cfg.get("fmt_fix_scope", "changed") != "changed" or runner.clean_at(
                    path, os.environ.get("RUSTFLOW_BASE") or "HEAD"):
                runner.fmt_file(path)
    if not cfg.get("edit_check", True) or os.environ.get("RUST_EDIT_CHECK") == "0":
        return 0
    step = runner.check()
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "nosession"))
    memo = _state_dir(root) / f"check-{session}"
    if step.ok:
        memo.unlink(missing_ok=True)
        return 0
    errors = [d for d in step.diagnostics if d.level == "error"]
    # Mid-refactor every edit fails the same way: repeat a known error set in one line, not 15 snippets.
    sig = json.dumps(sorted(f"{d.file}|{d.code}|{d.message}" for d in errors))
    if errors and memo.is_file() and memo.read_text(encoding="utf-8") == sig:
        sys.stderr.write(f"cargo check: still the same {len(errors)} compile error(s) as after the previous edit "
                         f"(first: {errors[0].short()[:200]}). Finish the change, then fix them.\n")
        return 2
    if errors:
        memo.write_text(sig, encoding="utf-8")
    lines = [d.rendered.rstrip() or d.short() for d in errors[:15]]
    if not lines:
        lines = step.tail.splitlines()[-25:]
    sys.stderr.write(
        f"cargo check: compile errors after editing {Path(file_path).name}.\n"
        "If this edit is part of a larger change, finish it first; then fix:\n" + "\n".join(lines) + "\n")
    return 2


# ── stop ─────────────────────────────────────────────────────────────────────
def _same_dir(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _inside(path: Path, directory: str | Path) -> bool:
    try:
        path.resolve().relative_to(Path(directory).resolve())
        return True
    except (OSError, ValueError):
        return False


def guard_edit(data: dict) -> int:
    """PreToolUse(Edit|Write|MultiEdit|NotebookEdit) for the DRIVING session: while a rustflow run works in a
    checkout, nobody else edits it (the run's agents carry $RUSTFLOW_ROLE and are not affected)."""
    if _role():
        return 0
    tool_input = data.get("tool_input") or {}
    target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not target:
        return 0
    root = gates.find_root()
    act = gates.find_active_for(root)
    if not act:
        return 0
    path = Path(target) if Path(target).is_absolute() else root / target
    if not _inside(path, act.get("workdir", root)):
        return 0  # e.g. worktree isolation: the main checkout stays editable
    return _decide("deny", f"rustflow run {act['run_id']} is changing this checkout (phase "
                           f"{act.get('phase', '?')}). Wait: `rustflow wait {act['run_id']}` - stop: "
                           f"`rustflow cancel {act['run_id']}`.")


def _rust_changes_pending(root: Path, cfg: dict | None = None) -> bool:
    try:
        r = gates._git(root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError, gates.IncompleteGate):
        return True
    if r.returncode != 0:
        return True
    extra = list((cfg or {}).get("integrity_extra_paths", []) or [])
    for line in r.stdout.splitlines():
        for part in line[3:].split(" -> "):   # a rename lists both sides
            path = part.strip().strip('"')
            if path.startswith(".rustflow/"):
                continue
            if RUST_RELEVANT.search(path):
                return True
            try:
                if _integrity().is_config_path(path, extra):
                    return True
            except Exception:  # fail closed: cannot classify -> check
                return True
    try:   # a gitignored config file is invisible to `status` but cargo and the gates read it
        return bool(gates.ignored_config_files(root, extra))
    except Exception:  # fail closed
        return True


def _config_notice(root: Path, cfg: dict, session: str) -> str:
    """Once per session: rustflow.toml does not parse, so the hooks run with default [gates] settings."""
    err = cfg.get("_config_error")
    if not err:
        return ""
    memo = _state_dir(root) / f"config-warned-{session}"
    if memo.is_file():
        return ""
    memo.write_text(err, encoding="utf-8")
    return (f"rustflow: rustflow.toml is invalid ({err[:200]}) - hooks and gates use the default [gates] "
            "settings until it is fixed (`rustflow doctor`).")


def stop(data: dict) -> int:
    role = _role()
    if role in READ_ONLY_ROLES | AUDIT_ROLES or os.environ.get("RUSTFLOW_HOOKS") == "0":
        return 0
    root = gates.find_root()
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "nosession"))
    counter = _state_dir(root) / f"stop-blocks-{session}"
    notice = ""

    def finish(obj: dict | None = None) -> int:
        if notice:
            obj = {**(obj or {}), "systemMessage": " ".join(x for x in (notice, (obj or {}).get("systemMessage")) if x)}
        return _emit(obj) if obj else 0

    def block(max_blocks: int, what: str, reason) -> int:
        try:
            n = int(counter.read_text(encoding="utf-8")) + 1
        except (OSError, ValueError):
            n = 1
        counter.write_text(str(n), encoding="utf-8")
        if n > max_blocks:
            counter.unlink(missing_ok=True)
            return finish({"systemMessage": f"rustflow: {what} still failing after {max_blocks} attempts - "
                                            "review manually (logs: .rustflow/cache/logs/)."})
        return finish({"decision": "block", "reason": reason(n)})

    try:
        cfg = _hook_config(root)
    except gates.GateConfigError as exc:
        err = _safe(exc, 300)
        return block(3, "gate configuration unreadable", lambda n: (
            f"rustflow cannot read the gate configuration ({err}); gates cannot be checked (attempt {n}/3)."))
    notice = _config_notice(root, cfg, session)
    if not (root / cfg.get("cargo_dir", ".") / "Cargo.toml").is_file():
        return finish()
    # Interactive sessions: only when Rust-relevant files differ from HEAD. Orchestrated: always
    # (the orchestrator commits checkpoints, so "no diff vs HEAD" does not mean "nothing to check").
    if not role:
        act = gates.find_active_for(root)
        if act and _same_dir(act.get("workdir", ""), root):
            return finish()  # a rustflow run owns this checkout; its own gates decide, not the driving session's
    if role not in ORCHESTRATED_WRITE_ROLES and not _rust_changes_pending(root, cfg):
        return finish()
    max_blocks = int(cfg.get("stop_max_blocks", 3))
    res = gates.run_gates(root, "stop", cfg=cfg)
    baseline = _baseline_signatures(root)
    if not (res.ok or (baseline and not gates.new_failures(res.signature_counts(), baseline))):
        return block(max_blocks, "gates", lambda n: (
            f"rustflow gates failed (attempt {n}/{max_blocks}). Fix them before finishing. Do not "
            "silence lints with #[allow] or skip/weaken tests.\n\n" + res.feedback(max_diags=10, max_chars=8000)))
    if not role:  # the orchestrator owns gate-affecting changes of orchestrated roles
        ok, why, internal = _integrity_verdict(root, cfg)
        if not ok:
            return block(max_blocks, "gate integrity", lambda n: (
                f"{_integrity_text(why, internal)} (attempt {n}/{max_blocks})"))
        counter.unlink(missing_ok=True)
        return finish({"systemMessage": why} if why else None)
    counter.unlink(missing_ok=True)
    return finish()


# ── guard-bash ───────────────────────────────────────────────────────────────
GITP = r"git(?:\s+(?:-[Cc]\s+\S+|--no-pager|--git-dir=\S+|--work-tree=\S+))*\s+"  # git -C dir ... = git ...
_RO_FIRST = re.compile(
    r"^\s*(" + GITP + r"(diff|log|show|status|blame|merge-base|rev-parse|ls-files|grep|cat-file)( |$)"
    r"|cargo (\+[a-z0-9.-]+ )?(fmt( --all)?( --)? --check|clippy|check|test|nextest (run|list)|doc|tree|"
    r"metadata|deny( --locked)? check|audit|semver-checks|geiger|expand|miri test)( |$)"
    r"|rg |grep |ls( |$)|dir( |$)|wc |cat |type |head |tail |Get-Content |Select-String "
    r"|find [^ ]+ .*-name|rust-analyzer (diagnostics|analysis-stats)"
    r"|(?:py -3|python3?|\S*python[0-9.]*(?:\.exe)?) \S*\.rustflow/gates\.py run (?=.*--check)\S*)")
_RO_PIPE = re.compile(r"^\s*(head|tail|grep|rg|wc|sort|uniq|cut|Select-Object|Select-String)( |$)")
_RO_FORBIDDEN = re.compile(
    r"--fix|--allow-dirty|--bless|--output|-o\s|cargo (add|remove|update|install|publish|upgrade)"
    r"|" + GITP + r"(commit|push|checkout|reset|restore|stash|add|rm|clean|switch|rebase|merge|apply|tag|branch -[dD])")
_ALWAYS_DENY = [
    (re.compile(GITP + r"push\b.*(--force|-f\b|--force-with-lease)"), "force push is not allowed"),
    (re.compile(r"\bcargo\s+publish\b"), "publishing crates is not allowed from an agent session"),
    (re.compile(GITP + r"(commit|push)\b.*--no-verify"), "--no-verify bypasses project gates"),
    (re.compile(r"sqlx\s+database\s+(drop|reset)"), "destructive database command - ask the user to run it"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME|\.\.?/?)(\s|$)"), "recursive delete of a root/home/parent dir"),
    (re.compile(r"Remove-Item\b.*-Recurse.*\s(C:\\|~|\$HOME)"), "recursive delete of a root/home dir"),
]
_ORCH_GIT = re.compile(r"(^|[;&|]\s*)" + GITP + r"(commit|push|reset|checkout|switch|rebase|merge|stash|clean|"
                       r"restore|branch\s+-[dDmM]|tag|worktree)\b")
_COMMIT = re.compile(r"(^|[;&|]\s*)" + GITP + r"commit\b")


def _decide(decision: str, reason: str) -> int:
    return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                         "permissionDecisionReason": reason}})


_RTK_PREFIX = re.compile(r"(^|[;&|(]\s*)rtk\s+(?:proxy\s+)?")
_RTK_ALIASES = {"read": "cat", "rg": "rg", "grep": "grep", "find": "find", "ls": "ls", "wc": "wc"}


def unwrap_rtk(cmd: str) -> str:
    """`rtk git commit` -> `git commit`, `rtk read f` -> `cat f`: judge what will actually run.
    Agents with rtk's global instructions write the prefix themselves."""
    def sub(m: re.Match) -> str:
        return m.group(1)
    out = _RTK_PREFIX.sub(sub, cmd)
    for alias, real in _RTK_ALIASES.items():
        out = re.sub(rf"(^|[;&|(]\s*){alias}\b", lambda m: m.group(1) + real, out) if alias != real else out
    return out


_SQLX_REVERT = re.compile(r"sqlx\s+migrate\s+revert")
_LOCAL_DB = re.compile(r"^(sqlite:|[a-z+]+://([^@/]*@)?(localhost|127\.0\.0\.1|\[::1\]|postgres|db|mysql)(:\d+)?/)")


def sqlx_revert_target(cmd: str) -> str:
    m = re.search(r"(?:--database-url[ =]|-D\s+|DATABASE_URL=)(\S+)", cmd)
    if m:
        return m.group(1).strip("'\"")
    return gates.database_url(gates.find_root())


def guard_bash(data: dict) -> int:
    cmd = str((data.get("tool_input") or {}).get("command") or "")
    if not cmd.strip():
        return 0
    cmd = unwrap_rtk(cmd)
    role = _role()
    for rx, why in _ALWAYS_DENY:
        if rx.search(cmd):
            return _decide("deny", f"rustflow guard: {why}.")
    if _SQLX_REVERT.search(cmd):
        url = sqlx_revert_target(cmd)
        if not _LOCAL_DB.match(url or ""):
            return _decide("deny", "rustflow guard: `sqlx migrate revert` is allowed only on a local database "
                                   "(localhost / sqlite). Instead of reverting an applied migration, add a new one.")
    if role in READ_ONLY_ROLES:
        norm = cmd.replace("2>&1", "").replace("2>/dev/null", "").replace("2>$null", "")
        if re.search(r"[;&<>`]|\$\(", norm):
            return _decide("deny", "read-only role: no command chaining, redirects or substitutions; "
                                   "run one command at a time.")
        if _RO_FORBIDDEN.search(norm):
            return _decide("deny", "read-only role must not modify files, dependencies or git state.")
        segs = norm.split("|")
        if not _RO_FIRST.search(segs[0]):
            return _decide("deny", f"read-only role: command not on the allowlist: {segs[0].strip()[:120]}")
        for seg in segs[1:]:
            if not _RO_PIPE.search(seg):
                return _decide("deny", f"read-only role: pipe target not allowed: {seg.strip()[:80]}")
        if re.search(r"find .*-(exec|delete)", norm):
            return _decide("deny", "find -exec/-delete not allowed")
        return _decide("allow", "rustflow read-only allowlist")
    if role and re.search(r"(^|[;&|]\s*)(\S*[/\\])?rustflow(\.exe)?\s+(run|review|resume|cancel)\b|-m\s+rustflow\s+(run|review|resume)", cmd):
        return _decide("deny", "you are an agent of a running rustflow orchestrator - do not start or control runs.")
    if role in ORCHESTRATED_WRITE_ROLES | AUDIT_ROLES and _ORCH_GIT.search(cmd):
        return _decide("deny", "rustflow manages git (branches/checkpoint commits). Leave your changes "
                               "in the working tree.")
    if not role and _ORCH_GIT.search(cmd):
        act = gates.find_active_for(gates.find_root())
        if act and _same_dir(act.get("workdir", ""), gates.find_root()):
            return _decide("deny", f"rustflow run {act['run_id']} is working in this checkout - no git state "
                                   f"changes until it finishes (`rustflow wait {act['run_id']}`).")
    if not role and _COMMIT.search(cmd):
        root = gates.find_root()
        try:
            cfg = _hook_config(root)
        except gates.GateConfigError as exc:
            return _decide("deny", f"Commit blocked: cannot read the gate configuration ({_safe(exc, 300)}).")
        if (root / cfg.get("cargo_dir", ".") / "Cargo.toml").is_file():
            res = gates.run_gates(root, "stop", cfg=cfg)
            if not res.ok:
                return _decide("deny", "Commit blocked: rustflow gates are RED. Fix them, then commit.\n\n"
                                       + res.feedback(max_diags=10, max_chars=6000))
            ok, why, internal = _integrity_verdict(root, cfg)
            if not ok:
                return _decide("deny", "Commit blocked. " + _integrity_text(why, internal))
    return 0


# ── /rustflow-pr-review support ──────────────────────────────────────────────
_RUST_SPECS = ("*.rs", "*Cargo.toml", "Cargo.lock", "*rust-toolchain*", "*clippy.toml", "*rustfmt.toml")


def _resolve_base(root: Path, ref: str | None) -> tuple[str, str]:
    candidates = [ref] if ref else ["origin/main", "origin/master", "main", "master"]
    for cand in candidates:
        r = gates._git(root, "merge-base", "HEAD", cand)
        if r.returncode == 0 and r.stdout.strip():
            return cand, r.stdout.strip()
    head = gates._git(root, "rev-parse", "HEAD").stdout.strip()
    label = f"{ref} (NOT FOUND - falling back to HEAD: only uncommitted changes are in scope)" if ref else \
        "HEAD (no origin/main|main|master found: only uncommitted changes are in scope)"
    return label, head or "HEAD"


def review_context(argv: list[str]) -> int:
    """Printed into the /rustflow-pr-review skill. Must ALWAYS exit 0: a failing `!` command aborts
    the whole skill invocation."""
    try:
        root = gates.find_root()
        base_ref, base = _resolve_base(root, argv[0] if argv else None)
        tree = gates.tree_fingerprint(root) or "HEAD"
        print(f"BASE_REF={base_ref}")
        print(f"BASE_SHA={base}")
        print(f"WORKTREE_TREE={tree}   (git diff {base} {tree} = committed + uncommitted + untracked)")
        print("\n### Changed Rust-relevant files (base -> working tree)")
        stat = gates._git(root, "diff", "--stat=120", base, tree, "--", *_RUST_SPECS).stdout.strip()
        print("\n".join(stat.splitlines()[-40:]) or "(none)")
        diff = gates._git(root, "diff", "-U0", base, tree, "--", *_RUST_SPECS).stdout
        print("\n### Risk signals in added lines")
        print(gates.risk_signals(diff).describe())
        print("\n### Deterministic gates")
        fp = gates.cache_key(root, gates.tree_fingerprint(root), _extra_paths(root))
        if gates.cache_lookup(root, "full", fp):
            print("GREEN (full profile) for exactly this tree - cached; no need to rerun.")
        else:
            print("not run for this tree - run them (check-only) as step 1.")
    except Exception as exc:  # never abort the skill
        print(f"(context unavailable: {exc})")
    return 0


_REPORT_GATE = re.compile(r"^- (fmt|clippy|tests): (PASS|FAIL|NOT RUN)", re.M | re.I)
_FINDING = re.compile(r"^- \[P[0-3]\]", re.M)
_ANCHOR = re.compile(r"[A-Za-z0-9_./\\-]+\.rs:[0-9]+")


def report_problems(msg: str) -> list[str]:
    missing: list[str] = []
    if not re.search(r"^## Gates", msg, re.M):
        missing.append("section '## Gates'")
    if not re.search(r"^## Findings", msg, re.M):
        missing.append("section '## Findings'")
    if not re.search(r"^## Verdict: (APPROVE|REQUEST_CHANGES|ESCALATE)\b", msg, re.M):
        missing.append("line '## Verdict: APPROVE|REQUEST_CHANGES|ESCALATE'")
    found = {m.group(1).lower() for m in _REPORT_GATE.finditer(msg)}
    for g in ("fmt", "clippy", "tests"):
        if g not in found:
            missing.append(f"gate line '- {g}: PASS|FAIL|NOT RUN (...)'")
    findings = [l for l in msg.splitlines() if _FINDING.match(l)]
    if findings:
        bad = [l for l in findings if not _ANCHOR.search(l)][:3]
        if bad:
            missing.append("file.rs:line anchor in findings: " + " | ".join(bad))
    elif not re.search(r"no findings", msg, re.I):
        missing.append("findings as '- [P0..P3] path.rs:LINE - ...' or the words 'No findings'")
    if re.search(r"^## Verdict: APPROVE", msg, re.M):
        if re.search(r"^- (fmt|clippy|tests): FAIL", msg, re.M | re.I):
            missing.append("APPROVE is invalid while a gate FAILs")
        if re.search(r"^- \[P[01]\]", msg, re.M):
            missing.append("APPROVE is invalid with P0/P1 findings")
    return missing


def check_report(data: dict) -> int:
    """SubagentStop for rustflow-reviewer: enforce the report contract (max 2 resends per session)."""
    msg = str(data.get("last_assistant_message") or "")
    if not msg:
        return 0  # older Claude Code without last_assistant_message
    root = gates.find_root()
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "x"))
    counter = _state_dir(root) / f"report-{session}"
    problems = report_problems(msg)
    if not problems:
        counter.unlink(missing_ok=True)
        return 0
    try:
        n = int(counter.read_text(encoding="utf-8")) + 1
    except (OSError, ValueError):
        n = 1
    counter.write_text(str(n), encoding="utf-8")
    if n > 2:
        counter.unlink(missing_ok=True)
        return 0  # loop guard
    sys.stderr.write("Review report does not follow the rustflow-reviewer contract. Fix it and resend the "
                     "FULL report. Missing/invalid:\n" + "".join(f" - {p}\n" for p in problems))
    return 2


_REVIEW_INTENT = re.compile(
    r"(^|[^\w])(review|code ?review|przegl\w*d\w*|zrecenzuj\w*|recenzj\w*|"
    r"sprawd\w* (pr|zmiany|kod|diff))([^\w]|$)", re.I)


def route_review(data: dict) -> int:
    """UserPromptSubmit: steer a free-form review request to the /rustflow-pr-review skill."""
    if _role():
        return 0  # orchestrated agents have their own contract
    prompt = str(data.get("prompt") or "")
    if prompt.lstrip().startswith("/") or not _REVIEW_INTENT.search(prompt):
        return 0
    if re.search(r"\brustflow\b|full workflow|pełn\w* (workflow|cykl)|codex", prompt, re.I):
        return 0  # the user chose the orchestrated review explicitly - the `rustflow` skill handles it
    root = gates.find_root()
    if not (root / gates.load_gate_config(root).get("cargo_dir", ".") / "Cargo.toml").is_file():
        return 0
    return _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        "Project rule: a Rust code review goes through the rustflow-pr-review skill (Skill tool, optional "
        "base ref argument) - a forked, read-only reviewer with deterministic gates and the REVIEW.md "
        "severity matrix. Do not review ad hoc. If its verdict is ESCALATE, offer the Trail of Bits audit "
        "command it prints.")}})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: hooks.py post-edit|stop|guard-bash|route-review|check-report|review-context [--role R]",
              file=sys.stderr)
        return 1
    if "--role" in argv:  # agent-frontmatter hooks: the role is fixed by the agent definition
        i = argv.index("--role")
        if i + 1 < len(argv):
            os.environ["RUSTFLOW_ROLE"] = argv[i + 1]
            del argv[i:i + 2]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if argv[0] == "review-context":  # not a hook: no JSON on stdin
        return review_context(argv[1:])
    data = _read_input()
    handlers = {"post-edit": post_edit, "stop": stop, "guard-bash": guard_bash, "route-review": route_review,
                "check-report": check_report, "guard-edit": guard_edit}
    handler = handlers.get(argv[0])
    if handler is None:
        print(f"unknown hook {argv[0]}", file=sys.stderr)
        return 1
    try:
        return handler(data)
    except Exception as exc:  # a crashing hook must never wedge the session
        print(f"rustflow hook {argv[0]} error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
