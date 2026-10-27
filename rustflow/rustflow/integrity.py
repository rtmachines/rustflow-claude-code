#!/usr/bin/env python3
"""Gate integrity: detect changes that can weaken the quality gates (config, lints, silenced warnings, removed tests).

Standalone (stdlib only; Python 3.9+). `rustflow init` copies it to `.rustflow/integrity.py` next to `gates.py`.
`scan` compares a base revision with a tree and reports every gate-affecting change as a `GateChange`.
Fail-closed: any git or parse error becomes an `unknown` change, which blocks.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.dont_write_bytecode = True  # no .rustflow/__pycache__ littering the working tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import gates  # type: ignore[import-not-found]   # .rustflow/gates.py (installed layout)
except ImportError:  # running from the package
    from rustflow import gates  # type: ignore[no-redef]

try:  # TOML parser: tomllib (3.11+), then tomli; None -> conservative line-based comparison
    import tomllib as _toml  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]

CONFIG_PATTERNS: tuple[str, ...] = (
    "rustflow.toml", ".cargo/config", ".cargo/config.toml", ".config/nextest.toml",
    "clippy.toml", ".clippy.toml", "rustfmt.toml", ".rustfmt.toml",
    "rust-toolchain", "rust-toolchain.toml", "deny.toml",
)
LINT_SECTIONS = ("lints", "workspace.lints", "profile")
SILENCE_RE = re.compile(r"#!?\[\s*(allow|expect)\s*\(|cfg_attr\s*\(.*\b(allow|expect)\s*\(|#\[\s*ignore\b")
TEST_RE = re.compile(r"#\[\s*(\w+::)*test\b")
SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?")
MAX_DETAIL = 300


@dataclass
class GateChange:
    id: str          # stable: "G-<kind>-<path>"
    path: str
    kind: str        # config | cargo-lints | silence | tests-removed | unknown
    detail: str
    digest: str      # sha256 of the head content (or of the added attribute lines / affected files)
    provable: bool   # kind in ("config", "silence")

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _change(kind: str, path: str, detail: str, digest: str) -> GateChange:
    return GateChange(f"G-{kind}-{path}", path, kind, detail[:MAX_DETAIL], digest, kind in ("config", "silence"))


def _unknown(path: str, detail: str) -> GateChange:
    return _change("unknown", path, detail, _sha(detail))


def _is_runtime(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in gates.RUNTIME_PATHS)


def is_config_path(path: str, extra: list[str]) -> bool:
    """True when `path` matches a config pattern on the path, its basename or any trailing sub-path."""
    parts = path.split("/")
    candidates = ["/".join(parts[i:]) for i in range(len(parts))]  # includes the basename
    return any(fnmatch.fnmatchcase(c, pat) for pat in (*CONFIG_PATTERNS, *extra) for c in candidates)


def _show(root: Path, rev: str, path: str) -> tuple[str | None, str]:
    r = gates._git(root, "show", f"{rev}:{path}")
    if r.returncode != 0:
        return None, (r.stderr or "").strip() or f"git show {rev}:{path} failed"
    return r.stdout, ""


def _lint_lines(text: str) -> list[str]:
    out, active = [], False
    for line in text.splitlines():
        s = line.strip()
        if m := SECTION_RE.match(s):
            name = m.group(1).replace(" ", "")
            active = any(name == sec or name.startswith(sec + ".") for sec in LINT_SECTIONS)
            if active:
                out.append(s)
        elif active and s and not s.startswith("#"):
            out.append(s)
    return out


def _lint_data(data: dict) -> list:
    ws = data.get("workspace")
    return [data.get("lints"), ws.get("lints") if isinstance(ws, dict) else None, data.get("profile")]


def _lints_changed(base: str, head: str) -> bool:
    if _toml is None:
        return _lint_lines(base) != _lint_lines(head)
    b = _lint_data(_toml.loads(base)) if base.strip() else [None, None, None]
    return b != _lint_data(_toml.loads(head))


def _scan_cargo(root: Path, base_rev: str, head_tree: str, path: str, status: str) -> GateChange | None:
    base = ""
    if not status.startswith("A"):
        text, err = _show(root, base_rev, path)
        if text is None:
            return _unknown(path, err)
        base = text
    head = ""
    if not status.startswith("D"):
        text, err = _show(root, head_tree, path)
        if text is None:
            return _unknown(path, err)
        head = text
    try:
        if _toml is not None and head.strip():
            _toml.loads(head)  # unparsable head -> unknown, even when base is unparsable too
        try:
            changed = _lints_changed(base, head)
        except ValueError:  # unparsable base: compare against nothing, so any lints in head count
            changed = _lints_changed("", head)
    except ValueError as e:  # TOMLDecodeError subclasses ValueError
        return _unknown(path, f"cannot parse {path}: {e}")
    if not changed:
        return None
    return _change("cargo-lints", path, "lints/profile settings changed", _sha(head) if head else "deleted")


_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}


def _unquote(name: str) -> str:
    """Decode a git C-quoted path (`"a/we\\"ird.rs"`); unquoted names pass through (minus a trailing tab)."""
    if not name.startswith('"'):
        return name.split("\t")[0]
    raw, i, body = bytearray(), 1, name
    while i < len(body) and body[i] != '"':
        c = body[i]
        if c != "\\":
            raw += c.encode("utf-8")
            i += 1
        elif body[i + 1 : i + 2].isdigit():
            raw.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            raw.append(_ESCAPES[body[i + 1]])
            i += 2
    return raw.decode("utf-8", "replace")


def _parse_rs_diff(diff: str) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Added silencing attributes per file and net removed test attributes per file (comment lines ignored)."""
    silence: dict[str, list[str]] = {}
    removed: dict[str, int] = {}
    path, in_header = None, False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            path, in_header = None, True
        elif in_header:
            name = _unquote(line[4:])
            if line.startswith("--- ") and name.startswith("a/"):
                path = name[2:]
            elif line.startswith("+++ ") and name.startswith("b/"):
                path = name[2:]
            elif line.startswith("@@"):
                in_header = False
        elif line[:1] in "+-" and not line.startswith("\\"):
            if not path:  # undecodable header: refuse to skip changes silently
                raise ValueError("cannot attribute diff lines to a file")
            body = line[1:].strip()
            if body.startswith("//"):
                continue
            if line[0] == "+" and SILENCE_RE.search(body):
                silence.setdefault(path, []).append(body)
            if TEST_RE.search(body):
                removed[path] = removed.get(path, 0) + (1 if line[0] == "-" else -1)
    return silence, removed


