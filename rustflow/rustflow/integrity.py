#!/usr/bin/env python3
"""Gate integrity: detect changes that can weaken the quality gates (config, lints, silenced warnings, removed tests).

Standalone (stdlib only; Python 3.9+). `rustflow init` copies it to `.rustflow/integrity.py` next to `gates.py`.
`scan` compares a base revision with a tree and reports every gate-affecting change as a `GateChange`.
Fail-closed: any git or parse error becomes an `unknown` change, which blocks.
"""

from __future__ import annotations

import bisect
import fnmatch
import hashlib
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
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
    ".gitattributes",   # changes how diffs (and checkout eol) are seen
)
CARGO_CONFIG_RULE = "**/.cargo/config*"
RUSTFLOW_DIR_RULE = ".rustflow/**"
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


def protected_edit_rules() -> list[str]:
    """`--disallowedTools` rules denying Edit/Write on the fixed gate config paths and `.rustflow/**`.

    Each fixed path gets a root-anchored form and a `**/<path>` form (rules with a slash are anchored to
    the cwd, so workspace subdirectories need the second one); `.rustflow/**` is root-anchored only.
    """
    paths = []
    for p in CONFIG_PATTERNS:
        if not p.startswith(".cargo/config"):
            paths += [p, f"**/{p}"]
    paths += [CARGO_CONFIG_RULE, RUSTFLOW_DIR_RULE]
    return [f"{tool}({p})" for p in paths for tool in ("Edit", "Write")]


def _is_runtime(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in gates.RUNTIME_PATHS)


def is_config_path(path: str, extra: list[str]) -> bool:
    """True when `path` matches a config pattern on the path, its basename or any trailing sub-path."""
    parts = path.split("/")
    candidates = ["/".join(parts[i:]) for i in range(len(parts))]  # includes the basename
    return any(fnmatch.fnmatchcase(c, pat) for pat in (*CONFIG_PATTERNS, *extra) for c in candidates)


