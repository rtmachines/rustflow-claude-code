"""Configuration: defaults from templates/rustflow.toml deep-merged with the project's rustflow.toml."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_DIR / "templates"
DEFAULT_CONFIG_PATH = TEMPLATES / "rustflow.toml"
CONFIG_NAME = "rustflow.toml"

PROVIDERS = ("claude", "codex")
DIFFICULTIES = ("easy", "medium", "hard")
SEVERITIES = ("P0", "P1", "P2", "P3")
CONFIDENCES = ("low", "medium", "high")
REVIEW_ROLES = ("reviewer_a", "reviewer_b")
ALL_ROLES = ("planner", "implementer", "reviewer_a", "reviewer_b", "fixer", "auditor")
LENSES = ("correctness", "regressions", "design", "audit")   # keys of prompts.LENSES


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    provider: str
    model: str
    effort: str

    @classmethod
    def parse(cls, text: str) -> "AgentSpec":
        parts = [p.strip() for p in text.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ConfigError(f"agent spec '{text}' must look like 'provider:model:effort'")
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}:{self.effort}"


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_defaults() -> dict:
    with DEFAULT_CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


class Config:
    """Validated, merged configuration with helpers for routing."""

    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        self.validate()

    # ── loading ──────────────────────────────────────────────────────────
    @classmethod
    def load(cls, repo: Path, explicit: Path | None = None) -> "Config":
        defaults = load_defaults()
        path = explicit or (repo / CONFIG_NAME)
        if path.is_file():
            with path.open("rb") as fh:
                try:
                    user = tomllib.load(fh)
                except tomllib.TOMLDecodeError as exc:
                    raise ConfigError(f"{path}: {exc}") from exc
            return cls(deep_merge(defaults, user), path)
        if explicit:
            raise ConfigError(f"config file not found: {explicit}")
        return cls(defaults, None)

    # ── access ───────────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    # ── validation ───────────────────────────────────────────────────────
    def check_spec(self, spec: AgentSpec, where: str) -> None:
        if spec.provider not in PROVIDERS:
            raise ConfigError(f"{where}: provider '{spec.provider}' not allowed (use one of {PROVIDERS})")
        models = self.get(f"models.{spec.provider}", [])
        if "*" not in models and spec.model not in models:
            raise ConfigError(
                f"{where}: model '{spec.model}' is not on the [models].{spec.provider} allowlist {models}"
            )
        efforts = self.get(f"efforts.{spec.provider}", [])
        if spec.effort not in efforts:
            raise ConfigError(
                f"{where}: effort '{spec.effort}' is not on the [efforts].{spec.provider} allowlist {efforts}"
            )

    def validate(self) -> None:
        from rustflow.budget import DEFAULT_LIMITS, validate_limits
        try:
            validate_limits({k: self.get(("process." if k == "max_output_bytes" else "budget.") + k, v)
                             for k, v in DEFAULT_LIMITS.items()})
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        for role in ALL_ROLES:
            if role == "auditor":
                spec = self.get("roles.auditor.spec", "")
                mode = self.get("roles.auditor.mode", "llm")
                if mode not in ("llm", "tob"):
                    raise ConfigError("roles.auditor.mode must be llm | tob")
                if mode == "tob" and not spec:
                    raise ConfigError("roles.auditor.mode = 'tob' needs roles.auditor.spec (a claude model)")
                if spec:
                    parsed = AgentSpec.parse(spec)
                    self.check_spec(parsed, "roles.auditor.spec")
                    if mode == "tob" and parsed.provider != "claude":
                        raise ConfigError("roles.auditor.mode = 'tob' runs a Claude Code plugin: spec must be claude:*")
                continue
            sect = self.section(f"roles.{role}")
            for diff in DIFFICULTIES:
                if diff not in sect:
                    raise ConfigError(f"roles.{role}.{diff} is missing")
                self.check_spec(AgentSpec.parse(sect[diff]), f"roles.{role}.{diff}")
        if self.get("triage.mode") not in ("heuristic", "llm", "hybrid"):
            raise ConfigError("triage.mode must be heuristic | llm | hybrid")
        if self.get("triage.mode") != "heuristic":
            self.check_spec(AgentSpec.parse(self.get("triage.model")), "triage.model")
        if self.get("roles.planner.when") not in ("hard", "always", "never"):
            raise ConfigError("roles.planner.when must be hard | always | never")
        for sev in self.get("loop.block_on", []):
            if sev not in SEVERITIES:
                raise ConfigError(f"loop.block_on: unknown severity '{sev}'")
        if int(self.get("loop.max_review_rounds", 0)) < 1:
            raise ConfigError("loop.max_review_rounds must be >= 1")
        if self.get("loop.rereview") not in ("both", "first"):
            raise ConfigError("loop.rereview must be both | first")
        if self.get("project.isolation") not in ("branch", "worktree", "none"):
            raise ConfigError("project.isolation must be branch | worktree | none")
        sources = str(self.get("providers.claude.setting_sources", "") or "").replace(" ", "")
        if sources and not set(sources.split(",")) <= {"user", "project", "local"}:
            raise ConfigError("providers.claude.setting_sources: comma-separated subset of user, project, local")
        for model in [m for m in str(self.get("providers.claude.fallback_model", "") or "").replace(" ", "").split(",") if m]:
            models = self.get("models.claude", [])
            if "*" not in models and model not in models:
                raise ConfigError(f"providers.claude.fallback_model: '{model}' is not on the [models].claude "
                                  f"allowlist {models}")
        for role in REVIEW_ROLES:
            lens = self.get(f"roles.{role}.lens", "correctness")
            if lens not in LENSES:
                raise ConfigError(f"roles.{role}.lens must be one of {', '.join(LENSES)}")
        if self.get("loop.min_confidence_single") not in CONFIDENCES:
            raise ConfigError("loop.min_confidence_single must be low | medium | high")

    # ── routing ──────────────────────────────────────────────────────────
    def spec_for(self, role: str, difficulty: str) -> AgentSpec:
        if difficulty not in DIFFICULTIES:
            raise ConfigError(f"unknown difficulty '{difficulty}'")
        return AgentSpec.parse(self.get(f"roles.{role}.{difficulty}"))

    def role_opt(self, role: str, key: str, default: Any = None) -> Any:
        return self.get(f"roles.{role}.{key}", default)


def python_invocation(cfg: "Config | None" = None) -> str:
    """Interpreter command used in hooks, prompts and allowedTools ([project].python overrides)."""
    import os
    import shutil
    explicit = cfg.get("project.python", "") if cfg else ""
    if explicit:
        return str(explicit)
    if os.name == "nt":
        return "py -3" if shutil.which("py") else "python"
    return "python3"


def gates_command(cfg: "Config | None" = None) -> str:
    return f"{python_invocation(cfg)} .rustflow/gates.py"


def bump(difficulty: str, steps: int = 1) -> str:
    idx = min(DIFFICULTIES.index(difficulty) + steps, len(DIFFICULTIES) - 1)
    return DIFFICULTIES[idx]


def max_difficulty(*values: str) -> str:
    return max(values, key=DIFFICULTIES.index)