def _name_status(out: str) -> list[tuple[str, str]]:
    parts = out.split("\0")
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def scan(root: Path, base_rev: str, head_tree: str, cfg: dict) -> list[GateChange]:
    """Gate-affecting changes between `base_rev` and `head_tree`, sorted by id. Errors yield `unknown`."""
    try:
        return sorted(_scan(root, base_rev, head_tree, cfg), key=lambda c: c.id)
    except Exception as e:  # fail closed: a scan that cannot run must block, not pass
        return [_unknown("*", f"integrity scan failed: {type(e).__name__}: {e}")]


def _scan(root: Path, base_rev: str, head_tree: str, cfg: dict) -> list[GateChange]:
    if not head_tree:
        return [_unknown("*", "no head tree to compare (tree fingerprint unavailable)")]
    git = ("-c", "core.quotepath=false")
    r = gates._git(root, *git, "diff", "--no-renames", "--name-status", "-z", base_rev, head_tree, "--")
    if r.returncode != 0:
        return [_unknown("*", (r.stderr or "").strip() or "git diff failed")]
    extra = [str(p) for p in cfg.get("integrity_extra_paths") or []]
    out: list[GateChange] = []
    for status, path in _name_status(r.stdout):
        if _is_runtime(path):
            continue
        if is_config_path(path, extra):
            if status.startswith("D"):
                out.append(_change("config", path, "config file deleted", "deleted"))
                continue
            text, err = _show(root, head_tree, path)
            out.append(_change("config", path, "config file changed", _sha(text)) if text is not None
                       else _unknown(path, err))
        if path.rsplit("/", 1)[-1] == "Cargo.toml":
            if change := _scan_cargo(root, base_rev, head_tree, path, status):
                out.append(change)
    d = gates._git(root, *git, "diff", "--no-renames", "--no-color", "-U0", base_rev, head_tree, "--", "*.rs")
    if d.returncode != 0:
        return out + [_unknown("*.rs", (d.stderr or "").strip() or "git diff -U0 failed")]
    silence, removed = _parse_rs_diff(d.stdout)
    for path, lines in silence.items():
        if not _is_runtime(path):
            out.append(_change("silence", path, "added: " + "; ".join(lines), _sha("\n".join(sorted(lines)))))
    files = sorted(p for p, n in removed.items() if n > 0 and not _is_runtime(p))
    if sum(n for p, n in removed.items() if not _is_runtime(p)) > 0:
        out.append(_change("tests-removed", "*", "test attributes removed in: " + ", ".join(files), _sha("\n".join(files))))
    return out