def _lines(text: str, keepends: bool = False) -> list[str]:
    """Split on `\\n` only, like git numbers lines (`str.splitlines` also splits on \\x0c, \\x85, U+2028, ...)."""
    parts = text.split("\n")
    if keepends:
        parts = [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    elif parts and parts[-1] == "":
        parts.pop()
    return parts


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


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def _parse_rs_diff(diff: str, positions: dict[str, list[int]] | None = None,
                   all_added: dict[str, set[int]] | None = None) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Added silencing attributes per file and net removed test attributes per file (comment lines ignored).
    When `positions` is given it receives, per file, the head line numbers (1-based) of the added silencing lines;
    `all_added` receives every added head line number per file."""
    silence: dict[str, list[str]] = {}
    removed: dict[str, int] = {}
    path, in_header, new_no = None, False, 0
    for line in _lines(diff):
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
                new_no = int(HUNK_RE.match(line).group(1))  # type: ignore[union-attr]
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                raise ValueError(f"cannot parse hunk header: {line}")
            new_no = int(m.group(1))
        elif line[:1] in "+-" and not line.startswith("\\"):
            if not path:  # undecodable header: refuse to skip changes silently
                raise ValueError("cannot attribute diff lines to a file")
            body = line[1:].strip()
            here = new_no
            if line[0] == "+":
                new_no += 1
                if all_added is not None:
                    all_added.setdefault(path, set()).add(here)
            if body.startswith("//"):
                continue
            if line[0] == "+" and SILENCE_RE.search(body):
                silence.setdefault(path, []).append(body)
                if positions is not None:
                    positions.setdefault(path, []).append(here)
            if TEST_RE.search(body):
                removed[path] = removed.get(path, 0) + (1 if line[0] == "-" else -1)
    return silence, removed


def _diff_u0(root: Path, base_rev: str, head_tree: str) -> subprocess.CompletedProcess:
    """The one `git diff -U0` over `*.rs` the scan and the proof both parse. Immune to user git config
    (`diff.external`, `diff.noprefix`, textconv) and to in-tree attributes (`-diff`, `binary`)."""
    return gates._git(root, "-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-textconv", "--text",
                      "--src-prefix=a/", "--dst-prefix=b/", "--no-renames", "--no-color", "-U0",
                      base_rev, head_tree, "--", "*.rs")


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
    d = _diff_u0(root, base_rev, head_tree)
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


# ── proof ────────────────────────────────────────────────────────────────────
@dataclass
class ProofResult:
    status: str      # passed | failed
    summary: str
    error: str = ""


_RAW_RE = re.compile(r'(?:br|cr|r)(#*)"')    # raw (byte / C) string: no escapes, closed by `"` + the same #s
_STR_RE = re.compile(r'[bc]?"')                # (byte / C) string with escapes
_CHAR_RE = re.compile(r"b?'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]*\}|[^\n])|[^\\'\n\r])'")  # else a lifetime
CODE_TOKENS = "{};"  # never inside a silencing attribute (allow / expect / ignore / cfg_attr) outside literals


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _skip_literal(text: str, i: int) -> int:
    """Index just past the string / char literal or comment starting at `i`; `i` itself when none starts there
    (a lifetime such as `'a` is no literal). Rust lexing: raw strings `r#"…"#` (any number of `#`, no escapes),
    byte / C strings `b"…"` `c"…"`, raw byte / C strings `br#"…"#` `cr#"…"#`, byte chars `b'…'`, char literals
    (`'a'`, `'\\n'`, `'\\u{..}'`) vs lifetimes, `//` comments (up to the newline) and nested `/* … */` comments.
    An unterminated literal or comment runs to the end of `text`."""
    n = len(text)
    c = text[i]
    if c == "/" and text.startswith("//", i):
        j = text.find("\n", i)
        return n if j < 0 else j
    if c == "/" and text.startswith("/*", i):
        depth, j = 1, i + 2
        while j < n and depth:
            if text.startswith("/*", j):
                depth, j = depth + 1, j + 2
            elif text.startswith("*/", j):
                depth, j = depth - 1, j + 2
            else:
                j += 1
        return j if not depth else n
    if c in "brc" and i > 0 and _is_ident_char(text[i - 1]):
        return i  # inside an identifier (`bar"x"`, `for'a'`): a prefix letter only starts a token
    m = _RAW_RE.match(text, i)
    if m:
        j = text.find('"' + m.group(1), m.end())
        return n if j < 0 else j + 1 + len(m.group(1))
    m = _STR_RE.match(text, i)
    if m:
        j = m.end()
        while j < n and text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return min(j + 1, n)
    m = _CHAR_RE.match(text, i) if c in "b'" else None
    return m.end() if m else i


def _attr_end(text: str, i: int) -> tuple[int, str]:
    """`i` is at `#`; index just past the matching `]` of `#[...]` / `#![...]` (-1 when it is no attribute) and,
    when the span holds code-like tokens, why: `{`, `}` or `;` outside literals and comments, or a literal / comment
    that crosses a line break and holds one of them (what a lexer miss swallowing code would look like)."""
    j = i + 1
    if text.startswith("!", j):
        j += 1
    if not text.startswith("[", j):
        return -1, ""
    depth, bad = 0, ""
    while j < len(text):
        k = _skip_literal(text, j)
        if k != j:
            chunk = text[j:k]
            if not bad and "\n" in chunk and any(t in chunk for t in CODE_TOKENS):
                bad = f"a literal or comment crossing a line break holds code tokens: {chunk[:60]!r}"
            j = k
            continue
        ch = text[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return j + 1, bad
        elif ch in CODE_TOKENS and not bad:
            bad = f"`{ch}` outside literals and comments"
        j += 1
    return -1, ""


def _strip(text: str, added: list[int], allowed: set[int] | None = None) -> tuple[str, list[int]]:
    """Cut the silencing attributes (`#[...]` / `#![...]`, bracket-counted, Rust-lexed) that start on the `added`
    lines (1-based line numbers in `text`, git numbering). The whole file is lexed, so a line inside a multi-line
    literal or comment holds no attribute. A line left empty disappears. Raises RuntimeError when a silencing
    attribute's span holds code tokens (it would swallow code). Also returns the added line numbers that still
    carry a silencing attribute afterwards (a tokenizer miss) and, when `allowed` (every added line) is given, the
    lines outside it that were cut (a span running into pre-existing code)."""
    wanted = set(added)
    lines = _lines(text, True)
    ends = list(itertools.accumulate(len(ln) for ln in lines))
    gone = bytearray(len(text))
    i, n = 0, len(text)
    while i < n:
        k = _skip_literal(text, i)
        if k != i:
            i = k
            continue
        if text[i] == "#" and bisect.bisect_right(ends, i) + 1 in wanted:
            e, bad = _attr_end(text, i)
            if e > 0:
                if SILENCE_RE.search(text[i:e]):
                    if bad:
                        line = bisect.bisect_right(ends, i) + 1
                        raise RuntimeError(f"the silencing attribute on line {line} would swallow code ({bad})")
                    while e < n and text[e] in " \t":
                        e += 1
                    for x in range(i, e):
                        gone[x] = 1
                i = e
                continue
        i += 1
    out, left, pos = [], [], 0
    for no, line in enumerate(lines, 1):
        kept = "".join(ch for x, ch in enumerate(line, pos) if not gone[x])
        pos += len(line)
        if no in wanted and SILENCE_RE.search(kept.strip()):
            left.append(no)
        if allowed is not None and kept != line and no not in allowed:
            left.append(no)
        if kept != line and not kept.strip():
            continue  # the line held only removed attributes (possibly continued from an earlier line)
        out.append(kept)
    return "".join(out), left


def strip_attributes(text: str, added: list[int]) -> str:
    return _strip(text, added)[0]


def _cache_base() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base)


def _wt_path(root: Path, head_tree: str) -> Path:
    """Deterministic (a crashed run is recovered) per-user location OUTSIDE the project: cargo merges
    `.cargo/config.toml` from every ancestor directory, so a worktree under `root/` would inherit the head config
    the proof removes, and a shared /tmp could be planted by anyone."""
    scope = hashlib.sha256(str(root).encode("utf-8", "replace")).hexdigest()[:12]
    return _cache_base() / "rustflow" / "wt" / scope / f"proof-{hashlib.sha256(head_tree.encode()).hexdigest()[:12]}"


def _private_dir(d: Path) -> None:
    """Create `d` (mode 0700) when missing; refuse a symlink, a foreign owner or group/other access (POSIX)."""
    if not d.exists() and not d.is_symlink():
        d.mkdir(mode=0o700)
    if os.name == "nt":
        return
    st = d.lstat()
    if d.is_symlink() or not d.is_dir():
        raise RuntimeError(f"{d} is a symlink or not a directory")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"{d} is not owned by the current user")
    if st.st_mode & 0o077:
        raise RuntimeError(f"{d} is accessible to other users (mode {st.st_mode & 0o777:o}, expected 700)")


def _prepare_wt_parent(wt: Path) -> None:
    """Secure `<cache>/rustflow/wt/<scope>`, and refuse a cargo config in any ancestor (cargo would merge it
    into the proof and the scan never sees it). `$CARGO_HOME` is the user's own config and is allowed."""
    wt.parents[3].mkdir(parents=True, exist_ok=True)
    for d in (wt.parents[2], wt.parents[1], wt.parent):
        _private_dir(d)
    home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo").resolve()
    for anc in wt.resolve().parents:  # cargo walks the physical directory, not the symlinked spelling
        cargo = anc / ".cargo"
        if cargo.resolve() == home:
            continue
        for name in ("config", "config.toml"):
            if (cargo / name).exists():
                raise RuntimeError(f"cargo config in an ancestor of the proof worktree: {cargo / name}")


def _added_attrs(root: Path, base_rev: str, head_tree: str) -> tuple[dict[str, list[int]], dict[str, set[int]]]:
    d = _diff_u0(root, base_rev, head_tree)
    if d.returncode != 0:
        raise RuntimeError((d.stderr or "").strip() or "git diff -U0 failed")
    positions: dict[str, list[int]] = {}
    all_added: dict[str, set[int]] = {}
    _parse_rs_diff(d.stdout, positions, all_added)
    return positions, all_added


def _silence_lines(text: str) -> int:
    return sum(1 for ln in _lines(text)
               if not ln.strip().startswith("//") and SILENCE_RE.search(ln.strip()))


def _strip_verified(text: str, positions: list[int], allowed: set[int], path: str) -> str:
    """Strip the added attributes and check that exactly those silencing lines are gone (a tokenizer miss such as
    an odd quote inside a block comment would otherwise leave the attribute in and let the proof pass)."""
    out, left = _strip(text, positions, allowed)
    if left or _silence_lines(out) != _silence_lines(text) - len(positions):
        raise RuntimeError(f"could not strip every added attribute in {path} exactly (unfinished or non-added lines: {left})")
    return out


def _git_ok(root: Path, *args: str, env: dict | None = None) -> str:
    r = gates._git(root, *args, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout or '').strip()}")
    return r.stdout


