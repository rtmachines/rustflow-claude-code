"""Extra skills that every agent of a role gets *for sure* - not left to the model's discretion.

Model-invoked skills are loaded only when the model decides a description matches, so a stack skill or
rust-skills may silently never be read. rustflow therefore attaches them
deterministically:

- Claude: `--append-system-prompt-file` (same text for every role -> prompt-cache friendly, no command-line
  length limit on Windows);
- Codex: inlined at the top of the prompt (stdin);
- interactive `rustflow-reviewer` subagent: `skills:` preload in its frontmatter (installed by init).

Only SKILL.md is attached; files it references (e.g. rust-skills rules/*.md) stay on disk and
the header tells the agent where they are.
"""

from __future__ import annotations

from pathlib import Path

AUTO = "auto"
STACK = "stack"   # only the installed rustflow-stack-* skills (no general knowledge base)
KNOWLEDGE_SKILLS = ("rust-skills", "rust-best-practices")
ROLE_GROUP = {"planner": "planner", "implementer": "implementer", "fixer": "fixer",
              "reviewer_a": "reviewer", "reviewer_b": "reviewer", "auditor": "reviewer", "reviewer": "reviewer"}
SKILL_BASE = {"claude": ".claude/skills", "codex": ".agents/skills"}


class SkillError(ValueError):
    pass


def _base(cwd: Path, provider: str) -> Path:
    return cwd / SKILL_BASE.get(provider, ".claude/skills")


def stack_names(cwd: Path, provider: str) -> list[str]:
    base = _base(cwd, provider)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.name.startswith("rustflow-stack-") and (p / "SKILL.md").is_file())


def auto_names(cwd: Path, provider: str) -> list[str]:
    base = _base(cwd, provider)
    return stack_names(cwd, provider) + [name for name in KNOWLEDGE_SKILLS if (base / name / "SKILL.md").is_file()]


def configured_names(section: dict, role: str) -> list[str]:
    group = ROLE_GROUP.get(role)
    if group is None:
        return []
    out: list[str] = []
    for key in ("all", group):
        val = section.get(key, [])
        out += [val] if isinstance(val, str) else list(val or [])
    return out


def resolve(section: dict, role: str, cwd: Path, provider: str, *, strict: bool = True) -> list[Path]:
    """SKILL.md paths for a role. Explicitly named but missing skills are an error (strict) - 'for sure'
    must fail loudly, not degrade silently. 'auto' expands to what is installed."""
    paths: list[Path] = []
    seen: set[str] = set()
    for name in configured_names(section, role):
        names = (auto_names(cwd, provider) if name == AUTO else stack_names(cwd, provider) if name == STACK
                 else [name])
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            path = _base(cwd, provider) / n / "SKILL.md"
            if path.is_file():
                paths.append(path)
            elif strict:
                raise SkillError(f"[skills] lists '{n}' for {ROLE_GROUP.get(role, role)}, but "
                                 f"{path.relative_to(cwd).as_posix()} does not exist (rustflow init / --stack)")
    limit = int(section.get("max_kb", 64)) * 1024
    total = sum(p.stat().st_size for p in paths)
    if strict and total > limit:
        raise SkillError(f"extra skills for {role} are {total // 1024} KB > [skills].max_kb = {limit // 1024} KB "
                         "(they go into every call of the role; trim the list or raise max_kb)")
    return paths


def body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def bundle(paths: list[Path], cwd: Path) -> str:
    parts = []
    for p in paths:
        rel = p.parent.relative_to(cwd).as_posix() if p.is_relative_to(cwd) else p.parent.as_posix()
        parts.append(f'<skill name="{p.parent.name}" dir="{rel}">\n'
                     f"(Always-on project skill. Files it references, e.g. references/*.md, are in {rel}/ - "
                     f"open them when the task touches that topic.)\n\n{body(p)}\n</skill>")
    return "\n\n".join(parts)
