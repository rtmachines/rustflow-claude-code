import io
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from rustflow import findings as fnd
from rustflow import gates, gitutil, hooks, procutil, schemas, triage
from rustflow.agents import AgentCall, ClaudeRunner, CodexRunner, compose_prompt
from rustflow.config import AgentSpec, Config, ConfigError, deep_merge, load_defaults
from rustflow.installer import Installer


# ── config ───────────────────────────────────────────────────────────────────
def test_defaults_valid():
    cfg = Config(load_defaults())
    assert str(cfg.spec_for("reviewer_b", "hard")).startswith("codex:")


def test_allowlist_rejects_unknown_model():
    data = deep_merge(load_defaults(), {"roles": {"implementer": {"hard": "claude:gpt-9:high"}}})
    with pytest.raises(ConfigError, match="allowlist"):
        Config(data)


def test_allowlist_rejects_effort_and_provider():
    with pytest.raises(ConfigError):
        Config(deep_merge(load_defaults(), {"roles": {"fixer": {"easy": "claude:opus:turbo"}}}))
    with pytest.raises(ConfigError):
        Config(deep_merge(load_defaults(), {"roles": {"fixer": {"easy": "gemini:pro:high"}}}))


def test_wildcard_model_allowlist():
    data = deep_merge(load_defaults(), {"models": {"codex": ["*"]},
                                        "roles": {"reviewer_b": {"easy": "codex:gpt-7-new:high"}}})
    assert Config(data).spec_for("reviewer_b", "easy").model == "gpt-7-new"


def test_spec_parse():
    assert AgentSpec.parse("codex:gpt-6-astra:xhigh") == AgentSpec("codex", "gpt-6-astra", "xhigh")
    with pytest.raises(ConfigError):
        AgentSpec.parse("claude:opus")


# ── triage ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("task,expected", [
    ("Popraw literówkę w README", "easy"),
    ("Dodaj endpoint GET /books z obsługą błędów i testami do crate web, zapis w bazie danych sqlx", "medium"),
    ("Zaimplementuj bezpieczny wrapper FFI (extern \"C\", unsafe) z wielowątkowym cache (Mutex, atomic)", "hard"),
])
def test_triage(task, expected):
    assert triage.heuristic(task, None, Config(load_defaults())).difficulty == expected


# ── schemas ──────────────────────────────────────────────────────────────────
def test_schema_coercion_and_defaults():
    raw = {"summary": "s", "verdict": "approve", "findings": [
        {"severity": "p1", "category": "correctness", "file": "a.rs", "line": "12", "title": "t",
         "trigger": "", "impact": "", "rule": "", "fix": "", "confidence": "HIGH"}]}
    out = schemas.validate(raw, schemas.REVIEW_SCHEMA)
    f = out["findings"][0]
    assert out["verdict"] == "APPROVE" and f["severity"] == "P1" and f["line"] == 12
    assert f["confidence"] == "high" and f["previous_id"] is None and out["disputes"] == []


def test_schema_rejects_bad_enum():
    with pytest.raises(schemas.SchemaError):
        schemas.validate({"summary": "", "resolutions": [{"id": "F1", "status": "maybe", "note": ""}],
                          "tests_added": []}, schemas.FIX_SCHEMA)


def test_schemas_are_strict_mode_compatible():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    for sch in (schemas.REVIEW_SCHEMA, schemas.FIX_SCHEMA, schemas.PLAN_SCHEMA, schemas.IMPLEMENT_SCHEMA,
                triage.TRIAGE_SCHEMA):
        walk(sch)


def test_extract_json_from_fenced_text():
    assert schemas.extract_json('Here:\n```json\n{"a": 1}\n```\nbye') == {"a": 1}
    assert schemas.extract_json('noise {"a": {"b": 2}} tail') == {"a": {"b": 2}}


# ── findings ─────────────────────────────────────────────────────────────────
def _f(file="src/lib.rs", line=3, sev="P1", cat="correctness", title="bug in parse", conf="high", prev=None):
    return {"severity": sev, "category": cat, "file": file, "line": line, "title": title, "trigger": "x",
            "impact": "y", "rule": "z", "fix": "w", "confidence": conf, "previous_id": prev}


def _review(findings=(), disputes=(), verdict="REQUEST_CHANGES"):
    return {"summary": "", "verdict": verdict, "findings": list(findings), "disputes": list(disputes),
            "acceptance": [], "residual_risks": []}


