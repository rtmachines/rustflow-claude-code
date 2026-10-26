"""`rustflow lints`: a lint ratchet.

Every rule the compiler enforces is one finding an LLM reviewer no longer has to catch (for free, on
every edit). Turning lints on in an existing codebase usually turns the gates red, so this command
MEASURES each candidate with one clippy run and adopts only the lints the code already satisfies.
The rest are reported with counts, ready to adopt once cleaned up. Candidates come from the
rust-feedback-loop workspace lint set (+ undocumented_unsafe_blocks for the REVIEW.md SAFETY rule).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rustflow import gates


@dataclass(frozen=True)
class Candidate:
    group: str            # "rust" | "clippy"
    name: str
    level: str            # level once adopted
    why: str
    test_allow: str = ""  # clippy.toml key that exempts #[test] code

    @property
    def code(self) -> str:
        return f"clippy::{self.name}" if self.group == "clippy" else self.name


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("rust", "unsafe_code", "forbid", "no unsafe at all (only adopted if there is none today)"),
    Candidate("rust", "unused_must_use", "deny", "ignored Result/#[must_use] values"),
    Candidate("rust", "missing_docs", "warn", "every public item documented"),
    Candidate("clippy", "await_holding_lock", "deny", "std MutexGuard held across .await (deadlocks)"),
    Candidate("clippy", "await_holding_refcell_ref", "deny", "RefCell borrow held across .await"),
    Candidate("clippy", "unwrap_used", "warn", "panics on recoverable paths", "allow-unwrap-in-tests"),
    Candidate("clippy", "expect_used", "warn", "panics on recoverable paths", "allow-expect-in-tests"),
    Candidate("clippy", "panic", "warn", "explicit panic! in production code", "allow-panic-in-tests"),
    Candidate("clippy", "todo", "warn", "todo!() left in code"),
    Candidate("clippy", "unimplemented", "warn", "unimplemented!() left in code"),
    Candidate("clippy", "dbg_macro", "warn", "dbg!() left in code", "allow-dbg-in-tests"),
    Candidate("clippy", "undocumented_unsafe_blocks", "warn", "unsafe block without // SAFETY: (REVIEW.md)"),
    Candidate("clippy", "large_futures", "warn", "huge futures moved around (stack/perf)"),
    Candidate("clippy", "unused_async", "warn", "async fn that never awaits"),
    Candidate("clippy", "print_stderr", "warn", "eprintln! instead of tracing (services/libraries)"),
    Candidate("clippy", "print_stdout", "warn", "println! instead of tracing (services/libraries)"),
)


@dataclass
class Measured:
    cand: Candidate
    count: int = 0
    samples: list[str] = field(default_factory=list)
    configured: str | None = None   # existing level in the manifests, if any

    @property
    def adoptable(self) -> bool:
        return self.configured is None and self.count == 0


class LintError(RuntimeError):
    pass


# ── manifests ────────────────────────────────────────────────────────────────
def metadata(root: Path) -> dict:
    rc, out, err = gates.run_tree(["cargo", "metadata", "--no-deps", "--format-version", "1"], root,
                                  os.environ.copy(), 300)
    if rc != 0:
        raise LintError(f"cargo metadata failed: {err.strip()[-400:]}")
    return json.loads(out)


def layout(meta: dict) -> tuple[Path, list[Path], bool]:
    """(root manifest, member manifests, is_workspace)."""
    root = Path(meta["workspace_root"]) / "Cargo.toml"
    members = set(meta.get("workspace_members") or [])
    manifests = [Path(p["manifest_path"]) for p in meta.get("packages", []) if p.get("id") in members]
    data = tomllib.loads(root.read_text(encoding="utf-8"))
    return root, manifests, "workspace" in data


def configured_levels(root_manifest: Path, is_ws: bool) -> dict[str, str]:
    data = tomllib.loads(root_manifest.read_text(encoding="utf-8"))
    lints = (data.get("workspace", {}) if is_ws else data).get("lints", {}) or {}
    out: dict[str, str] = {}
    for group in ("rust", "clippy"):
        for name, val in (lints.get(group) or {}).items():
            level = val.get("level") if isinstance(val, dict) else val
            out[f"clippy::{name}" if group == "clippy" else name] = str(level)
    return out


# ── measuring ────────────────────────────────────────────────────────────────
def _clippy_conf_dir(root: Path, cands: list[Candidate]) -> Path:
    """Existing clippy.toml + the #[test] exemptions the adopted lints would bring."""
    tmp = Path(tempfile.mkdtemp(prefix="rustflow-clippy-"))
    src = root / "clippy.toml"
    if not src.is_file():
        src = root / ".clippy.toml"
    text = src.read_text(encoding="utf-8") if src.is_file() else ""
    have = tomllib.loads(text) if text else {}
    extra = [f"{c.test_allow} = true" for c in cands if c.test_allow and c.test_allow not in have]
    (tmp / "clippy.toml").write_text(text.rstrip() + ("\n" if text else "") + "\n".join(extra) + "\n",
                                     encoding="utf-8")
    return tmp


