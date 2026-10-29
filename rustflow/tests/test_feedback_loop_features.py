"""Features taken from rust-feedback-loop: lint ratchet, SQLx gate/guard, nextest config, stack skill, selftest."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rustflow import gates, hooks, lints
from rustflow.installer import Installer

needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not available")


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "crates").mkdir(parents=True)
    (ws / "Cargo.toml").write_text('[workspace]\nresolver = "3"\nmembers = ["crates/*"]\n')
    for name, kind in (("core", "--lib"), ("cli", "--bin")):
        subprocess.run(["cargo", "new", "-q", kind, "--vcs", "none", str(ws / "crates" / name)], check=True)
    (ws / "crates" / "core" / "src" / "lib.rs").write_text(
        "//! core\n/// Parses.\npub fn parse(s: &str) -> Option<i32> {\n    s.trim().parse().ok()\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n        assert_eq!(super::parse(\" 4 \").unwrap(), 4);\n    }\n}\n")
    (ws / "crates" / "cli" / "src" / "main.rs").write_text(
        'fn main() {\n    let v: i32 = "3".parse().unwrap();\n    let _ = std::fs::remove_file("none");\n    println!("{v}");\n}\n')
    return ws


@needs_cargo
def test_lint_ratchet_adopts_only_satisfied_lints(tmp_path, monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "target"))
    ws = _ws(tmp_path)
    out: list[str] = []
    assert lints.run(ws, do_apply=True, exclude={"missing_docs"}, out=out.append) == 0
    root = (ws / "Cargo.toml").read_text()
    assert 'unsafe_code = "forbid"' in root and 'await_holding_lock = "deny"' in root
    assert "unwrap_used" not in root and "print_stdout" not in root      # cli violates them
    # REVIEW.md rules as lints: `let _ =` / `.ok()` swallowing, `#[allow]` without a reason, panicking indexing
    assert "let_underscore_must_use" not in root                          # cli swallows a Result
    for lint in ("unused_result_ok", "allow_attributes_without_reason", "indexing_slicing"):
        assert f'{lint} = "warn"' in root
    assert (ws / "crates" / "core" / "Cargo.toml").read_text().rstrip().endswith("[lints]\nworkspace = true")
    conf = (ws / "clippy.toml").read_text()
    assert "allow-expect-in-tests = true" in conf and "allow-indexing-slicing-in-tests = true" in conf
    assert any("clippy -D warnings after apply: PASS" in l for l in out)
    # second run: everything already configured, nothing written twice
    out2: list[str] = []
    lints.run(ws, do_apply=True, exclude={"missing_docs"}, out=out2.append)
    assert (ws / "Cargo.toml").read_text().count("unsafe_code") == 1
    assert any("already configured" in l for l in out2)


def test_init_points_to_the_lint_ratchet(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    out: list[str] = []
    Installer(tmp_path, with_rust_skills=False, python_cmd="python3", ci=False, out=out.append).run()
    assert any("rustflow lints" in line for line in out)


def test_insert_entries_into_existing_section():
    text = '[package]\nname = "x"\n\n[lints.clippy]\ntodo = "warn"\n'
    new = lints._insert_entries(text, "lints.clippy", ['dbg_macro = "warn"'])
    assert new.count("[lints.clippy]") == 1 and 'dbg_macro = "warn"' in new
    new2 = lints._insert_entries(text, "lints.rust", ['unsafe_code = "forbid"'])
    assert new2.endswith('[lints.rust]\nunsafe_code = "forbid"\n')


def test_sqlx_gate_modes(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REQUIRE_SQLX_CHECK", raising=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn f() { let _ = 1; }\n")
    r = gates.GateRunner(tmp_path, dict(gates.DEFAULT_GATES))
    assert r.sqlx().reason == "no SQLx query macros"
    (tmp_path / "src" / "db.rs").write_text('async fn g() { sqlx::query_as!(B, "SELECT 1"); }\n')
    st = r.sqlx()
    assert st.skipped and "DATABASE_URL" in st.reason
    monkeypatch.setenv("REQUIRE_SQLX_CHECK", "1")              # CI: missing DB is a failure
    st = r.sqlx()
    assert not st.ok and not st.skipped
    (tmp_path / ".env").write_text('DATABASE_URL="postgres://u@localhost/dev"\n')
    assert gates.database_url(tmp_path) == "postgres://u@localhost/dev"
    monkeypatch.setattr(gates, "has_tool", lambda *a: False)
    assert "sqlx-cli not installed" in r.sqlx().reason


@pytest.mark.parametrize("cmd,env,decision", [
    ("sqlx migrate revert", "postgres://u:p@localhost:5432/dev", None),
    ("sqlx migrate revert", "postgres://u:p@prod.example.com/app", "deny"),
    ("DATABASE_URL=sqlite:dev.db sqlx migrate revert", "", None),
    ("cargo sqlx migrate revert -D postgres://x@10.1.2.3/app", "", "deny"),
    ("sqlx database reset", "postgres://u@localhost/dev", "deny"),
])
def test_sqlx_revert_guard(monkeypatch, capsys, tmp_path, cmd, env, decision):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("RUSTFLOW_ROLE", raising=False)
    if env:
        monkeypatch.setenv("DATABASE_URL", env)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    hooks.guard_bash({"tool_input": {"command": cmd}})
    out = capsys.readouterr().out
    assert (json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None) == decision


def test_installer_stack_and_nextest(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    (tmp_path / ".config").mkdir()
    Installer(tmp_path, with_rust_skills=False, python_cmd="python3", stack="axum-sqlx", out=lambda *_: None).run()
    assert "slow-timeout" in (tmp_path / ".config" / "nextest.toml").read_text()
    for base in (".claude/skills", ".agents/skills"):
        assert (tmp_path / base / "rustflow-stack-axum-sqlx" / "SKILL.md").is_file()
    assert "rustflow-stack-axum-sqlx" in (tmp_path / "AGENTS.md").read_text()
    assert "postgres:17" in (tmp_path / ".github" / "workflows" / "rustflow-gates.yml").read_text()
    with pytest.raises(ValueError):
        Installer(tmp_path, with_rust_skills=False, stack="django", out=lambda *_: None)


def test_existing_nextest_config_is_kept(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "nextest.toml").write_text("[profile.default]\nretries = 2\n")
    Installer(tmp_path, with_rust_skills=False, python_cmd="python3", ci=False, out=lambda *_: None).run()
    assert (tmp_path / ".config" / "nextest.toml").read_text() == "[profile.default]\nretries = 2\n"


@needs_cargo
def test_selftest_passes(tmp_path, monkeypatch, capsys):
    from rustflow.selftest import SelfTest
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "t"))
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": sys.executable, "args": ["${CLAUDE_PROJECT_DIR}/.rustflow/hooks.py", "stop"]}]}]}}))
    lines: list[str] = []
    assert SelfTest(repo, out=lines.append).run() == 0, "\n".join(lines)
    assert lines[-1].endswith("passed") and "FAIL" not in "\n".join(lines)


# ── always-on skills ─────────────────────────────────────────────────────────
from rustflow import skills as sk


def _skill(root, base, name, body="BODY"):
    d = root / base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n{body}\n")


def test_auto_resolves_stack_and_apollo(tmp_path):
    _skill(tmp_path, ".claude/skills", "rustflow-stack-axum-sqlx", "STACK")
    _skill(tmp_path, ".claude/skills", "rust-best-practices", "APOLLO see references/chapter_01.md")
    _skill(tmp_path, ".claude/skills", "rustflow-review")          # role skills are never "auto" extras
    names = [p.parent.name for p in sk.resolve({"all": ["auto"]}, "reviewer_b", tmp_path, "claude")]
    assert names == ["rustflow-stack-axum-sqlx", "rust-best-practices"]
    text = sk.bundle(sk.resolve({"all": ["auto"]}, "implementer", tmp_path, "claude"), tmp_path)
    assert "STACK" in text and 'dir=".claude/skills/rust-best-practices"' in text and "name: " not in text
    assert sk.resolve({"all": ["auto"]}, "triage", tmp_path, "claude") == []


def test_explicit_missing_skill_fails_loudly(tmp_path):
    with pytest.raises(sk.SkillError, match="does not exist"):
        sk.resolve({"all": [], "reviewer": ["rust-best-practices"]}, "reviewer_a", tmp_path, "claude")
    assert sk.resolve({"all": [], "reviewer": ["rust-best-practices"]}, "implementer", tmp_path, "claude") == []


def test_skill_size_limit(tmp_path):
    _skill(tmp_path, ".claude/skills", "rust-best-practices", "x" * 5000)
    with pytest.raises(sk.SkillError, match="max_kb"):
        sk.resolve({"all": ["auto"], "max_kb": 2}, "fixer", tmp_path, "claude")


def test_claude_gets_system_prompt_file_codex_gets_inline(tmp_path, monkeypatch):
    from rustflow import procutil
    from rustflow.agents import AgentCall, ClaudeRunner, CodexRunner, compose_prompt
    from rustflow.config import AgentSpec, Config, load_defaults
    _skill(tmp_path, ".claude/skills", "rust-best-practices", "APOLLO-RULES")
    _skill(tmp_path, ".agents/skills", "rust-best-practices", "APOLLO-RULES")
    monkeypatch.setattr(procutil, "resolve_command", lambda name: [name])
    cfg = Config(load_defaults())
    call = AgentCall(role="reviewer_a", name="r", spec=AgentSpec("claude", "opus", "high"), prompt="TASK", write=False,
                     schema=None, cwd=tmp_path, log_dir=tmp_path / "logs",
                     extra_skills=sk.resolve({"all": ["auto"]}, "reviewer_a", tmp_path, "claude"))
    argv = ClaudeRunner(cfg).build_argv(call)
    f = Path(argv[argv.index("--append-system-prompt-file") + 1])
    assert "APOLLO-RULES" in f.read_text() and "--append-system-prompt" not in argv
    call2 = AgentCall(**{**call.__dict__, "spec": AgentSpec("codex", "gpt-6-astra", "high"),
                         "extra_skills": sk.resolve({"all": ["auto"]}, "reviewer_b", tmp_path, "codex")})
    assert compose_prompt(call2, "mention", extras_inline=True).startswith('<skill name="rust-best-practices"')
    assert "APOLLO-RULES" not in compose_prompt(call, "slash")    # Claude: not duplicated into the prompt


def test_claude_system_prompt_is_identical_across_roles(tmp_path, monkeypatch):
    # Measured live: the fixer started 3.5 min after the implementer and re-wrote the whole ~35k-token
    # prefix to the cache because only the role line in the system prompt differed.
    from rustflow import procutil
    from rustflow.agents import AgentCall, ClaudeRunner
    from rustflow.config import AgentSpec, Config, load_defaults
    _skill(tmp_path, ".claude/skills", "rust-best-practices", "APOLLO-RULES")
    monkeypatch.setattr(procutil, "resolve_command", lambda name: [name])
    runner = ClaudeRunner(Config(load_defaults()))

    def system_text(role, extra=True):
        call = AgentCall(role=role, name=role, spec=AgentSpec("claude", "opus", "high"), prompt="TASK",
                         write=role in ("implementer", "fixer"), schema=None, cwd=tmp_path, log_dir=tmp_path / "logs",
                         extra_skills=sk.resolve({"all": ["auto"]}, role, tmp_path, "claude") if extra else [])
        argv = runner.build_argv(call)
        if extra:
            return Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
        return argv[argv.index("--append-system-prompt") + 1]

    roles = ("implementer", "fixer", "reviewer_a", "planner")
    assert len({system_text(r) for r in roles}) == 1
    assert len({system_text(r, extra=False) for r in roles}) == 1


def test_claude_prompt_carries_the_role_after_the_skill_command(tmp_path, monkeypatch):
    from rustflow import procutil
    from rustflow.agents import AgentCall, make_runners
    from rustflow.config import AgentSpec, Config, load_defaults
    _skill(tmp_path, ".claude/skills", "rustflow-fix", "FIX-PROCEDURE")
    monkeypatch.setattr(procutil, "resolve_command", lambda name: [name])
    call = AgentCall(role="fixer", name="fix-r1", spec=AgentSpec("claude", "opus", "high"), prompt="TASK", write=True,
                     schema=None, cwd=tmp_path, log_dir=tmp_path / "logs", skill="rustflow-fix")
    make_runners(Config(load_defaults()), dry_run=True)["claude"].run(call)
    prompt = (tmp_path / "logs" / "fix-r1.prompt.md").read_text()
    assert prompt.startswith("/rustflow-fix\n")              # the slash command must stay first to expand
    assert "You are the FIXER" in prompt and prompt.index("You are the FIXER") < prompt.index("TASK")


def test_role_line_never_displaces_a_leading_slash_command(tmp_path):
    from rustflow.agents import AgentCall, with_role
    from rustflow.config import AgentSpec
    call = AgentCall(role="tob_audit", name="audit-tob", spec=AgentSpec("claude", "opus", "high"),
                     prompt="/rust-review:rust-review scope src\n\nNon-interactive run.", write=True, schema=None,
                     cwd=tmp_path, log_dir=tmp_path)
    prompt = with_role(call).prompt
    assert prompt.startswith("/rust-review:rust-review scope src\n") and "SOUNDNESS/SECURITY" not in prompt.split("\n")[0]
    assert "audit plugin" in prompt


def test_installer_preloads_always_on_skills_in_reviewer_agent(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    _skill(tmp_path, ".claude/skills", "rust-best-practices")
    Installer(tmp_path, with_rust_skills=False, python_cmd="python3", ci=False, stack="axum-sqlx", out=lambda *_: None).run()
    agent = (tmp_path / ".claude" / "agents" / "rustflow-reviewer.md").read_text()
    head = agent.split("hooks:")[0]
    assert "  - rustflow-review\n  - rustflow-stack-axum-sqlx\n" in head
    assert "rust-best-practices" not in head      # default: reviewers get stack skills, no knowledge base
