"""`rustflow init`: install config, gates/hooks scripts, skills, Claude settings, docs blocks, CI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from rustflow import gitutil
from rustflow.config import PACKAGE_DIR, TEMPLATES

SKILLS = ("rustflow-plan", "rustflow-implement", "rustflow-review", "rustflow-fix", "rustflow")
CLAUDE_ONLY = (("skills/rustflow-pr-review/SKILL.md", ".claude/skills/rustflow-pr-review/SKILL.md"),
               ("agents/rustflow-reviewer.md", ".claude/agents/rustflow-reviewer.md"))
STACKS = {"axum-sqlx": "rustflow-stack-axum-sqlx"}
TOB_MARKETPLACE = {"source": {"source": "github", "repo": "trailofbits/skills"}}
SKILL_DIRS = (".claude/skills", ".agents/skills")
BEGIN, END = "<!-- rustflow:begin", "<!-- rustflow:end -->"
HOOK_MARK = ".rustflow/hooks.py"
RUST_SKILLS_REPO = "https://github.com/rtmachines/rust-skills"
APOLLO_REPO = "https://github.com/apollographql/skills"


class InstallationError(RuntimeError):
    """Required installation input could not be obtained."""


def python_command(explicit: str | None = None) -> tuple[str, list[str]]:
    """Interpreter for hooks. Exec-form hooks on Windows need a real .exe (py.exe / python.exe).
    An explicit value that is an existing file is taken whole (paths with spaces)."""
    if explicit:
        if Path(explicit).exists():
            return explicit, []
        parts = explicit.split()
        return parts[0], parts[1:]
    from rustflow.config import python_invocation
    parts = python_invocation().split()
    return parts[0], parts[1:]


class Installer:
    def __init__(self, repo: Path, *, force: bool = False, python_cmd: str | None = None,
                 local_settings: bool = False, ci: bool = True, with_apollo: bool = False,
                 with_plugins: bool = False, router: bool = True, stack: str | None = None,
                 with_mcp: bool = False, with_rust_skills: bool = True, out=print):
        self.repo = repo.resolve()
        self.force = force
        self.py, self.py_args = python_command(python_cmd)
        self.local_settings = local_settings
        self.ci = ci
        self.with_apollo = with_apollo
        self.with_rust_skills = with_rust_skills
        self.with_plugins = with_plugins
        self.router = router
        if stack and stack not in STACKS:
            raise ValueError(f"unknown stack '{stack}' (available: {', '.join(STACKS)})")
        self.stack = stack
        self.with_mcp = with_mcp
        self.out = out

    def note(self, action: str, path: str) -> None:
        self.out(f"  {action:<9} {path}")

    @property
    def py_display(self) -> str:
        return " ".join([self.py, *self.py_args])

    def _render(self, text: str) -> str:
        """Interpreter, language and optional skills baked into Claude-only templates."""
        lang = "pl"
        skills_cfg: dict = {"all": ["auto"]}
        cfg = self.repo / "rustflow.toml"
        try:
            import tomllib
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            lang = data.get("project", {}).get("language", "pl")
            skills_cfg = data.get("skills", skills_cfg) or skills_cfg
        except Exception:
            pass
        from rustflow import skills as sk
        from rustflow.prompts import LANG_NAMES
        extra = [p.parent.name for p in sk.resolve(skills_cfg, "reviewer", self.repo, "claude", strict=False)]
        args = "".join(json.dumps(a) + ", " for a in self.py_args)
        return (text.replace("{{PY}}", self.py_display)
                    .replace("{{PYCMD}}", self.py.replace("\\", "/"))
                    .replace("{{PYARGS}}", args)
                    .replace("{{LANGUAGE}}", LANG_NAMES.get(lang, lang))
                    .replace("{{EXTRA_SKILLS}}", "".join(f"  - {n}\n" for n in extra))
                    .replace("{{EXTRA_SKILLS_NOTE}}", (" and the preloaded always-on skills: " + ", ".join(
                        f"`{n}`" for n in extra) + " (open referenced chapters/files only for topics the diff "
                        "touches)") if extra else ""))

    def _copy(self, src: Path, rel: str, *, managed: bool, render: bool = False) -> None:
        dst = self.repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        new = self._render(src.read_text(encoding="utf-8")).encode("utf-8") if render else src.read_bytes()
        if dst.exists():
            if dst.read_bytes() == new:
                self.note("ok", rel)
                return
            if not managed and not self.force:
                self.note("kept", rel + "  (differs; --force to overwrite)")
                return
            self.note("updated", rel)
        else:
            self.note("created", rel)
        dst.write_bytes(new)

    def run(self) -> None:
        self.out(f"rustflow init -> {self.repo}")
        if not (self.repo / "Cargo.toml").is_file():
            self.out("  warning: no Cargo.toml here (set [gates].cargo_dir if the workspace is in a subdir)")
        if not gitutil.is_repo(self.repo):
            self.out("  warning: not a git repository - `rustflow run` requires git")
        self._copy(TEMPLATES / "rustflow.toml", "rustflow.toml", managed=False)
        self._copy(PACKAGE_DIR / "process.py", ".rustflow/process.py", managed=True)
        self._copy(PACKAGE_DIR / "gates.py", ".rustflow/gates.py", managed=True)
        self._copy(PACKAGE_DIR / "hooks.py", ".rustflow/hooks.py", managed=True)
        self._copy(PACKAGE_DIR / "integrity.py", ".rustflow/integrity.py", managed=True)
        gi = self.repo / ".rustflow" / ".gitignore"
        wanted = ["runs/", "cache/", "wt/", "__pycache__/"]
        have = gi.read_text(encoding="utf-8").split() if gi.exists() else []
        if missing := [w for w in wanted if w not in have]:
            gi.write_text("\n".join(have + missing) + "\n", encoding="utf-8")
            self.note("updated" if have else "created", ".rustflow/.gitignore")
        if gitutil.ensure_gitignore(self.repo, ["target/", ".rust-review-results/"]):
            self.note("updated", ".gitignore (target/, .rust-review-results/)")
        if self.with_rust_skills:
            self._rust_skills()
        if self.with_apollo:
            self._apollo()  # before the agent template, which preloads it when present
        for skill in SKILLS:
            for base in SKILL_DIRS:
                self._copy(TEMPLATES / "skills" / skill / "SKILL.md", f"{base}/{skill}/SKILL.md", managed=False)
        agents_block = (TEMPLATES / "AGENTS.block.md").read_text(encoding="utf-8")
        if self.stack:
            skill = STACKS[self.stack]
            for base in SKILL_DIRS:
                self._copy(TEMPLATES / "stacks" / self.stack / "SKILL.md", f"{base}/{skill}/SKILL.md", managed=False)
        for src, dst in CLAUDE_ONLY:  # interactive review: forked read-only subagent (Claude Code only)
            self._copy(TEMPLATES / "claude" / src, dst, managed=True, render=True)
        self._copy(TEMPLATES / "REVIEW.md", "REVIEW.md", managed=False)
        self._copy(TEMPLATES / "nextest" / "nextest.toml", ".config/nextest.toml", managed=False)
        if self.stack:
            skill = STACKS[self.stack]
            agents_block = agents_block.replace(
                "<!-- rustflow:end -->",
                f"- Stack conventions, test patterns and pitfalls: skill `{skill}` - read it before touching "
                "handlers, queries, migrations or templates.\n<!-- rustflow:end -->")
        self._block("AGENTS.md", agents_block)
        self._block("CLAUDE.md", (TEMPLATES / "CLAUDE.block.md").read_text(encoding="utf-8"))
        self._settings()
        if self.with_mcp:
            self._mcp()
        if self.ci:
            ci_src = (TEMPLATES / "stacks" / self.stack / "rustflow-gates.yml"
                      if self.stack and (TEMPLATES / "stacks" / self.stack / "rustflow-gates.yml").is_file()
                      else TEMPLATES / "github" / "rustflow-gates.yml")
            self._copy(ci_src, ".github/workflows/rustflow-gates.yml", managed=False)
        self.out("")
        self.out("Next: `rustflow doctor`, then commit the files above (hooks/skills must be committed for")
        self.out("worktree isolation and for your team). In Claude Code run /hooks to confirm the hooks.")

    def _block(self, name: str, block: str) -> None:
        path = self.repo / name
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
        if pattern.search(text):
            new = pattern.sub(lambda _m: block if block.endswith("\n") else block + "\n", text)
            action = "ok" if new == text else "updated"
        else:
            new = (text.rstrip() + "\n\n" if text.strip() else "") + block
            action = "updated" if text else "created"
        if new != text:
            path.write_text(new, encoding="utf-8")
        self.note(action, f"{name} (rustflow block)")

    def hook_entry(self, sub: str, timeout: int, status: str | None = None) -> dict:
        entry = {"type": "command", "command": self.py,
                 "args": [*self.py_args, "${CLAUDE_PROJECT_DIR}/.rustflow/hooks.py", sub], "timeout": timeout}
        if status:
            entry["statusMessage"] = status
        return entry

    def _settings(self) -> None:
        rel = ".claude/settings.local.json" if self.local_settings else ".claude/settings.json"
        path = self.repo / rel
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.note("SKIPPED", f"{rel}: invalid JSON ({exc}) - fix it and rerun init")
                return
        before = json.dumps(data, sort_keys=True)
        data.setdefault("$schema", "https://json.schemastore.org/claude-code-settings.json")
        plugins = data.setdefault("enabledPlugins", {})
        plugins.setdefault("rust-analyzer-lsp@claude-plugins-official", True)
        perms = data.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        for rule in ("Bash(rustflow *)", "Bash(python .rustflow/gates.py *)", "Bash(python3 .rustflow/gates.py *)",
                     "Bash(py -3 .rustflow/gates.py *)", "Bash(cargo check *)", "Bash(cargo clippy *)",
                     "Bash(cargo test *)", "Bash(cargo nextest *)", "Bash(cargo fmt *)"):
            if rule not in allow:
                allow.append(rule)
        skill_names = [*SKILLS, "rustflow-pr-review", *STACKS.values()]
        for name in skill_names:
            for rule in (f"Skill({name})", f"Skill({name} *)"):
                if rule not in allow:
                    allow.append(rule)
        deny = perms.setdefault("deny", [])
        for rule in ("Bash(git push --force *)", "Bash(git push -f *)", "Bash(cargo publish *)",
                     "Bash(git commit --no-verify *)"):
            if rule not in deny:
                deny.append(rule)
        hooks = data.setdefault("hooks", {})
        wanted = {
            "PostToolUse": {"matcher": "Edit|Write|MultiEdit",
                            "hooks": [self.hook_entry("post-edit", 300, "cargo fmt + cargo check")]},
            "Stop": {"hooks": [self.hook_entry("stop", 1800, "rustflow gates: clippy + tests")]},
            "PreToolUse": {"matcher": "Bash|PowerShell", "hooks": [self.hook_entry("guard-bash", 1800)]},
        }
        guard_edit = {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [self.hook_entry("guard-edit", 30)]}
        if self.router:
            wanted["UserPromptSubmit"] = {"hooks": [self.hook_entry("route-review", 30)]}
        if self.with_mcp:
            if "mcp__rustflow" not in allow:
                allow.append("mcp__rustflow")                 # all rustflow MCP tools without prompts
            enabled = data.setdefault("enabledMcpjsonServers", [])
            if "rustflow" not in enabled:
                enabled.append("rustflow")                    # approve the project .mcp.json server
        if self.with_plugins:
            data.setdefault("extraKnownMarketplaces", {}).setdefault("trailofbits", TOB_MARKETPLACE)
            plugins["rust-review@trailofbits"] = True
        for event, entry in wanted.items():
            groups = [g for g in hooks.get(event, [])
                      if not any(HOOK_MARK in " ".join([h.get("command", ""), *h.get("args", [])])
                                 for h in g.get("hooks", []))]
            groups.append(entry)
            if event == "PreToolUse":
                groups.append(guard_edit)
            hooks[event] = groups
        if json.dumps(data, sort_keys=True) == before:
            self.note("ok", rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.note("updated" if existed else "created", f"{rel} (hooks via `{self.py} {' '.join(self.py_args)}`)")

    def mcp_command(self) -> tuple[str, list[str]]:
        """`rustflow mcp` if the console script is on PATH (a real .exe on Windows), else `<python> -m rustflow mcp`."""
        if shutil.which("rustflow"):
            return "rustflow", ["mcp"]
        return self.py, [*self.py_args, "-m", "rustflow", "mcp"]

    def _mcp(self) -> None:
        path = self.repo / ".mcp.json"
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.note("SKIPPED", f".mcp.json: invalid JSON ({exc})")
                return
        cmd, args = self.mcp_command()
        servers = data.setdefault("mcpServers", {})
        entry = {"command": cmd, "args": args}
        if servers.get("rustflow") == entry:
            self.note("ok", ".mcp.json")
            return
        servers["rustflow"] = entry
        existed = path.exists()
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.note("updated" if existed else "created", f".mcp.json (rustflow MCP server: {cmd} {' '.join(args)})")

    def _rust_skills(self) -> None:
        destinations = [self.repo / base / "rust-skills" for base in SKILL_DIRS]
        if not self.force and all((dst / "SKILL.md").is_file() for dst in destinations):
            for dst in destinations:
                self.note("kept", str(dst.relative_to(self.repo)))
            return
        with tempfile.TemporaryDirectory(prefix="rustflow-skills-") as tmp:
            src = Path(tmp) / "repo"
            try:
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", RUST_SKILLS_REPO, str(src)],
                    capture_output=True, text=True, timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise InstallationError(f"cannot install rust-skills: {exc}; use --no-rust-skills for offline init") from exc
            if result.returncode != 0 or not (src / "SKILL.md").is_file() or not (src / "rules").is_dir():
                raise InstallationError("cannot install rust-skills: "
                                   + (result.stderr.strip()[:200] or "missing SKILL.md or rules/")
                                   + "; use --no-rust-skills for offline init")
            for dst in destinations:
                if dst.exists() and not self.force:
                    self.note("kept", str(dst.relative_to(self.repo)) + " (--force to overwrite)")
                    continue
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
                self.note("created", f"{dst.relative_to(self.repo)} (rtmachines/rust-skills, local copy)")

    def _apollo(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rustflow-apollo-"))
        try:
            r = subprocess.run(["git", "clone", "--depth", "1", APOLLO_REPO, str(tmp / "repo")],
                               capture_output=True, text=True)
            src = tmp / "repo" / "skills" / "rust-best-practices"
            if r.returncode != 0 or not src.is_dir():
                self.note("FAILED", f"Apollo rust-best-practices ({r.stderr.strip()[:200] or 'not found'})")
                return
            for base in SKILL_DIRS:
                dst = self.repo / base / "rust-best-practices"
                if dst.exists() and not self.force:
                    self.note("kept", f"{base}/rust-best-practices")
                    continue
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
                self.note("created", f"{base}/rust-best-practices (Apollo, pinned copy)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