def measure(root: Path, cands: list[Candidate], features: list[str] | None = None) -> dict[str, Measured]:
    conf = _clippy_conf_dir(root, cands)
    argv = ["cargo", "clippy", "--workspace", "--all-targets", "--message-format=json",
            *(features if features is not None else ["--all-features"]), "--"]
    for c in cands:
        argv += ["-W", c.code]
    env = os.environ.copy()
    env.update({"CLIPPY_CONF_DIR": str(conf), "CARGO_TERM_COLOR": "never"})
    rc, out, err = gates.run_tree(argv, root, env, 3600)
    diags = gates.parse_cargo_json(out, root)
    if any(gates.is_compile_error(d) for d in diags):
        raise LintError("the code does not compile - fix the build first (`rustflow gates`)")
    res = {c.code: Measured(c) for c in cands}
    for d in diags:
        m = res.get(d.code)
        if m is None:
            continue
        m.count += 1
        if len(m.samples) < 3:
            m.samples.append(f"{d.file}:{d.line}")
    return res


# ── applying ─────────────────────────────────────────────────────────────────
def _insert_entries(text: str, header: str, lines: list[str]) -> str:
    rx = re.compile(rf"^\[{re.escape(header)}\][ \t]*(#.*)?$", re.M)
    m = rx.search(text)
    if m:
        pos = m.end()
        return text[:pos] + "\n" + "\n".join(lines) + text[pos:]
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + f"[{header}]\n" + "\n".join(lines) + "\n"


def _entry(c: Candidate) -> str:
    # Lints are named with underscores in rustc/clippy config; levels as plain strings.
    return f'{c.name} = "{c.level}"'


def apply(root_manifest: Path, members: list[Path], is_ws: bool, adopt: list[Candidate],
          clippy_toml: Path) -> list[str]:
    """Write the adopted lints. Returns human-readable notes of what changed."""
    notes: list[str] = []
    if not adopt:
        return notes
    text = root_manifest.read_text(encoding="utf-8")
    prefix = "workspace.lints" if is_ws else "lints"
    parsed = tomllib.loads(text)
    lints_tbl = (parsed.get("workspace", {}) if is_ws else parsed).get("lints")
    if lints_tbl is not None and not isinstance(lints_tbl, dict):
        raise LintError(f"unsupported [{prefix}] layout in {root_manifest}; edit it by hand")
    if not is_ws and isinstance(lints_tbl, dict) and lints_tbl.get("workspace") is True:
        raise LintError(f"{root_manifest} inherits workspace lints; run from the workspace root")
    for group in ("rust", "clippy"):
        group_lines = [_entry(c) for c in adopt if c.group == group]
        if group_lines:
            text = _insert_entries(text, f"{prefix}.{group}", group_lines)
    root_manifest.write_text(text, encoding="utf-8")
    notes.append(f"{root_manifest.name}: [{prefix}.*] += " + ", ".join(c.name for c in adopt))
    if is_ws:
        for man in members:
            inherit = tomllib.loads(man.read_text(encoding="utf-8")).get("lints")
            if inherit is None:
                body = man.read_text(encoding="utf-8")
                man.write_text(_insert_entries(body, "lints", ["workspace = true"]), encoding="utf-8")
                notes.append(f"{man.parent.name}/Cargo.toml: [lints] workspace = true")
            elif inherit.get("workspace") is not True:
                notes.append(f"WARNING {man}: has its own [lints] table - workspace lints do NOT apply there")
    keys = [c.test_allow for c in adopt if c.test_allow]
    if keys:
        existing = clippy_toml.read_text(encoding="utf-8") if clippy_toml.is_file() else ""
        have = tomllib.loads(existing) if existing else {}
        add = [f"{k} = true" for k in keys if k not in have]
        if add:
            clippy_toml.write_text(existing.rstrip() + ("\n" if existing else "")
                                   + "# tests may unwrap/expect/panic/dbg (assert! panics too)\n"
                                   + "\n".join(add) + "\n", encoding="utf-8")
            notes.append(f"{clippy_toml.name}: " + ", ".join(add))
    return notes


def run(root: Path, *, do_apply: bool, exclude: set[str], out=print, as_json: bool = False) -> int:
    meta = metadata(root)
    root_manifest, members, is_ws = layout(meta)
    ws_root = root_manifest.parent
    configured = configured_levels(root_manifest, is_ws)
    cands = [c for c in CANDIDATES if c.name not in exclude]
    cfg = gates.load_gate_config(ws_root)
    measured = measure(ws_root, cands, list(cfg.get("features") or []))
    for code, m in measured.items():
        m.configured = configured.get(code)
    adopt = [m.cand for m in measured.values() if m.adoptable]
    if as_json:
        out(json.dumps([{"lint": m.cand.code, "level": m.cand.level, "violations": m.count,
                         "samples": m.samples, "configured": m.configured, "adoptable": m.adoptable}
                        for m in measured.values()], indent=2))
    else:
        out(f"{'lint':<36} {'level':<7} {'now':>5}  status")
        for m in measured.values():
            if m.configured:
                status = f"already configured ({m.configured})"
            elif m.count == 0:
                status = "ADOPT - code already complies" + ("" if do_apply else " (use --apply)")
            else:
                status = f"{m.count} violation(s), e.g. {', '.join(m.samples)} - clean up, then rerun"
            out(f"{m.cand.code:<36} {m.cand.level:<7} {m.count:>5}  {status}")
            if m.count == 0 and not m.configured:
                out(f"{'':<36} {'':<7} {'':>5}  why: {m.cand.why}")
    if do_apply:
        notes = apply(root_manifest, members, is_ws, adopt, ws_root / "clippy.toml")
        for n in notes:
            out("  " + n)
        if adopt:
            res = gates.GateRunner(ws_root, cfg).clippy()
            out("clippy -D warnings after apply: " + ("PASS" if res.ok else "FAIL\n" + res.tail))
            return 0 if res.ok else 1
        out("nothing to adopt")
    return 0