def _remove_worktree(root: Path, wt: Path) -> None:
    try:
        gates._git(root, "worktree", "remove", "--force", str(wt))
    except Exception:
        pass
    shutil.rmtree(wt, ignore_errors=True)
    try:
        gates._git(root, "worktree", "prune")
    except Exception:
        pass


def prove(root: Path, base_rev: str, head_tree: str, changes: list[GateChange], profile: str, cfg: dict,
          log_dir: Path | None = None) -> ProofResult:
    """Run the gates on `head_tree` with the base configuration restored and the added silencing attributes
    stripped, in a temporary worktree. Passing means the provable changes do not hide any failure.
    Fail-closed: any error or incomplete gate output is `failed`."""
    provable = [c for c in changes if c.provable]
    if not provable:
        return ProofResult("passed", "nothing to prove")
    root = root.resolve()
    wt = _wt_path(root, head_tree)
    had_target = "CARGO_TARGET_DIR" in os.environ
    saved_base = os.environ.get("RUSTFLOW_BASE")
    try:
        _prepare_wt_parent(wt)
        shutil.rmtree(wt, ignore_errors=True)
        gates._git(root, "worktree", "prune")
        env = {f"GIT_{who}_{f}": v for who in ("AUTHOR", "COMMITTER") for f, v in (("NAME", "rustflow"), ("EMAIL", "rustflow@localhost"))}
        commit = _git_ok(root, "commit-tree", head_tree, "-m", "proof", env=env).strip()
        _git_ok(root, "worktree", "add", "--detach", str(wt), commit)
        for c in provable:
            if c.kind == "config":
                base = gates._git(root, "show", f"{base_rev}:{c.path}")
                target = wt / c.path
                if base.returncode == 0:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base.stdout.encode("utf-8"))
                elif target.exists():
                    target.unlink()
        added, all_added = _added_attrs(root, base_rev, head_tree)
        for c in provable:
            if c.kind == "silence":
                f = wt / c.path
                if c.path not in added or not f.is_file():
                    raise RuntimeError(f"cannot locate the added attributes of {c.path}")
                with open(f, encoding="utf-8", newline="") as fh:  # no newline translation: line numbers must match git's
                    text = fh.read()
                with open(f, "w", encoding="utf-8", newline="") as fh:
                    fh.write(_strip_verified(text, added[c.path], all_added.get(c.path, set()), c.path))
        if not had_target:
            os.environ["CARGO_TARGET_DIR"] = str(root / "target")
        os.environ["RUSTFLOW_BASE"] = base_rev   # the semver gate compares against the base, not crates.io
        res = gates.run_gates(wt, profile, cfg=cfg, strict=(profile == "full"), use_cache=False,
                              log_dir=log_dir, fix_fmt=False)
    except Exception as e:
        return ProofResult("failed", "proof could not run", f"{type(e).__name__}: {e}")
    finally:
        if not had_target:
            os.environ.pop("CARGO_TARGET_DIR", None)
        if saved_base is None:
            os.environ.pop("RUSTFLOW_BASE", None)
        else:
            os.environ["RUSTFLOW_BASE"] = saved_base
        _remove_worktree(root, wt)
    incomplete = [s.name for s in res.steps if not s.output_complete]
    if incomplete:
        return ProofResult("failed", res.summary_line(), "incomplete gate output: " + ", ".join(incomplete))
    return ProofResult("passed" if res.ok else "failed", res.summary_line())