@pytest.fixture
def src(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("\n".join(f"line {i}" for i in range(1, 21)))
    return tmp_path


def test_merge_confirm_and_policy(src):
    agg = fnd.aggregate({
        "a": _review([_f(line=3), _f(line=15, sev="P2", conf="low", title="style")]),
        "b": _review([_f(line=5, title="parse bug again"), _f(file="src/ghost.rs"),
                      _f(line=10, conf="low", title="maybe race")]),
    }, root=src, round_no=1, previous=[], ids=fnd.IdAllocator(), block_on=["P0", "P1"])
    blocking = agg.blocking()
    assert len(blocking) == 1 and blocking[0].confirmed and blocking[0].line in (3, 5)
    assert len(agg.dropped) == 1                       # ghost file
    statuses = sorted(f.status for f in agg.findings)
    assert statuses == ["advisory", "advisory", "open"]  # P2 -> advisory; single low-confidence P1 -> advisory


def test_p0_blocks_even_with_low_confidence(src):
    agg = fnd.aggregate({"a": _review([_f(sev="P0", conf="low")])}, root=src, round_no=1, previous=[],
                        ids=fnd.IdAllocator(), block_on=["P1"])
    assert agg.blocking()[0].severity == "P0"


def test_out_of_range_line_clamped_and_downgraded(src):
    agg = fnd.aggregate({"a": _review([_f(line=999, conf="medium")])}, root=src, round_no=1, previous=[],
                        ids=fnd.IdAllocator(), block_on=["P0", "P1"])
    f = agg.findings[0]
    assert f.line == 20 and not f.anchor_ok and f.status == "advisory"


def test_dispute_flow_goes_to_human_after_two_disagreements(src):
    ids = fnd.IdAllocator()
    r1 = fnd.aggregate({"a": _review([_f()])}, root=src, round_no=1, previous=[], ids=ids, block_on=["P1"])
    fnd.apply_resolutions(r1.findings, [{"id": "F1", "status": "rejected", "note": "cannot happen"}], True)
    # Round 2: reviewer re-raises -> still blocking, dispute 1
    r2 = fnd.aggregate({"a": _review([_f(prev="F1")], [{"id": "F1", "decision": "reraise", "note": "it can"}])},
                       root=src, round_no=2, previous=r1.findings, ids=ids, block_on=["P1"])
    assert [f.id for f in r2.blocking()] == ["F1"] and r2.blocking()[0].dispute_count == 1
    fnd.apply_resolutions(r2.findings, [{"id": "F1", "status": "rejected", "note": "still no"}], True)
    r3 = fnd.aggregate({"a": _review([_f(prev="F1")])}, root=src, round_no=3, previous=r2.findings, ids=ids,
                       block_on=["P1"])
    assert r3.blocking() == [] and r3.disputed()[0].id == "F1"


def test_rejection_accepted_closes_finding(src):
    ids = fnd.IdAllocator()
    r1 = fnd.aggregate({"a": _review([_f()])}, root=src, round_no=1, previous=[], ids=ids, block_on=["P1"])
    fnd.apply_resolutions(r1.findings, [{"id": "F1", "status": "rejected", "note": "by design"}], True)
    r2 = fnd.aggregate({"a": _review([], [{"id": "F1", "decision": "accept_rejection", "note": "ok"}], "APPROVE")},
                       root=src, round_no=2, previous=r1.findings, ids=ids, block_on=["P1"])
    assert r2.blocking() == [] and r2.findings[0].status == "accepted_rejection"


def test_same_reviewer_two_nearby_findings_not_merged(src):
    agg = fnd.aggregate({"a": _review([_f(line=3, title="overflow"), _f(line=4, title="panic on empty")])},
                        root=src, round_no=1, previous=[], ids=fnd.IdAllocator(), block_on=["P1"])
    assert len(agg.findings) == 2


def test_windows_style_paths_normalized(src):
    agg = fnd.aggregate({"a": _review([_f(file=".\\src\\lib.rs:3")])}, root=src, round_no=1, previous=[],
                        ids=fnd.IdAllocator(), block_on=["P1"])
    assert agg.findings[0].file == "src/lib.rs"


# ── git helpers ──────────────────────────────────────────────────────────────
DIFF = """diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,3 +1,5 @@
 fn a() {}
+unsafe fn b() {}
+static mut X: i32 = 0;
 fn c() {}
-fn d() {}
+fn e() { let o = Ordering::Relaxed; }
diff --git a/Cargo.toml b/Cargo.toml
--- a/Cargo.toml
+++ b/Cargo.toml
@@ -1 +1,2 @@
 [package]
+name = "x"
"""


def test_changed_line_ranges():
    r = gitutil.changed_line_ranges(DIFF)
    assert r["src/lib.rs"] == [(2, 3), (5, 5)]
    assert r["Cargo.toml"] == [(2, 2)]


def test_risk_signals():
    s = gitutil.risk_signals(DIFF)
    assert s.unsafe == 1 and s.unsafe_misc == 1 and s.atomics == 1 and s.manifests == 1
    assert s.escalation == 3


# ── gates parsing ────────────────────────────────────────────────────────────
def test_parse_failed_tests_nextest_and_libtest():
    text = ("test tests::a ... FAILED\n        FAIL [   0.004s] (1/3) demo tests::b\n"
            "test tests::c ... ok\n")
    assert gates.parse_failed_tests(text) == ["tests::a", "demo tests::b"]


def test_parse_cargo_json_relative_paths(tmp_path):
    msg = {"reason": "compiler-message", "message": {
        "level": "error", "message": "mismatched types", "code": {"code": "E0308"}, "rendered": "error[E0308]",
        "spans": [{"file_name": str(tmp_path / "src" / "lib.rs"), "line_start": 7, "is_primary": True}]}}
    d = gates.parse_cargo_json(json.dumps(msg), tmp_path)[0]
    assert d.file == "src/lib.rs" and d.line == 7 and gates.is_compile_error(d)
    lint = gates.Diagnostic("error", "x", code="clippy::needless_return")
    assert not gates.is_compile_error(lint)


def test_signatures_ignore_line_numbers():
    a = gates.GateResult("full", False, None, steps=[gates.Step("clippy", False, diagnostics=[
        gates.Diagnostic("error", "unused", "unused_variables", "src/a.rs", 3)])])
    b = gates.GateResult("full", False, None, steps=[gates.Step("clippy", False, diagnostics=[
        gates.Diagnostic("error", "unused", "unused_variables", "src/a.rs", 9)])])
    assert a.signatures() == b.signatures()


# ── hooks ────────────────────────────────────────────────────────────────────
def _guard(monkeypatch, role, cmd, capsys):
    if role:
        monkeypatch.setenv("RUSTFLOW_ROLE", role)
    else:
        monkeypatch.delenv("RUSTFLOW_ROLE", raising=False)
    hooks.guard_bash({"tool_input": {"command": cmd}})
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


@pytest.mark.parametrize("cmd,decision", [
    ("git diff HEAD~1 -- src/lib.rs", "allow"),
    ("cargo clippy --workspace -- -D warnings", "allow"),
    ("rg -n unwrap src | head -20", "allow"),
    ("git commit -am x", "deny"),
    ("cargo fmt --all", "deny"),
    ("cat src/lib.rs > /tmp/x", "deny"),
    ("git diff; rm -rf src", "deny"),
    ("python script.py", "deny"),
    ("find . -name '*.rs' -delete", "deny"),
])
def test_reviewer_guard(monkeypatch, capsys, cmd, decision):
    assert _guard(monkeypatch, "reviewer_a", cmd, capsys) == decision


def test_implementer_guard(monkeypatch, capsys):
    assert _guard(monkeypatch, "implementer", "cargo test --workspace", capsys) is None
    assert _guard(monkeypatch, "implementer", "git commit -m wip", capsys) == "deny"
    assert _guard(monkeypatch, "fixer", "git stash", capsys) == "deny"
    assert _guard(monkeypatch, "implementer", "git push --force origin main", capsys) == "deny"
    assert _guard(monkeypatch, "", "cargo publish", capsys) == "deny"


def test_post_edit_skips_reviewer_and_non_rust(monkeypatch):
    monkeypatch.setenv("RUSTFLOW_ROLE", "reviewer_b")
    assert hooks.post_edit({"tool_input": {"file_path": "src/lib.rs"}}) == 0
    monkeypatch.delenv("RUSTFLOW_ROLE")
    assert hooks.post_edit({"tool_input": {"file_path": "README.md"}}) == 0
    assert hooks.post_edit({"tool_input": {"file_path": "target/debug/x.rs"}}) == 0


def test_hook_main_never_crashes(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hooks.main(["guard-bash"]) == 0


# ── installer ────────────────────────────────────────────────────────────────
def test_installer_idempotent_and_preserves_user_settings(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    (tmp_path / ".claude").mkdir()
    user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]},
            "permissions": {"allow": ["Bash(make *)"]}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(user))
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nRules here.\n")
    out: list[str] = []
    inst = Installer(tmp_path, with_rust_skills=False, python_cmd="py -3", ci=False, out=out.append)
    inst.run()
    inst.run()
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_cmds = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds == ["mine.sh", "py"]                       # user hook kept, ours once
    ours = s["hooks"]["Stop"][1]["hooks"][0]
    assert ours["args"] == ["-3", "${CLAUDE_PROJECT_DIR}/.rustflow/hooks.py", "stop"]
    assert "Bash(make *)" in s["permissions"]["allow"]
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert claude_md.count("rustflow:begin") == 1 and claude_md.startswith("# My project")
    for base in (".claude/skills", ".agents/skills"):
        assert (tmp_path / base / "rustflow-review" / "SKILL.md").is_file()
    assert (tmp_path / ".rustflow" / "gates.py").read_bytes() == Path(gates.__file__).read_bytes()


