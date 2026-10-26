#!/usr/bin/env python3
"""Deterministic Rust quality gates - one source of truth for hooks, orchestrator and CI.

Standalone (stdlib only; Python 3.9+ - reads rustflow.toml with tomllib on 3.11+, tomli if installed,
otherwise defaults). `rustflow init` copies it to `.rustflow/gates.py`.

    python .rustflow/gates.py run --profile edit|stop|full [--json] [--no-cache]
    python .rustflow/gates.py ci            # full profile, fmt --check, --locked, no cache
    python .rustflow/gates.py fingerprint   # tree hash used for caching

Profiles
    edit : cargo fmt (fix) + cargo check                      (seconds; PostToolUse hook)
    stop : cargo fmt (fix) + clippy -D warnings + tests       (Stop hook, agent self-check)
    full : fmt + clippy + tests + doc tests + cargo-deny + extras   (orchestrator, final)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROFILES = ("edit", "stop", "full")
PROFILE_RANK = {p: i for i, p in enumerate(PROFILES)}

DEFAULT_GATES = {
    "features": ["--all-features"],
    "tests": "auto",
    "doc_tests": True,
    "deny": "auto",
    "clippy_args": ["-D", "warnings"],
    "edit_check": True,
    "edit_fmt": "file",
    "stop_max_blocks": 3,
    "log_lines": 60,
    "cargo_dir": ".",
    "semver": "auto",
    "sqlx": "auto",
    "extra": [],
    "tree_stable": True,
}
RUNTIME_PATHS = ("target", ".rustflow/runs", ".rustflow/cache", ".rustflow/wt")
MANIFEST_SPECS = ("Cargo.toml", "Cargo.lock", "*/Cargo.toml", "**/Cargo.toml")


# ── data ─────────────────────────────────────────────────────────────────────
@dataclass
class Diagnostic:
    level: str
    message: str
    code: str = ""
    file: str = ""
    line: int = 0
    rendered: str = ""

    def short(self) -> str:
        loc = f"{self.file}:{self.line}: " if self.file else ""
        code = f"[{self.code}] " if self.code else ""
        return f"{loc}{self.level}: {code}{self.message}"


@dataclass
class Step:
    name: str
    ok: bool
    cmd: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""
    duration_s: float = 0.0
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failed_tests: list[str] = field(default_factory=list)
    tail: str = ""
    passed_tests: list[str] = field(default_factory=list)   # for mechanical acceptance criteria


@dataclass
class GateResult:
    profile: str
    ok: bool
    fingerprint: str | None
    cached: bool = False
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GateResult":
        steps = []
        for s in d.get("steps", []):
            s = dict(s)
            s["diagnostics"] = [Diagnostic(**x) for x in s.get("diagnostics", [])]
            steps.append(Step(**s))
        return cls(d["profile"], d["ok"], d.get("fingerprint"), d.get("cached", False), steps)

    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if not s.ok and not s.skipped]

    def signatures(self) -> set[str]:
        """Identity of each failure, to compare against a red baseline."""
        sig: set[str] = set()
        for s in self.failed_steps():
            # No line numbers: they shift when unrelated code changes.
            items = [f"{d.file}|{d.code}|{d.message}" for d in s.diagnostics if d.level == "error"]
            items += s.failed_tests
            sig.update(f"{s.name}|{i}" for i in items) if items else sig.add(f"{s.name}|*")
        return sig

    def summary_line(self) -> str:
        if self.cached:
            return f"GREEN (cached: this exact tree already passed the '{self.profile}' profile)"
        parts = []
        for s in self.steps:
            parts.append(f"{s.name}={'SKIP' if s.skipped else ('PASS' if s.ok else 'FAIL')}")
        return ("GREEN" if self.ok else "RED") + (" (cached)" if self.cached else "") + " | " + ", ".join(parts)

    def feedback(self, max_diags: int = 25, max_tail_lines: int = 60, max_chars: int | None = None) -> str:
        """Compact text for a model: only what failed. max_chars cuts the END: the first error is usually
        the root cause and the step header says what failed; later diagnostics are often its echoes."""
        if self.ok:
            return f"All gates passed ({self.profile})."
        out: list[str] = []
        for s in self.failed_steps():
            out.append(f"### GATE FAILED: {s.name}")
            out.append("$ " + " ".join(s.cmd))
            if s.reason:
                out.append(s.reason)
            if s.diagnostics:
                errors = [d for d in s.diagnostics if d.level == "error"] or s.diagnostics
                for d in errors[:max_diags]:
                    out.append(d.rendered.rstrip() if d.rendered else d.short())
                if len(errors) > max_diags:
                    out.append(f"... and {len(errors) - max_diags} more diagnostics")
            if s.failed_tests:
                out.append("Failed tests: " + ", ".join(s.failed_tests[:30]))
            if s.tail and (not s.diagnostics or s.failed_tests):
                out.append("\n".join(s.tail.splitlines()[-max_tail_lines:]))
            out.append("")
        text = "\n".join(out).strip()
        if max_chars and len(text) > max_chars:
            cut = text.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            text = (text[:cut] + f"\n... ({len(text) - cut} more characters cut - fix the first errors above, "
                    "then rerun the gates)")
        return text


# ── environment ──────────────────────────────────────────────────────────────
def _git(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=full_env)


def find_root(start: Path | None = None) -> Path:
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    start = (start or Path.cwd()).resolve()
    try:
        r = _git(start, "rev-parse", "--show-toplevel")
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except FileNotFoundError:
        pass
    for p in (start, *start.parents):
        if (p / "Cargo.toml").is_file():
            return p
    return start


def load_gate_config(root: Path) -> dict:
    cfg = dict(DEFAULT_GATES)
    path = root / "rustflow.toml"
    if path.is_file():
        try:
            import tomllib as toml  # Python 3.11+
        except ImportError:
            try:
                import tomli as toml  # type: ignore[no-redef]
            except ImportError:
                print("rustflow gates: Python < 3.11 without tomli - using default [gates] settings",
                      file=sys.stderr)
                return cfg
        try:
            with path.open("rb") as fh:
                data = toml.load(fh)
            cfg.update(data.get("gates", {}) or {})
        except (OSError, ValueError) as exc:  # TOMLDecodeError subclasses ValueError
            print(f"rustflow gates: cannot read rustflow.toml: {exc}", file=sys.stderr)
    return cfg


def crate_edition(path: Path, root: Path) -> str:
    """Edition of the package that owns `path` (what `cargo fmt` would pass to rustfmt); 2015 if unset."""
    try:
        import tomllib as toml  # Python 3.11+
    except ImportError:
        return "2021"
    member = None
    for d in [path.parent, *path.parent.parents]:
        manifest = d / "Cargo.toml"
        if manifest.is_file():
            try:
                data = toml.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if member is None and "package" in data:
                member = data["package"].get("edition", "2015")
                if not isinstance(member, dict):
                    return str(member)
            if isinstance(member, dict) and "workspace" in data:  # edition.workspace = true
                return str(data["workspace"].get("package", {}).get("edition", "2015"))
        if d == root or d == d.parent:
            break
    return "2015" if member is None else "2021"


def runtime_excludes(root: Path) -> list[str]:
    """Pathspec excludes for build/runtime dirs - only those that exist and are NOT already ignored
    (git refuses an explicit pathspec that names an ignored path)."""
    out = []
    for rel in RUNTIME_PATHS:
        if (root / rel).exists() and _git(root, "check-ignore", "-q", rel).returncode != 0:
            out.append(f":(exclude){rel}")
    return out


def tree_fingerprint(root: Path) -> str | None:
    """Hash of the working tree (tracked + untracked, respecting .gitignore) without touching the index."""
    try:
        if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
            return None
    except FileNotFoundError:
        return None
    idx = _git(root, "rev-parse", "--git-path", "index").stdout.strip()
    real_index = (root / idx) if idx and not Path(idx).is_absolute() else Path(idx or "")
    fd, tmp = tempfile.mkstemp(prefix="rustflow-idx-")
    os.close(fd)
    try:
        if real_index.is_file():
            shutil.copyfile(real_index, tmp)
        else:
            os.unlink(tmp)
        env = {"GIT_INDEX_FILE": tmp}
        add = _git(root, "add", "-A", "--", ".", *runtime_excludes(root), env=env)
        if add.returncode != 0:
            return None
        wt = _git(root, "write-tree", env=env)
        return wt.stdout.strip() or None if wt.returncode == 0 else None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def manifests_changed(root: Path) -> bool:
    base = os.environ.get("RUSTFLOW_BASE")
    if base:
        r = _git(root, "diff", "--name-only", base, "--", *MANIFEST_SPECS)
        if r.returncode == 0 and r.stdout.strip():
            return True
    r = _git(root, "status", "--porcelain", "--", *MANIFEST_SPECS)
    return r.returncode == 0 and bool(r.stdout.strip())


def has_tool(*argv: str) -> bool:
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── cargo output parsing ─────────────────────────────────────────────────────
def parse_cargo_json(stdout: str, root: Path | None = None, base: Path | None = None) -> list[Diagnostic]:
    """root: paths are reported relative to it (git root). base: dir cargo ran in (relative spans)."""
    diags: list[Diagnostic] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        m = msg.get("message") or {}
        level = m.get("level", "")
        if level not in ("error", "warning"):
            continue
        text = m.get("message", "")
        if level == "warning" and re.match(r"\d+ warnings? emitted", text):
            continue
        if level == "error" and text.startswith(("aborting due to", "could not compile")):
            continue
        rendered = m.get("rendered") or ""
        if rendered in seen:
            continue
        seen.add(rendered)
        spans = m.get("spans") or []
        primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else None)
        file_name = (primary or {}).get("file_name", "")
        if file_name and root is not None:
            fp = Path(file_name)
            if not fp.is_absolute():
                fp = (base or root) / fp
            try:
                file_name = fp.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                pass
        code = ((m.get("code") or {}) or {}).get("code", "") if m.get("code") else ""
        diags.append(Diagnostic(level=level, message=text, code=code or "",
                                file=file_name.replace("\\", "/"),
                                line=int((primary or {}).get("line_start", 0) or 0),
                                rendered=rendered))
    return diags


_TEST_FAIL_RES = (
    re.compile(r"^test (.+?) \.\.\. FAILED\s*$", re.M),   # names may contain spaces (doctests)
    re.compile(r"^\s*FAIL \[[^\]]*\]\s+(?:\(\s*\d+/\d+\)\s+)?(.+?)\s*$", re.M),
)


_TEST_PASS_RES = (
    re.compile(r"^test (.+?) \.\.\. ok\s*$", re.M),
    re.compile(r"^\s*PASS \[[^\]]*\]\s+(?:\(\s*\d+/\d+\)\s+)?(.+?)\s*$", re.M),
)


def parse_passed_tests(text: str) -> list[str]:
    """Names of tests that ran and passed (libtest `test x ... ok`, nextest `PASS [..] bin x`).
    Ignored tests and filtered-out tests are absent, so `0 tests ran` never counts as proof."""
    found: dict[str, None] = {}
    for rx in _TEST_PASS_RES:
        for m in rx.finditer(text):
            found.setdefault(m.group(1).strip(), None)
    return list(found)


def parse_failed_tests(text: str) -> list[str]:
    found: list[str] = []
    for rx in _TEST_FAIL_RES:
        for m in rx.finditer(text):
            name = m.group(1).strip()
            if name not in found:
                found.append(name)
    return found


def run_tree(argv: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int, str, str]:
    """subprocess with a hard timeout that kills the whole tree (cargo + test binaries).
    subprocess.run(timeout=) only kills cargo; on Windows an orphaned test keeps the pipe open."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kwargs)
    except OSError as exc:
        return 127, "", str(exc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out or "", (err or "") + f"\nTIMEOUT after {timeout}s"
    except KeyboardInterrupt:
        kill_process_tree(proc)
        raise


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


SQLX_HINT = ("HINT: the queries in the code do not match .sqlx/. Run: cargo sqlx prepare --workspace -- "
             "--all-targets --all-features  (schema changed? first: sqlx migrate run) and keep .sqlx/ in the commit.")
_SQLX_MACRO = re.compile(r"\bquery(?:_as|_scalar|_file|_file_as|_file_scalar)?(?:_unchecked)?!")


def uses_sqlx_macros(root: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", ".git", ".rustflow", "node_modules")]
        for fn in filenames:
            if fn.endswith(".rs"):
                try:
                    if _SQLX_MACRO.search(Path(dirpath, fn).read_text(encoding="utf-8", errors="ignore")):
                        return True
                except OSError:
                    continue
    return False


def database_url(root: Path) -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = root / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def publishable_libs_from_metadata(meta: dict) -> list[str]:
    out = []
    ws = set(meta.get("workspace_members") or [])
    for pkg in meta.get("packages") or []:
        if ws and pkg.get("id") not in ws:
            continue
        if pkg.get("publish") == []:          # publish = false
            continue
        if any(k in ("lib", "rlib", "proc-macro") for t in pkg.get("targets") or [] for k in t.get("kind") or []):
            out.append(pkg.get("name", "?"))
    return out


# ── risk signals in added lines (escalation to a soundness audit) ──────────────
@dataclass
class RiskSignals:
    unsafe: int = 0
    ffi: int = 0
    unsafe_misc: int = 0
    atomics: int = 0
    locks_async: int = 0
    manifests: int = 0

    @property
    def escalation(self) -> int:
        return self.unsafe + self.ffi + self.unsafe_misc + self.atomics

    def describe(self) -> str:
        return (f"unsafe: {self.unsafe} | ffi/no_mangle/packed: {self.ffi} | "
                f"static mut/transmute/unsafe impl Send|Sync: {self.unsafe_misc} | "
                f"atomic orderings: {self.atomics} | locks/select!/blocking: {self.locks_async} | "
                f"manifest changes: {self.manifests} | ESCALATION_SIGNALS={self.escalation}")


_RISK = {
    "unsafe": re.compile(r"\bunsafe\b"),
    "ffi": re.compile(r'extern\s+"C"|#\[(unsafe\()?no_mangle|#\[repr\([^)]*packed'),
    "unsafe_misc": re.compile(r"\bstatic mut\b|transmute|unsafe impl (Send|Sync)"),
    "atomics": re.compile(r"Ordering::(Relaxed|Acquire|Release|AcqRel|SeqCst)"),
    "locks_async": re.compile(r"\bMutex\b|\bRwLock\b|Condvar|select!|spawn_blocking|block_on"),
}


def risk_files(diff_text: str) -> dict[str, int]:
    """{file: number of added lines with escalation signals (unsafe/FFI/atomics...)}."""
    out: dict[str, int] = {}
    current = ""
    keys = ("unsafe", "ffi", "unsafe_misc", "atomics")
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current = line[4:]
            current = current[2:] if current.startswith("b/") else current
            continue
        if line.startswith("+") and not line.startswith("+++") and current.endswith(".rs"):
            code = line[1:].split("//", 1)[0]
            if any(_RISK[k].search(code) for k in keys):
                out[current] = out.get(current, 0) + 1
    return out


def risk_signals(diff_text: str) -> RiskSignals:
    sig = RiskSignals()
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current = line[4:]
            if current.endswith("Cargo.toml") or current.endswith("Cargo.lock"):
                sig.manifests += 1
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if not current.endswith(".rs"):
            continue
        code = line[1:].split("//", 1)[0]
        for name, rx in _RISK.items():
            if rx.search(code):
                setattr(sig, name, getattr(sig, name) + 1)
    return sig


def is_compile_error(d: Diagnostic) -> bool:
    """rustc errors have codes like E0308 or none (syntax); lints carry the lint name as code."""
    return d.level == "error" and (not d.code or re.fullmatch(r"E\d{4}", d.code) is not None)


# ── runner ───────────────────────────────────────────────────────────────────
class GateRunner:
    def __init__(self, root: Path, cfg: dict | None = None, *, ci: bool = False, strict: bool = False,
                 log_dir: Path | None = None):
        """strict: check-only (fmt --check, --locked). ci: strict + extras with when_env always run and
        cargo-deny regardless of manifest changes (the CI job owns its environment)."""
        self.root = root
        self.cfg = cfg or load_gate_config(root)
        self.cargo_dir = (root / self.cfg.get("cargo_dir", ".")).resolve()
        self.ci = ci
        self.strict = strict or ci
        self.log_dir = log_dir or (root / ".rustflow" / "cache" / "logs")
        self.features = list(self.cfg.get("features") or [])
        self.locked = ["--locked"] if self.strict and (self.cargo_dir / "Cargo.lock").is_file() else []

    def _exec(self, name: str, argv: list[str], timeout: int = 3600) -> tuple[int, str, str, float]:
        env = os.environ.copy()
        env.update({"CARGO_TERM_COLOR": "never", "RUST_BACKTRACE": "0"})
        start = time.monotonic()
        rc, out, err = run_tree(argv, self.cargo_dir, env, timeout)
        dur = time.monotonic() - start
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            (self.log_dir / f"{safe}.log").write_text(
                f"$ {' '.join(argv)}\n--- stdout\n{out}\n--- stderr\n{err}\n", encoding="utf-8")
        except OSError:
            pass
        return rc, out, err, dur

    def _tail(self, text: str) -> str:
        lines = int(self.cfg.get("log_lines", 60))
        return "\n".join(text.splitlines()[-lines:])

    # individual steps
    def fmt(self, fix: bool) -> Step:
        argv = ["cargo", "fmt", "--all"] + ([] if fix else ["--", "--check", "--color", "never"])
        rc, out, err, dur = self._exec("fmt", argv)
        return Step("fmt", rc == 0, argv, duration_s=dur, tail=self._tail(out + err) if rc else "")

    def fmt_file(self, path: Path) -> Step:
        """rustfmt one file through stdin. Input on stdin has no path, so rustfmt does not descend into
        `mod x;` children: an edit never reformats files the agent did not touch (`cargo fmt --all` would
        rewrite the whole workspace of an unformatted repo on the first edit). Syntax errors leave the file
        as it is - `cargo check` reports them."""
        argv = ["rustfmt", "--edition", crate_edition(path, self.root)]
        try:
            src = path.read_text(encoding="utf-8")
            r = subprocess.run(argv, input=src, capture_output=True, text=True, encoding="utf-8",
                               cwd=path.parent, timeout=120)   # cwd: rustfmt.toml is found from here upwards
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
            return Step("fmt", False, argv, reason=str(exc))
        if r.returncode == 0 and r.stdout and r.stdout != src:
            path.write_text(r.stdout, encoding="utf-8")
        return Step("fmt", r.returncode == 0, argv, tail=self._tail(r.stderr) if r.returncode else "")

    def cargo_json(self, name: str, sub: str, extra_after: list[str] | None = None,
                   keep_all: bool = False) -> Step:
        argv = ["cargo", sub, "--workspace", "--all-targets", "--message-format=json",
                *self.features, *self.locked]
        if extra_after:
            argv += ["--", *extra_after]
        rc, out, err, dur = self._exec(name, argv)
        diags = parse_cargo_json(out, self.root, self.cargo_dir)
        ok = rc == 0
        tail = "" if ok else self._tail(err)
        if not keep_all and ok:
            diags = [d for d in diags if d.level == "error"]
        return Step(name, ok, argv, duration_s=dur, diagnostics=diags, tail=tail)

    def check(self) -> Step:
        return self.cargo_json("check", "check")

    def clippy(self) -> Step:
        return self.cargo_json("clippy", "clippy", list(self.cfg.get("clippy_args") or []))

    def tests(self) -> Step:
        mode = self.cfg.get("tests", "auto")
        if mode == "off":
            return Step("tests", True, skipped=True, reason="disabled in config")
        use_nextest = mode == "nextest" or (mode == "auto" and has_tool("cargo", "nextest", "--version"))
        if use_nextest:
            argv = ["cargo", "nextest", "run", "--workspace", *self.features, *self.locked,
                    "--no-fail-fast", "--no-tests=pass", "--status-level", "pass",
                    "--final-status-level", "fail"]
        else:
            argv = ["cargo", "test", "--workspace", *self.features, *self.locked, "--no-fail-fast"]
        rc, out, err, dur = self._exec("tests", argv)
        both = out + "\n" + err
        ok = rc == 0
        diags = [d for d in parse_cargo_json(out, self.root, self.cargo_dir) if d.level == "error"]
        return Step("tests", ok, argv, duration_s=dur, diagnostics=diags,
                    failed_tests=parse_failed_tests(both) if not ok else [],
                    tail="" if ok else self._tail(both), passed_tests=parse_passed_tests(both))

    def doc_tests(self) -> Step:
        if not self.cfg.get("doc_tests", True):
            return Step("doctests", True, skipped=True, reason="disabled in config")
        argv = ["cargo", "test", "--workspace", "--doc", *self.features, *self.locked]
        rc, out, err, dur = self._exec("doctests", argv)
        both = out + "\n" + err
        if rc != 0 and "no library targets found" in both:
            return Step("doctests", True, argv, skipped=True, reason="no library targets", duration_s=dur)
        return Step("doctests", rc == 0, argv, duration_s=dur,
                    failed_tests=parse_failed_tests(both) if rc else [],
                    tail="" if rc == 0 else self._tail(both), passed_tests=parse_passed_tests(both))

    def deny(self) -> Step:
        mode = self.cfg.get("deny", "auto")
        argv = ["cargo", "deny", *self.locked, "check"]
        if mode == "off":
            return Step("deny", True, argv, skipped=True, reason="disabled in config")
        if not (self.cargo_dir / "deny.toml").is_file():
            return Step("deny", True, argv, skipped=True, reason="no deny.toml (run: cargo deny init)")
        if mode == "auto" and not self.ci and not manifests_changed(self.root):  # strict local: same rule
            return Step("deny", True, argv, skipped=True, reason="manifests unchanged")
        if not has_tool("cargo", "deny", "--version"):
            return Step("deny", True, argv, skipped=True, reason="cargo-deny not installed")
        rc, out, err, dur = self._exec("deny", argv)
        return Step("deny", rc == 0, argv, duration_s=dur, tail="" if rc == 0 else self._tail(out + err))

    def sqlx(self) -> Step:
        """`cargo sqlx prepare --check`: .sqlx/ offline metadata must match the query!() macros.
        auto: only when the code uses SQLx macros and a DATABASE_URL is available (env or .env);
        require: missing DATABASE_URL / sqlx-cli is a failure (set it in CI)."""
        mode = self.cfg.get("sqlx", "auto")
        argv = ["cargo", "sqlx", "prepare", "--workspace", "--check", "--", "--all-targets", *self.features]
        if mode == "off":
            return Step("sqlx", True, argv, skipped=True, reason="disabled in config")
        require = mode == "require" or os.environ.get("REQUIRE_SQLX_CHECK") == "1"
        if not uses_sqlx_macros(self.cargo_dir):
            return Step("sqlx", True, argv, skipped=True, reason="no SQLx query macros")
        if not database_url(self.cargo_dir):
            return Step("sqlx", not require, argv, skipped=not require,
                        reason="no DATABASE_URL (env or .env)" + (" but sqlx = require" if require else ""))
        if not has_tool("cargo", "sqlx", "--version"):
            return Step("sqlx", not require, argv, skipped=not require,
                        reason="sqlx-cli not installed (cargo install --locked sqlx-cli)")
        rc, out, err, dur = self._exec("sqlx", argv)
        return Step("sqlx", rc == 0, argv, duration_s=dur, tail="" if rc == 0 else self._tail(out + err),
                    reason="" if rc == 0 else SQLX_HINT)

    def publishable_libs(self) -> list[str]:
        """Workspace packages that are published (publish != false) and have a library target."""
        rc, out, _err = run_tree(["cargo", "metadata", "--no-deps", "--format-version", "1", *self.locked],
                                 self.cargo_dir, os.environ.copy(), 300)
        if rc != 0:
            return []
        try:
            meta = json.loads(out)
        except json.JSONDecodeError:
            return []
        return publishable_libs_from_metadata(meta)

    def semver(self) -> Step:
        mode = self.cfg.get("semver", "auto")
        argv = ["cargo", "semver-checks", "--workspace"]
        base = os.environ.get("RUSTFLOW_BASE")
        if base:
            argv += ["--baseline-rev", base]  # compare against the run's base commit, not crates.io
        if mode == "off":
            return Step("semver", True, argv, skipped=True, reason="disabled in config")
        if not has_tool("cargo", "semver-checks", "--version"):
            if mode == "always":
                return Step("semver", False, argv, reason="cargo-semver-checks not installed "
                                                          "(cargo install --locked cargo-semver-checks)")
            return Step("semver", True, argv, skipped=True, reason="cargo-semver-checks not installed")
        libs = self.publishable_libs()
        if not libs:
            return Step("semver", True, argv, skipped=True, reason="no publishable library crates")
        rc, out, err, dur = self._exec("semver", argv)
        return Step("semver", rc == 0, argv, duration_s=dur, tail="" if rc == 0 else self._tail(out + err),
                    reason="" if rc == 0 else "public API change without a semver-compatible version bump")

    def extras(self, profile: str) -> list[Step]:
        steps = []
        for ex in self.cfg.get("extra") or []:
            if profile not in (ex.get("profiles") or ["full"]):
                continue
            name = ex.get("name", "extra")
            argv = list(ex.get("cmd") or [])
            when_env = ex.get("when_env")
            if when_env and not os.environ.get(when_env) and not self.ci:
                steps.append(Step(name, True, argv, skipped=True, reason=f"${when_env} not set"))
                continue
            rc, out, err, dur = self._exec(name, argv, int(ex.get("timeout_s", 1800)))
            steps.append(Step(name, rc == 0, argv, duration_s=dur,
                              tail="" if rc == 0 else self._tail(out + err),
                              reason=ex.get("hint", "") if rc else ""))
        return steps

    # profiles
    def run(self, profile: str, *, fix_fmt: bool | None = None, fail_fast: bool = False,
            after_fmt=None) -> list[Step]:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile}")
        fix = (not self.strict) if fix_fmt is None else fix_fmt
        steps: list[Step] = [self.fmt(fix=fix)]
        if after_fmt is not None:
            after_fmt()
        if profile == "edit":
            if self.cfg.get("edit_check", True):
                steps.append(self.check())
            return steps
        plan = [self.clippy, self.sqlx, self.tests]
        if profile == "full":
            plan += [self.doc_tests, self.deny, self.semver]
        for fn in plan:
            st = fn()
            steps.append(st)
            if fail_fast and not st.ok and not st.skipped:
                return steps
            if st.name == "clippy" and not st.ok and any(is_compile_error(d) for d in st.diagnostics):
                # Code does not compile: tests would only repeat the same errors.
                steps.append(Step("tests", False, skipped=True, reason="skipped: code does not compile"))
                return steps
        steps += self.extras(profile)
        return steps


# ── active run (one orchestrator per repo; hooks of the driving session respect it) ──
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_lock_path(repo: Path) -> Path:
    return repo / ".rustflow" / "runs" / "ACTIVE.json"


def read_active(repo: Path) -> dict | None:
    """The live run in this repo, or None. A lock whose process is gone is stale and ignored."""
    path = active_lock_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not pid_alive(int(data.get("pid") or 0)):
        return None
    return data


def find_active_for(path: Path) -> dict | None:
    """Active run whose working directory contains `path` (checked from the repo root upwards)."""
    path = path.resolve()
    for cand in (path, *path.parents):
        if (cand / ".rustflow" / "runs").is_dir():
            act = read_active(cand)
            if act:
                return act
    return None


# ── cache ────────────────────────────────────────────────────────────────────
def _cache_path(root: Path) -> Path:
    return root / ".rustflow" / "cache" / "gates.json"


def cache_lookup(root: Path, profile: str, fp: str | None) -> bool:
    if not fp:
        return False
    try:
        data = json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return any(data.get(p) == fp for p in PROFILES if PROFILE_RANK[p] >= PROFILE_RANK[profile])


def cache_store(root: Path, profile: str, fp: str | None) -> None:
    if not fp:
        return
    path = _cache_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[profile] = fp
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def run_gates(root: Path, profile: str, *, ci: bool = False, strict: bool = False, use_cache: bool = True,
              log_dir: Path | None = None, cfg: dict | None = None,
              fix_fmt: bool | None = None) -> GateResult:
    root = root.resolve()
    strict = strict or ci
    fp = tree_fingerprint(root)
    if use_cache and not strict and cache_lookup(root, profile, fp):
        return GateResult(profile, True, fp, cached=True)
    runner = GateRunner(root, cfg, ci=ci, strict=strict, log_dir=log_dir)
    check_tree = profile == "full" and bool(runner.cfg.get("tree_stable", True))
    mid: dict[str, str | None] = {}
    steps = runner.run(profile, fix_fmt=fix_fmt,
                       after_fmt=(lambda: mid.setdefault("fp", tree_fingerprint(root))) if check_tree else None)
    fp_after = tree_fingerprint(root)  # fmt --fix may have changed files
    if check_tree:
        step = tree_stability_step(root, mid.get("fp"), fp_after, allow_lockfile=not runner.locked)
        if step is not None:
            steps.append(step)
    ok = all(s.ok or s.skipped for s in steps)
    if ok and not strict:
        cache_store(root, profile, fp_after)
    return GateResult(profile, ok, fp_after, steps=steps)


def changed_between(root: Path, tree_a: str, tree_b: str) -> list[str]:
    r = _git(root, "diff", "--name-only", "--no-renames", tree_a, tree_b)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def tree_stability_step(root: Path, before: str | None, after: str | None, *,
                        allow_lockfile: bool) -> Step | None:
    """Gate results describe one tree. If clippy/tests/doctests/extras changed tracked or untracked,
    non-ignored files (a build.rs or test writing into the source tree, a snapshot tool, a code
    generator), the results belong to neither version - fail instead of approving a moving target.
    Cargo.lock may change unless the run uses --locked (non-strict runs resolve new dependencies there;
    a crate without a committed lockfile gets one created)."""
    if not before or not after or before == after:
        return None
    changed = changed_between(root, before, after)
    bad = [p for p in changed if not (allow_lockfile and Path(p).name == "Cargo.lock")]
    if not bad:
        return None
    return Step("tree", False, ["git", "diff", "--name-only", before[:12], after[:12]],
                reason=("the gates changed source files while running (build script, test or tool writing into "
                        "the tree): results are not valid for any single version. Write such output under "
                        "target/ or OUT_DIR, or add it to .gitignore; generators belong in a step before the gates."),
                diagnostics=[Diagnostic("error", "changed while the gates were running", file=p) for p in bad[:50]],
                tail="\n".join(bad[:50]))


# ── advisory lints for reviewers ─────────────────────────────────────────────
def advisory_clippy(root: Path, lints: list[str], changed: dict[str, list[tuple[int, int]]],
                    cfg: dict | None = None, limit: int = 60) -> list[Diagnostic]:
    """Run clippy with extra (warn) lints and keep only diagnostics on changed lines."""
    if not lints:
        return []
    runner = GateRunner(root, cfg)
    extra: list[str] = []
    for lint in lints:
        extra += ["-W", lint]
    step = runner.cargo_json("advisory-clippy", "clippy", extra, keep_all=True)
    out: list[Diagnostic] = []
    for d in step.diagnostics:
        if d.level != "warning" or not d.file:
            continue
        ranges = changed.get(d.file)
        if ranges and any(a <= d.line <= b for a, b in ranges):
            out.append(d)
    return out[:limit]


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gates.py", description="rustflow deterministic Rust gates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a gate profile")
    r.add_argument("--profile", choices=PROFILES, default="full")
    r.add_argument("--json", action="store_true", help="print GateResult as JSON")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--root", type=Path)
    r.add_argument("--check", action="store_true",
                   help="check-only: never modify the tree (fmt --check, --locked, no cache) - for reviewers")
    c = sub.add_parser("ci", help="CI mode: full profile, fmt --check, --locked, no cache")
    c.add_argument("--root", type=Path)
    c.add_argument("--json", action="store_true")
    f = sub.add_parser("fingerprint")
    f.add_argument("--root", type=Path)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # cp1252 pipes on Windows vs non-ASCII feedback
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    root = (args.root or find_root()).resolve()
    if args.cmd == "fingerprint":
        print(tree_fingerprint(root) or "")
        return 0
    if not (root / load_gate_config(root).get("cargo_dir", ".") / "Cargo.toml").is_file():
        print(f"rustflow gates: no Cargo.toml in {root}", file=sys.stderr)
        return 0
    if args.cmd == "ci":
        res = run_gates(root, "full", ci=True, use_cache=False)
    else:
        res = run_gates(root, args.profile, strict=args.check, use_cache=not (args.no_cache or args.check))
    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))
    else:
        for s in res.steps:
            status = "skip" if s.skipped else ("ok" if s.ok else "FAIL")
            extra = f" ({s.reason})" if s.reason else ""
            print(f"{status:>4}: {s.name} [{s.duration_s:.1f}s]{extra}")
        if not res.ok:
            print()
            print(res.feedback())
        print("ALL GREEN" + (" (cached)" if res.cached else "") if res.ok else "GATES RED")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
