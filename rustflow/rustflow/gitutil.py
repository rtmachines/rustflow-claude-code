"""Git helpers: isolation (branch/worktree), tree snapshots, diffs, changed lines, risk signals."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rustflow import gates

RUST_SPECS = ("*.rs", "*Cargo.toml", "Cargo.lock", "*rust-toolchain*", "*clippy.toml", "*rustfmt.toml")
IGNORED_PREFIXES = (".rustflow/runs/", ".rustflow/cache/", ".rustflow/wt/", ".rustflow/.gitignore",
                    ".rustflow/__pycache__/")
FALLBACK_IDENTITY = ["-c", "user.name=rustflow", "-c", "user.email=rustflow@localhost"]


class GitError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    r = gates._git(root, *args, env=env)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def toplevel(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve()


def is_repo(path: Path) -> bool:
    try:
        return gates._git(path, "rev-parse", "--is-inside-work-tree").returncode == 0
    except FileNotFoundError:
        return False


def rev_parse(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def head_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def dirty_files(root: Path) -> list[str]:
    out = git(root, "status", "--porcelain", "--untracked-files=all")
    files = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if not path.startswith(IGNORED_PREFIXES):
            files.append(path)
    return files


def snapshot(root: Path) -> str:
    """Tree sha of the current working tree (tracked + untracked, honoring .gitignore)."""
    fp = gates.tree_fingerprint(root)
    if not fp:
        raise GitError("cannot snapshot working tree")
    return fp


def tree_of(root: Path, commit: str) -> str:
    return git(root, "rev-parse", f"{commit}^{{tree}}").strip()


def create_branch(root: Path, name: str) -> None:
    git(root, "switch", "-c", name)


def add_worktree(root: Path, path: Path, branch: str, base: str) -> None:
    git(root, "worktree", "add", "-b", branch, str(path), base)


def checkout_worktree(root: Path, path: Path, branch: str) -> None:
    """A worktree for an EXISTING branch (re-created after cleanup, e.g. for `resume --extra-rounds`)."""
    git(root, "worktree", "add", str(path), branch)


def remove_worktree(root: Path, path: Path) -> None:
    git(root, "worktree", "remove", "--force", str(path), check=False)


def checkpoint(root: Path, message: str) -> str | None:
    """Commit everything (except rustflow runtime dirs). Returns new sha or None if nothing changed."""
    git(root, "add", "-A", "--", ".", *gates.runtime_excludes(root))
    if not git(root, "diff", "--cached", "--name-only").strip():
        return None
    has_identity = all(gates._git(root, "config", key).stdout.strip() for key in ("user.email", "user.name"))
    ident = [] if has_identity else FALLBACK_IDENTITY
    # Checkpoints live on a throwaway work branch: no signing prompts, no pre-commit hooks.
    r = gates._git(root, *ident, "-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-m", message)
    if r.returncode != 0:
        raise GitError(f"checkpoint commit failed: {r.stderr.strip()}")
    return git(root, "rev-parse", "HEAD").strip()


def diff(root: Path, a: str, b: str, *, stat: bool = False, paths: tuple[str, ...] = ()) -> str:
    args = ["diff", "--no-color", "--no-ext-diff"]
    if stat:
        args.append("--stat=160")
    else:
        args.append("-U5")
    args += [a, b]
    if paths:
        args += ["--", *paths]
    return git(root, *args)


def changed_files(root: Path, a: str, b: str) -> list[str]:
    return [l for l in git(root, "diff", "--name-only", a, b).splitlines() if l.strip()]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_line_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """{new_path: [(start, end), ...]} of added/modified lines in the new version."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    new_line = 0
    run_start: int | None = None

    def close_run(end_line: int) -> None:
        nonlocal run_start
        if current and run_start is not None:
            ranges.setdefault(current, []).append((run_start, end_line))
        run_start = None

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            close_run(new_line - 1)
            path = line[4:].strip()
            current = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            close_run(new_line - 1)
            continue
        m = _HUNK.match(line)
        if m:
            close_run(new_line - 1)
            new_line = int(m.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+"):
            if run_start is None:
                run_start = new_line
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            close_run(new_line - 1)
            new_line += 1
    close_run(new_line - 1)
    return ranges


RiskSignals = gates.RiskSignals
risk_signals = gates.risk_signals


def ensure_gitignore(root: Path, entries: list[str]) -> bool:
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = {l.strip() for l in text.splitlines()}
    missing = [e for e in entries if e not in lines and e.rstrip("/") not in lines and "/" + e not in lines]
    if not missing:
        return False
    with path.open("a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(missing) + "\n")
    return True


def run_ok(argv: list[str], cwd: Path) -> bool:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True).returncode == 0
    except OSError:
        return False