# ── state of the detected changes (pure functions; entries are plain dicts kept in the run state) ──────
_CARRIED = ("status", "accepted_by", "proof", "proof_tree", "votes", "justification")


def reconcile(prev: list[dict], changes: list[GateChange]) -> list[dict]:
    """New state entries for `changes`. Verdicts (proof, votes, justification) survive only for the same
    id AND digest; `fix_rounds` survives for the same id; changes that are gone disappear."""
    old = {e.get("id"): e for e in prev}
    out = []
    for c in changes:
        e = c.to_dict()
        p = old.get(c.id) or {}
        same = bool(p) and p.get("digest") == c.digest
        e.update(status="pending", accepted_by="", proof="", proof_tree="", votes={}, justification="",
                 fix_rounds=int(p.get("fix_rounds", 0) or 0))
        if same:
            for k in _CARRIED:
                if k in p:
                    e[k] = deepcopy(p[k])
        out.append(e)
    return out


def apply_proof(entries: list[dict], result: ProofResult, tree: str = "") -> None:
    """Record the proof outcome on every provable entry that has none yet."""
    for e in entries:
        if e.get("provable") and not e.get("proof"):
            e["proof"] = result.status
            e["proof_tree"] = tree


REVIEWERS = ("reviewer_a", "reviewer_b")