# ── procutil / agents ────────────────────────────────────────────────────────

@pytest.mark.parametrize("timeout_s", [None, 5])
def test_polled_communication_preserves_stdin_and_output(timeout_s):
    import subprocess
    import sys
    text = "input " * 20000
    with subprocess.Popen([sys.executable, "-c",
                           "import sys,time; data=sys.stdin.read(); time.sleep(0.6); "
                           "sys.stdout.write(data); sys.stderr.write('done')"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8") as proc:
        out, err = procutil._communicate_polled(proc, text, timeout_s)
    assert out == text and err == "done" and proc.returncode == 0

def test_unwrap_npm_cmd_shim(tmp_path):
    shim = tmp_path / "codex.cmd"
    script = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// js")
    shim.write_text('@ECHO off\r\nSET dp0=%~dp0\r\n"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    assert procutil._unwrap_npm_shim(shim) == script.resolve()


def test_run_timeout_kills_tree(tmp_path):
    import sys
    r = procutil.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
    assert r.timed_out and r.duration_s < 15


def _call(tmp_path, provider, write, skill="rustflow-review"):
    spec = AgentSpec(provider, "opus" if provider == "claude" else "gpt-6-astra", "high")
    return AgentCall(role="reviewer_a", name="t", spec=spec, prompt="TASK", write=write,
                     schema=schemas.REVIEW_SCHEMA, cwd=tmp_path, log_dir=tmp_path / "logs", skill=skill)


def test_claude_argv(tmp_path, monkeypatch):
    cfg = Config(load_defaults())
    monkeypatch.setattr(procutil, "resolve_command", lambda name: ["claude"])
    argv = ClaudeRunner(cfg).build_argv(_call(tmp_path, "claude", write=False))
    assert argv[:2] == ["claude", "-p"]
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    assert "Edit" in argv[argv.index("--disallowedTools") + 1]
    json.loads(argv[argv.index("--json-schema") + 1])


def test_codex_argv_write_with_network(tmp_path, monkeypatch):
    data = deep_merge(load_defaults(), {"providers": {"codex": {"network": True, "add_dirs": ["~/.cargo"]}}})
    cfg = Config(data)
    monkeypatch.setattr(procutil, "resolve_command", lambda name: ["codex"])
    argv = CodexRunner(cfg).build_argv(_call(tmp_path, "codex", write=True), tmp_path / "o.txt", tmp_path / "s.json")
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in argv
    assert argv[argv.index("--add-dir") + 1] == str(Path("~/.cargo").expanduser())
    assert argv[-1] == "-"


def test_compose_prompt_skill_modes(tmp_path):
    d = tmp_path / ".agents" / "skills" / "rustflow-review"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: rustflow-review\ndescription: x\n---\nBODY")
    call = _call(tmp_path, "codex", write=False)
    assert compose_prompt(call, "mention").startswith("$rustflow-review")
    inline = compose_prompt(call, "inline")
    assert "BODY" in inline and "name: rustflow-review" not in inline
    call.skill = "missing-skill"
    assert compose_prompt(call, "mention") == "TASK"


def test_unlabelled_persisting_finding_keeps_id(src):
    ids = fnd.IdAllocator()
    r1 = fnd.aggregate({"a": _review([_f(line=3)])}, root=src, round_no=1, previous=[], ids=ids, block_on=["P1"])
    r2 = fnd.aggregate({"a": _review([_f(line=4)])}, root=src, round_no=2, previous=r1.findings, ids=ids,
                       block_on=["P1"])
    assert [f.id for f in r2.blocking()] == ["F1"]


def test_open_finding_of_failed_reviewer_is_carried(src):
    ids = fnd.IdAllocator()
    r1 = fnd.aggregate({"a": _review([]), "b": _review([_f()])}, root=src, round_no=1, previous=[], ids=ids,
                       block_on=["P1"], min_confidence_single="low")
    fnd.apply_resolutions(r1.findings, [{"id": "F1", "status": "partial", "note": "half"}], True)
    r2 = fnd.aggregate({"a": _review([], verdict="APPROVE")}, root=src, round_no=2, previous=r1.findings,
                       ids=ids, block_on=["P1"], min_confidence_single="low")
    assert [f.id for f in r2.blocking()] == ["F1"]
    fnd.apply_resolutions(r1.findings, [{"id": "F1", "status": "fixed", "note": "done"}], True)
    r3 = fnd.aggregate({"a": _review([], verdict="APPROVE")}, root=src, round_no=2, previous=r1.findings,
                       ids=ids, block_on=["P1"], min_confidence_single="low")
    assert r3.blocking() == []


def test_doctest_names_with_spaces():
    text = "test src/lib.rs - parse_kv (line 12) ... FAILED\n"
    assert gates.parse_failed_tests(text) == ["src/lib.rs - parse_kv (line 12)"]


def test_hook_stdin_is_utf8(tmp_path):
    import subprocess, sys
    payload = json.dumps({"tool_input": {"command": "cat plik_zażółć.rs > /tmp/x"}}, ensure_ascii=False)
    env = {**os.environ, "RUSTFLOW_ROLE": "reviewer_a", "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run([sys.executable, str(Path(hooks.__file__))], input=payload.encode("utf-8"),
                       capture_output=True, env=env) if False else subprocess.run(
        [sys.executable, str(Path(hooks.__file__)), "guard-bash"], input=payload.encode("utf-8"),
        capture_output=True, env=env)
    assert json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_installer_python_path_with_spaces(tmp_path):
    from rustflow.installer import python_command
    exe = tmp_path / "Program Files" / "python.exe"
    exe.parent.mkdir()
    exe.write_text("")
    assert python_command(str(exe)) == (str(exe), [])
    assert python_command("py -3") == ("py", ["-3"])


def test_rtk_twins_in_allowed_tools(tmp_path, monkeypatch):
    from rustflow.agents import with_rtk_twins
    assert with_rtk_twins(["Read", "Bash(git diff *)"]) == ["Read", "Bash(git diff *)", "Bash(rtk git diff *)"]
    assert with_rtk_twins(["Bash(git diff *)"], enabled=False) == ["Bash(git diff *)"]
    cfg = Config(load_defaults())
    monkeypatch.setattr(procutil, "resolve_command", lambda name: ["claude"])
    argv = ClaudeRunner(cfg).build_argv(_call(tmp_path, "claude", write=True, skill=None))
    assert "Bash(rtk cargo *)" in argv[argv.index("--allowedTools") + 1]


@pytest.mark.parametrize("role,cmd,decision", [
    ("implementer", "rtk git commit -m x", "deny"),             # rtk prefix must not bypass the git ban
    ("fixer", "cd crate && rtk git stash", "deny"),
    ("implementer", "rtk cargo publish", "deny"),
    ("reviewer_a", "rtk git diff HEAD", "allow"),
    ("reviewer_a", "rtk read src/lib.rs", "allow"),
    ("reviewer_a", "rtk proxy git log -5", "allow"),
    ("reviewer_a", "rtk cargo build", "deny"),
    ("implementer", "rtk cargo test", None),
])
def test_guard_understands_rtk_prefix(monkeypatch, capsys, role, cmd, decision):
    assert _guard(monkeypatch, role, cmd, capsys) == decision


def test_agents_disable_review_kit_gate():
    from rustflow.orchestrator import Options, Orchestrator
    o = Orchestrator(Path("."), Config(load_defaults()), Options(dry_run=True))
    o.state = {"run_id": "r", "base_sha": "abc"}
    assert o._agent_env()["RUST_GATE_SKIP"] == "1"


GOOD_REPORT = """## Gates
- fmt: PASS
- clippy: PASS
- tests: FAIL (parse::trims)
- doctests: N/A
## Findings
- [P1] src/lib.rs:7 - no trim. Trigger: " a = 1". Impact: wrong keys. Rule: task. Fix: trim.
## Residual risks
- none
## Verdict: REQUEST_CHANGES
needs a fix
"""


def test_report_contract():
    assert hooks.report_problems(GOOD_REPORT) == []
    approve = GOOD_REPORT.replace("## Verdict: REQUEST_CHANGES", "## Verdict: APPROVE")
    probs = hooks.report_problems(approve)
    assert any("gate FAILs" in p for p in probs) and any("P0/P1" in p for p in probs)
    no_anchor = GOOD_REPORT.replace("src/lib.rs:7", "somewhere")
    assert any("anchor" in p for p in hooks.report_problems(no_anchor))
    assert hooks.report_problems("## Gates\n- fmt: PASS\n- clippy: PASS\n- tests: PASS\n## Findings\nNo findings.\n"
                                 "## Verdict: APPROVE\n") == []


def test_route_review(monkeypatch, capsys, tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("RUSTFLOW_ROLE", raising=False)
    hooks.route_review({"prompt": "zrób review tej gałęzi"})
    assert "rustflow-pr-review" in capsys.readouterr().out
    for prompt in ("/rustflow-pr-review main", "dodaj funkcję parse"):
        hooks.route_review({"prompt": prompt})
        assert capsys.readouterr().out == ""
    monkeypatch.setenv("RUSTFLOW_ROLE", "reviewer_a")
    hooks.route_review({"prompt": "review the change set"})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("cmd,decision", [
    ("python3 .rustflow/gates.py run --profile full --check", "allow"),
    ("py -3 .rustflow/gates.py run --check --json", "allow"),
    ("python3 .rustflow/gates.py run --profile full", "deny"),        # would run cargo fmt (writes)
    ("python3 evil.py run --check", "deny"),
])
def test_reviewer_may_run_check_only_gates(monkeypatch, capsys, cmd, decision):
    assert _guard(monkeypatch, "reviewer", cmd, capsys) == decision


def test_tob_audit_role_cannot_touch_git(monkeypatch, capsys):
    assert _guard(monkeypatch, "tob_audit", "git commit -am x", capsys) == "deny"
    assert _guard(monkeypatch, "tob_audit", "rg -n unsafe src", capsys) is None


def test_publishable_libs_from_metadata():
    meta = {"workspace_members": ["a 0.1", "b 0.1", "c 0.1"], "packages": [
        {"id": "a 0.1", "name": "a", "publish": None, "targets": [{"kind": ["lib"]}]},
        {"id": "b 0.1", "name": "b", "publish": [], "targets": [{"kind": ["lib"]}]},
        {"id": "c 0.1", "name": "c", "publish": None, "targets": [{"kind": ["bin"]}]},
        {"id": "dep 1.0", "name": "dep", "publish": None, "targets": [{"kind": ["lib"]}]}]}
    assert gates.publishable_libs_from_metadata(meta) == ["a"]


def test_semver_skipped_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "has_tool", lambda *a: False)
    st = gates.GateRunner(tmp_path, dict(gates.DEFAULT_GATES)).semver()
    assert st.skipped and "not installed" in st.reason
    st2 = gates.GateRunner(tmp_path, {**gates.DEFAULT_GATES, "semver": "always"}).semver()
    assert not st2.ok and not st2.skipped


def test_risk_files():
    assert gates.risk_files(DIFF) == {"src/lib.rs": 3}


def test_tob_mode_requires_claude_spec():
    with pytest.raises(ConfigError, match="needs"):
        Config(deep_merge(load_defaults(), {"roles": {"auditor": {"mode": "tob", "spec": ""}}}))
    with pytest.raises(ConfigError, match="claude"):
        Config(deep_merge(load_defaults(), {"roles": {"auditor": {"mode": "tob", "spec": "codex:gpt-6-astra:high"}}}))


def test_installer_interactive_review_files(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    Installer(tmp_path, with_rust_skills=False, python_cmd="py -3", ci=False, with_plugins=True, out=lambda *_: None).run()
    agent = (tmp_path / ".claude" / "agents" / "rustflow-reviewer.md").read_text()
    assert 'args: ["-3", "${CLAUDE_PROJECT_DIR}/.rustflow/hooks.py", "guard-bash", "--role", "reviewer"]' in agent
    assert "py -3 .rustflow/gates.py run --profile full --check" in agent and "{{" not in agent
    skill = (tmp_path / ".claude" / "skills" / "rustflow-pr-review" / "SKILL.md").read_text()
    assert "py -3 .rustflow/hooks.py review-context $ARGUMENTS" in skill and "{{" not in skill
    assert not (tmp_path / ".agents" / "skills" / "rustflow-pr-review").exists()   # Claude-only
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert s["enabledPlugins"]["rust-review@trailofbits"] is True
    assert "trailofbits" in s["extraKnownMarketplaces"] and "UserPromptSubmit" in s["hooks"]
    assert ".rust-review-results/" in (tmp_path / ".gitignore").read_text()


@pytest.mark.parametrize("role,cmd,decision", [
    ("reviewer", "git -C /repo diff abc def", "allow"),
    ("reviewer", "git --no-pager log -3", "allow"),
    ("reviewer", "git -C /repo commit -m x", "deny"),
    ("implementer", "git -C . commit -m sneaky", "deny"),
    ("fixer", "git -c user.name=x commit -m y", "deny"),
    ("implementer", "git -C . push --force origin main", "deny"),
])
def test_guard_handles_git_global_options(monkeypatch, capsys, role, cmd, decision):
    assert _guard(monkeypatch, role, cmd, capsys) == decision


# ── gate config from a revision / multiset signatures ────────────────────────
def _commit_toml(repo, text):
    (repo / "rustflow.toml").write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "rustflow.toml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "cfg"], cwd=repo, check=True)


def test_load_gate_config_at_missing(git_repo):
    assert gates.load_gate_config_at(git_repo, "HEAD") == gates.DEFAULT_GATES


def test_load_gate_config_at_reads_revision(git_repo):
    _commit_toml(git_repo, '[gates]\ntests = "off"\n')
    (git_repo / "rustflow.toml").write_text('[gates]\ntests = "auto"\n', encoding="utf-8")
    assert gates.load_gate_config_at(git_repo, "HEAD")["tests"] == "off"


def test_load_gate_config_at_invalid_toml(git_repo):
    _commit_toml(git_repo, "[gates\n")
    cfg = gates.load_gate_config_at(git_repo, "HEAD")
    assert cfg["_config_error"] and cfg["tests"] == "auto"


def test_load_gate_config_at_bad_rev(git_repo):
    with pytest.raises(gates.GateConfigError):
        gates.load_gate_config_at(git_repo, "nie-ma")


def test_load_gate_config_at_not_a_repo(tmp_path):
    with pytest.raises(gates.GateConfigError):
        gates.load_gate_config_at(tmp_path, "HEAD")


def test_new_failures_multiset():
    a = "clippy|a|x|m"
    assert gates.new_failures(Counter({a: 2}), Counter({a: 1})) == Counter({a: 1})
    assert not gates.new_failures(Counter({a: 1}), Counter({a: 1}))
    assert gates.new_failures(Counter({"fmt|*": 1}), Counter({"fmt|*": 1})) == Counter({"fmt|*": 1})


def test_signature_counts_keeps_duplicates():
    d = gates.Diagnostic("error", "unneeded `return` statement", "clippy::needless_return", "src/lib.rs", 3)
    res = gates.GateResult("full", False, None, steps=[gates.Step("clippy", False, diagnostics=[d, d])])
    key = "clippy|src/lib.rs|clippy::needless_return|unneeded `return` statement"
    assert res.signature_counts()[key] == 2
    assert res.signatures() == {key}


def test_load_gate_config_at_missing_blob_fails_closed(git_repo):
    _commit_toml(git_repo, '[gates]\ntests = "off"\n')
    blob = subprocess.run(["git", "rev-parse", "HEAD:rustflow.toml"], cwd=git_repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    obj = git_repo / ".git" / "objects" / blob[:2] / blob[2:]
    obj.chmod(0o644)
    obj.unlink()
    with pytest.raises(gates.GateConfigError):
        gates.load_gate_config_at(git_repo, "HEAD")


def test_review_schema_gate_changes_optional():
    base = {"summary": "s", "verdict": "APPROVE", "findings": []}
    assert schemas.validate(dict(base), schemas.REVIEW_SCHEMA)["gate_changes"] == []
    ok = schemas.validate({**base, "gate_changes": [{"id": "G-x", "decision": "accept", "reason": "r"}]},
                          schemas.REVIEW_SCHEMA)
    assert ok["gate_changes"][0]["decision"] == "accept"
    with pytest.raises(schemas.SchemaError):
        schemas.validate({**base, "gate_changes": [{"id": "G-x", "decision": "maybe", "reason": ""}]},
                         schemas.REVIEW_SCHEMA)


def test_review_prompt_lists_pending_changes():
    from rustflow import prompts
    ctx = prompts.ReviewContext(task="t", base="b", head_tree="h", diff_stat="", diff_inline="", gates_summary="",
                                gate_changes="- [G-cargo-lints-Cargo.toml] cargo-lints in `Cargo.toml`: x")
    p = prompts.review_prompt(ctx, "correctness", "en", "reviewer_a")
    assert "GATE-AFFECTING CHANGES NEEDING YOUR VOTE" in p and "[G-cargo-lints-Cargo.toml]" in p
    ctx.gate_changes = ""
    assert "NEEDING YOUR VOTE" not in prompts.review_prompt(ctx, "correctness", "en", "reviewer_a")


def test_report_gate_changes_section():
    from pathlib import Path
    from rustflow import report
    entry = {"id": "G-config-clippy.toml", "path": "clippy.toml", "kind": "config", "status": "accepted",
             "accepted_by": "proof", "proof": "passed", "votes": {"a": {"decision": "accept"}},
             "justification": "ok | fine"}
    text = report.render({"run_id": "r", "status": "approved_with_advisories", "gate_changes": [entry]}, Path("."))
    assert "## Zmiany wpływające na bramki" in text
    assert "| id | rodzaj | ścieżka | status | accepted_by | dowód | głosy | uzasadnienie |" in text
    row = next(ln for ln in text.splitlines() if ln.startswith("| G-config-clippy.toml"))
    assert "ok \\| fine" in row and "proof" in row and "passed" in row and "a: accept" in row
    assert "Zmiany wpływające na bramki" not in report.render({"run_id": "r", "status": "approved"}, Path("."))


def test_report_gate_changes_pending_resume_line():
    from pathlib import Path
    from rustflow import report
    text = report.render({"run_id": "r9", "status": "gate_changes_pending"}, Path("."))
    assert "rustflow resume r9 --extra-rounds 1" in text


def test_report_gate_changes_row_survives_cr():
    from pathlib import Path
    from rustflow import report
    entry = {"id": "G-config-a\rb", "path": "a\rb\x0bc\u2028d\x85e", "kind": "config", "status": "pending"}
    text = report.render({"run_id": "r", "status": "gate_changes_pending", "gate_changes": [entry]}, Path("."))
    assert len([ln for ln in text.splitlines() if "G-config-a" in ln]) == 1
    assert "G-config-a" in next(ln for ln in text.splitlines() if "G-config-a" in ln) and "d" in text


def test_integrity_extra_paths_default_is_empty():
    from rustflow.config import load_defaults
    assert load_defaults()["gates"]["integrity_extra_paths"] == []


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_doctor_warns_when_rustflow_toml_is_not_committed(tmp_path):
    from rustflow.doctor import Doctor
    _git(tmp_path, "init", "-q")
    (tmp_path / "rustflow.toml").write_text("[gates]\n", encoding="utf-8")
    lines: list[str] = []
    Doctor(tmp_path, Config(load_defaults()), out=lines.append).check_config_tracked()
    assert any("[WARN]" in l and "rustflow.toml" in l for l in lines), lines
    _git(tmp_path, "add", "rustflow.toml")
    lines.clear()
    Doctor(tmp_path, Config(load_defaults()), out=lines.append).check_config_tracked()
    assert lines == []


def test_doctor_flags_missing_integrity_script(tmp_path):
    from rustflow.doctor import Doctor
    lines: list[str] = []
    Doctor(tmp_path, Config(load_defaults()), out=lines.append).run()
    assert any(("[FAIL]" in l or "[WARN]" in l) and "integrity.py" in l for l in lines), lines
