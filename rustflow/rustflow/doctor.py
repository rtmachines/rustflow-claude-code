"""`rustflow doctor`: verify tools, CLIs, models/efforts, config and installed files."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rustflow import gitutil, procutil
from rustflow.config import ALL_ROLES, DIFFICULTIES, PACKAGE_DIR, AgentSpec, Config, ConfigError
from rustflow.installer import SKILL_DIRS, SKILLS


class Doctor:
    def __init__(self, repo: Path, cfg: Config | None, cfg_error: str | None = None, out=print):
        self.repo = repo
        self.cfg = cfg
        self.cfg_error = cfg_error
        self.out = out
        self.fails = 0
        self.warns = 0

    def line(self, status: str, what: str, detail: str = "") -> None:
        if status == "FAIL":
            self.fails += 1
        elif status == "WARN":
            self.warns += 1
        self.out(f"  [{status:<4}] {what}" + (f" - {detail}" if detail else ""))

    def tool(self, label: str, argv: list[str], required: bool, hint: str = "") -> str | None:
        try:
            full = procutil.resolve_command(argv[0]) + argv[1:]
        except procutil.CommandNotFound:
            self.line("FAIL" if required else "WARN", label, "not found" + (f" ({hint})" if hint else ""))
            return None
        r = procutil.run(full, timeout_s=60)
        if r.returncode != 0:
            first = ((r.stderr or r.stdout).strip().splitlines() or ["failed"])[0][:120]
            self.line("FAIL" if required else "WARN", label, first + (f" ({hint})" if hint else ""))
            return None
        first = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "ok"
        self.line("OK", label, first[:100])
        return r.stdout

    def run(self) -> int:
        self.out("rustflow doctor")
        v = sys.version_info
        self.line("OK" if v >= (3, 11) else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro} (needs >= 3.11)")
        self.line("OK" if gitutil.is_repo(self.repo) else "FAIL", "git repository", str(self.repo))
        self.line("OK" if (self.repo / "Cargo.toml").is_file() else "WARN", "Cargo.toml")
        self.tool("cargo", ["cargo", "--version"], True)
        self.tool("rustfmt", ["cargo", "fmt", "--version"], True, "rustup component add rustfmt")
        self.tool("clippy", ["cargo", "clippy", "--version"], True, "rustup component add clippy")
        self.tool("rust-analyzer", ["rust-analyzer", "--version"], False, "rustup component add rust-analyzer")
        self.tool("cargo-nextest", ["cargo", "nextest", "--version"], False, "cargo install --locked cargo-nextest")
        self.tool("cargo-deny", ["cargo", "deny", "--version"], False, "cargo install --locked cargo-deny")

        if self.cfg_error or self.cfg is None:
            self.line("FAIL", "rustflow.toml", self.cfg_error or "not loaded")
            return self.finish()
        self.line("OK", "rustflow.toml", str(self.cfg.path or "defaults (no rustflow.toml)"))
        used = self.used_specs()
        providers = {s.provider for s in used}

        if "claude" in providers:
            bin_ = self.cfg.get("providers.claude.bin", "claude")
            if self.tool("claude CLI", [bin_, "--version"], True, "npm i -g @anthropic-ai/claude-code"):
                try:
                    argv = procutil.resolve_command(bin_)
                    self.line("OK", "claude argv", " ".join(argv))
                    help_text = procutil.run(argv + ["--help"], timeout_s=60).stdout
                    pc = self.cfg.section("providers.claude")
                    # Only flags listed in `--help` (e.g. --append-system-prompt-file works but is hidden).
                    needed = ["--effort", "--json-schema", "--permission-prompts", "--max-budget-usd"]
                    needed += ["--exclude-dynamic-system-prompt-sections"] if pc.get(
                        "exclude_dynamic_system_prompt", True) else []
                    needed += ["--strict-mcp-config"] if pc.get("strict_mcp", True) else []
                    needed += ["--setting-sources"] if pc.get("setting_sources") else []
                    needed += ["--tools"] if pc.get("tools_read") or pc.get("tools_write") else []
                    needed += ["--fallback-model"] if pc.get("fallback_model") else []
                    missing = [f for f in needed if f not in help_text]
                    self.line("OK" if not missing else "FAIL", "claude flags",
                              "all present" if not missing else f"missing {missing} - update Claude Code")
                except procutil.CommandNotFound:
                    pass
                self.check_user_settings()
        if "codex" in providers:
            bin_ = self.cfg.get("providers.codex.bin", "codex")
            if self.tool("codex CLI", [bin_, "--version"], True, "npm i -g @openai/codex"):
                self.check_codex_models(bin_, [s for s in used if s.provider == "codex"])

        from rustflow import skills as sk
        for role in ("planner", "implementer", "reviewer", "fixer"):
            for prov in sorted({s.provider for s in used}):
                try:
                    names = [x.parent.name for x in sk.resolve(self.cfg.section("skills"), role, self.repo, prov)]
                    if names:
                        self.line("OK", f"always-on skills {role}/{prov}", ", ".join(names))
                except sk.SkillError as exc:
                    self.line("FAIL", f"always-on skills {role}/{prov}", str(exc))
        for skill in SKILLS:
            for base in SKILL_DIRS:
                p = self.repo / base / skill / "SKILL.md"
                if not p.is_file():
                    self.line("WARN", f"skill {base}/{skill}", "missing (rustflow init)")
        for name in ("gates.py", "hooks.py", "integrity.py"):
            inst = self.repo / ".rustflow" / name
            if not inst.is_file():
                self.line("FAIL", f".rustflow/{name}", "missing - run `rustflow init`")
            elif inst.read_bytes() != (PACKAGE_DIR / name).read_bytes():
                self.line("WARN", f".rustflow/{name}", "differs from installed rustflow - rerun `rustflow init`")
            else:
                self.line("OK", f".rustflow/{name}")
        self.check_config_tracked()
        settings = self.repo / ".claude" / "settings.json"
        local = self.repo / ".claude" / "settings.local.json"
        found = False
        for s in (settings, local):
            if s.is_file():
                try:
                    found = found or ".rustflow/hooks.py" in s.read_text(encoding="utf-8")
                except OSError:
                    pass
        self.line("OK" if found else "WARN", "Claude Code hooks registered", "" if found else "run `rustflow init`")
        self.check_hook_interpreter(settings, local)
        self.check_trust()
        self.check_foreign_gates(settings, local)
        for rel in (".claude/skills/rustflow-pr-review/SKILL.md", ".claude/agents/rustflow-reviewer.md"):
            if not (self.repo / rel).is_file():
                self.line("WARN", rel, "missing - interactive /rustflow-pr-review unavailable (rustflow init)")
        if self.cfg.get("roles.auditor.mode") == "tob":
            s_all = "".join(p.read_text(encoding="utf-8") for p in (settings, local) if p.is_file())
            if "rust-review@trailofbits" not in s_all:
                self.line("FAIL", "auditor.mode = tob", "Trail of Bits plugin not enabled - `rustflow init --with-plugins`")
        if not (self.repo / "REVIEW.md").is_file():
            self.line("WARN", "REVIEW.md", "missing")
        return self.finish()

    def check_config_tracked(self) -> None:
        """Gate integrity reads the policy from the base commit; an untracked rustflow.toml means default [gates]."""
        if not (self.repo / "rustflow.toml").is_file():
            return
        r = procutil.run(["git", "ls-files", "--error-unmatch", "rustflow.toml"], cwd=self.repo, timeout_s=30)
        if r.returncode != 0:
            self.line("WARN", "rustflow.toml", "not committed - gate-integrity bases use the default [gates] "
                      "(commit rustflow.toml)")

    def check_user_settings(self) -> None:
        """User-level hooks (rtk, personal gates) and ~/.claude/CLAUDE.md also run inside rustflow agents."""
        if self.cfg is None or self.cfg.get("providers.claude.setting_sources"):
            return
        home = Path.home() / ".claude"
        try:
            hooks = "\"hooks\"" in (home / "settings.json").read_text(encoding="utf-8")
        except OSError:
            hooks = False
        personal = (home / "CLAUDE.md").is_file()
        if hooks or personal:
            what = " and ".join(x for x, on in (("hooks in ~/.claude/settings.json", hooks),
                                                 ("~/.claude/CLAUDE.md", personal)) if on)
            self.line("INFO", "user-level Claude config", f"{what} also apply to rustflow agents; to isolate them set "
                      "[providers.claude] setting_sources = \"project,local\" (not with apiKeyHelper/env auth in "
                      "~/.claude/settings.json)")

    def check_trust(self) -> None:
        """Until the folder is trusted, Claude Code ignores the project's permissions.allow (it says so on
        stderr: "this workspace has not been trusted") and agent-frontmatter hooks - `claude -p` then denies
        mcp__rustflow / Skill(rustflow-*) with nobody to ask."""
        cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        path = Path(cfg_dir) / ".claude.json" if cfg_dir else Path.home() / ".claude.json"
        try:
            projects = json.loads(path.read_text(encoding="utf-8")).get("projects") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            self.line("INFO", "folder trust", f"cannot read {path}")
            return
        repo = self.repo.resolve()
        if (projects.get(str(repo)) or {}).get("hasTrustDialogAccepted"):
            self.line("OK", "folder trust", "Claude Code trusts this folder")
            return
        parent = next((d for d in repo.parents if (projects.get(str(d)) or {}).get("hasTrustDialogAccepted")), None)
        if parent is not None:
            self.line("INFO", "folder trust", f"not recorded for this folder, but {parent} is trusted")
            return
        self.line("WARN", "folder trust", "Claude Code has not trusted this folder: it ignores the project's "
                  "permissions.allow (mcp__rustflow, Skill(rustflow-*), ...) and agent-frontmatter hooks, so "
                  "`claude -p` denies them. Open Claude Code here once and accept the trust dialog.")

    def check_foreign_gates(self, *paths: Path) -> None:
        """Other Stop-hook gates (e.g. rust-review-kit's rust-gate.sh) double cargo runs per turn."""
        for s in paths:
            try:
                text = s.read_text(encoding="utf-8")
            except OSError:
                continue
            if "rust-gate.sh" in text:
                self.line("WARN", "second gate hook", f"{s.name} also registers rust-gate.sh (rust-review-kit): "
                          "interactive turns run cargo twice. Under `rustflow run` it is disabled via "
                          "RUST_GATE_SKIP=1; for interactive use keep one Stop gate (remove the kit's Stop/commit "
                          "hooks or rustflow's).")
            if "review-router.sh" in text:
                try:
                    router = (self.repo / ".claude" / "hooks" / "review-router.sh").read_text(encoding="utf-8")
                except OSError:
                    router = ""
                if "RUSTFLOW_ROLE" not in router:
                    self.line("WARN", "review-router.sh", "unpatched rust-review-kit router injects 'reviews MUST go "
                              "through rust-pr-review' into rustflow reviewers; apply the kit patch")

    def check_hook_interpreter(self, *paths: Path) -> None:
        """Run the interpreter the hooks are registered with: an old python3 silently disables hooks."""
        for s in paths:
            try:
                data = json.loads(s.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for group in (data.get("hooks") or {}).get("Stop", []):
                for h in group.get("hooks", []):
                    args = h.get("args") or []
                    if not any(".rustflow/hooks.py" in a for a in args):
                        continue
                    pre = [a for a in args[: next(i for i, a in enumerate(args) if ".rustflow/hooks.py" in a)]]
                    try:
                        argv = procutil.resolve_command(h["command"]) + pre
                    except procutil.CommandNotFound:
                        self.line("FAIL", "hook interpreter", f"'{h['command']}' not found")
                        return
                    r = procutil.run(argv + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                                     timeout_s=30)
                    ver = r.stdout.strip()
                    try:
                        ok = tuple(int(x) for x in ver.split(".")) >= (3, 9)
                    except ValueError:
                        ok = False
                    self.line("OK" if ok else "FAIL", "hook interpreter",
                              f"{' '.join(argv)} -> Python {ver or '?'}" + ("" if ok else " (needs >= 3.9; "
                              "use `rustflow init --python-cmd <path>`)"))
                    return

    def used_specs(self) -> list[AgentSpec]:
        assert self.cfg is not None
        specs: list[AgentSpec] = []
        for role in ALL_ROLES:
            if role == "auditor":
                if self.cfg.get("roles.auditor.spec"):
                    specs.append(AgentSpec.parse(self.cfg.get("roles.auditor.spec")))
                continue
            for d in DIFFICULTIES:
                specs.append(self.cfg.spec_for(role, d))
        if self.cfg.get("triage.mode") != "heuristic":
            specs.append(AgentSpec.parse(self.cfg.get("triage.model")))
        return specs

    def check_codex_models(self, bin_: str, specs: list[AgentSpec]) -> None:
        try:
            argv = procutil.resolve_command(bin_) + ["debug", "models"]
        except procutil.CommandNotFound:
            return
        r = procutil.run(argv, timeout_s=60)
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.line("WARN", "codex model catalog", "could not read `codex debug models`")
            return
        catalog: dict[str, list[str]] = {}
        for m in data.get("models", data) if isinstance(data, dict) else data:
            if isinstance(m, dict) and m.get("slug"):
                levels = [e.get("effort") if isinstance(e, dict) else e
                          for e in (m.get("supported_reasoning_levels") or [])]
                catalog[m["slug"]] = [x for x in levels if x]
        bad = []
        for s in {str(s): s for s in specs}.values():
            if s.model not in catalog:
                bad.append(f"{s.model} (unknown model)")
            elif catalog[s.model] and s.effort not in catalog[s.model]:
                bad.append(f"{s.model}:{s.effort} (supported: {', '.join(catalog[s.model])})")
        self.line("OK" if not bad else "FAIL", "codex models/efforts in config",
                  "all in catalog" if not bad else "; ".join(sorted(set(bad))))

    def finish(self) -> int:
        self.out(f"\n{self.fails} failure(s), {self.warns} warning(s)")
        return 1 if self.fails else 0


def load_cfg(repo: Path, explicit: Path | None) -> tuple[Config | None, str | None]:
    try:
        return Config.load(repo, explicit), None
    except ConfigError as exc:
        return None, str(exc)