def apply_votes(entries: list[dict], reviewer: str, votes: list[dict]) -> None:
    """Record `reviewer`'s votes on existing entries that are neither provable nor unknown, bound to the
    entry's current digest. A new vote of the same role replaces the old one; a missing vote is no vote."""
    by_id = {e.get("id"): e for e in entries if not e.get("provable") and e.get("kind") != "unknown"}
    for v in votes or []:
        e = by_id.get(v.get("id"))
        if e is None or v.get("decision") not in ("accept", "reject"):
            continue
        e.setdefault("votes", {})[reviewer] = {"digest": e.get("digest", ""), "decision": v["decision"],
                                                "reason": str(v.get("reason", ""))}


def _consent(e: dict) -> bool:
    votes = e.get("votes") or {}
    return all((votes.get(r) or {}).get("digest") == e.get("digest")
               and (votes.get(r) or {}).get("decision") == "accept" for r in REVIEWERS)


def decide(entries: list[dict]) -> None:
    for e in entries:
        if e["kind"] == "unknown":
            e["status"], e["accepted_by"] = "blocked", ""
        elif e.get("provable"):
            if e.get("proof") == "passed":
                e["status"], e["accepted_by"] = "accepted", "proof"
            elif e.get("proof") == "failed":
                e["status"], e["accepted_by"] = "blocked", ""
            else:
                e["status"], e["accepted_by"] = "pending", ""
        elif _consent(e):   # unanimous reviewer consent for a change that cannot be proven
            e["status"], e["accepted_by"] = "accepted", "reviewers"
        else:
            e["status"], e["accepted_by"] = "pending", ""


def pending(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("status") != "accepted"]
