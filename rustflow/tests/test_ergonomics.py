"""Claude Code ergonomics: tool set in the prompt, transient agent failures, structured-output repair,
hook feedback size, continuing a finished run, re-review payload."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git, read_log
from rustflow import agents, gates, hooks, runs
from rustflow.config import AgentSpec, Config, ConfigError, load_defaults
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


def _run(repo, scenario, monkeypatch, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    cfg = Config.load(repo, opts.pop("config", None))
    rc = Orchestrator(repo, cfg, Options(**opts)).start(TASK)
    return rc, _state(repo)


def _state(repo):
    return json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


def _resume(repo, monkeypatch, scenario, **kw):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    rc = Orchestrator(repo, Config.load(repo), Options()).resume(find_run(repo, "latest"), **kw)
    return rc, _state(repo)


def _call(tmp_path, role="reviewer_a", write=False, **kw):
    return agents.AgentCall(role, "x", AgentSpec.parse("claude:opus:high"), "p", write, None, tmp_path,
                            tmp_path / "logs", **kw)


def _arg(argv, flag):
    return argv[argv.index(flag) + 1]


# ── tool set in the prompt (--tools) ─────────────────────────────────────────
def test_builtin_tool_set_is_restricted_per_role(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "IS_WINDOWS", False)
    runner = agents.ClaudeRunner(Config(load_defaults()))
    read = runner.build_argv(_call(tmp_path))
    assert _arg(read, "--tools") == "Read,Glob,Grep,LSP,Bash"          # no Skill: /skill expands without it
    write = runner.build_argv(_call(tmp_path, "implementer", write=True))
    assert _arg(write, "--tools") == "Read,Edit,Write,Glob,Grep,LSP,Bash"
    # A call with its own tool list (Trail of Bits audit needs Agent/Task*) keeps the full built-in set.
    tob = runner.build_argv(_call(tmp_path, "tob_audit", write=True, allowed_tools=["Agent", "Read"]))
    assert "--tools" not in tob


def test_powershell_added_on_windows_and_empty_list_disables_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "IS_WINDOWS", True)
    d = load_defaults()
    assert _arg(agents.ClaudeRunner(Config(d)).build_argv(_call(tmp_path)), "--tools").endswith(",Bash,PowerShell")
    d["providers"]["claude"]["tools_read"] = []
    assert "--tools" not in agents.ClaudeRunner(Config(d)).build_argv(_call(tmp_path))


def test_fallback_model_flag_and_allowlist(tmp_path):
    d = load_defaults()
    assert "--fallback-model" not in agents.ClaudeRunner(Config(d)).build_argv(_call(tmp_path))
    d["providers"]["claude"]["fallback_model"] = "sonnet"
    assert _arg(agents.ClaudeRunner(Config(d)).build_argv(_call(tmp_path)), "--fallback-model") == "sonnet"
    d["providers"]["claude"]["fallback_model"] = "gpt-9"
    with pytest.raises(ConfigError, match="fallback_model"):
        Config(d)


# ── transient failures are retried / resumable ──────────────────────────────
def test_write_role_is_retried_when_the_tree_is_unchanged(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "impl_flaky", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert [c["role"] for c in read_log(rust_repo)][:2] == ["implementer", "implementer"]


def test_failed_implementer_is_resumable_not_no_changes(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "impl_down", monkeypatch)
    assert st["status"] == "agent_failed" and st["phase"] == "implement", st.get("stop_reason")
    assert st["failed_agent"]["retryable"] is True
    acts = runs.next_actions(st)
    assert acts[0]["action"] == "resume" and "--detach" in acts[0]["command"]
    rc2, st2 = _resume(rust_repo, monkeypatch, "happy")
    assert st2["status"].startswith("approved"), st2.get("stop_reason")
    assert "stop_reason" not in st2                       # the old failure does not leak into the final report


def test_all_reviewers_down_is_resumable(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "reviewers_down", monkeypatch)
    assert st["status"] == "agent_failed" and st["phase"] == "review", st.get("stop_reason")
    rc2, st2 = _resume(rust_repo, monkeypatch, "happy")
    assert st2["status"].startswith("approved"), st2.get("stop_reason")


# ── structured output ───────────────────────────────────────────────────────
def test_fixer_structured_output_is_repaired_in_its_own_session(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "fix_json", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["rounds"][0]["fix"]["resolutions"][0]["status"] == "fixed"
    fixer = [c for c in read_log(rust_repo) if c["role"] == "fixer"]
    assert len(fixer) == 2 and "--resume" in fixer[1]["argv"]
    # Same argv apart from --resume: same tools, system prompt and schema -> the session's cache prefix holds.
    assert [a for a in fixer[1]["argv"] if a not in ("--resume", "sess-fix")] == fixer[0]["argv"]
    agent = next(a for a in st["agents"] if a["name"] == "fix-r1")
    assert agent["ok"] and agent["json_repair"] and agent["cost_usd"] == 0.5


def test_gate_fix_runs_without_output_schema(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "gatefix", monkeypatch)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    gate_fix = next(c for c in read_log(rust_repo) if c["role"] == "fixer")
    assert "--json-schema" not in gate_fix["argv"]


def test_permission_denials_are_recorded(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "denials", monkeypatch)
    impl = next(a for a in st["agents"] if a["role"] == "implementer")
    assert impl["permission_denials"] == ["Bash(ls -la src)"] and impl["num_turns"] == 3
    report = (find_run(rust_repo, "latest") / "report.md").read_text(encoding="utf-8")
    assert "Bash(ls -la src)" in report


def test_timeout_books_the_role_budget(tmp_path, fake_bin, monkeypatch):
    d = load_defaults()
    d["providers"]["claude"]["bin"] = str(fake_bin / "claude.py")
    monkeypatch.setenv("RUSTFLOW_ROLE", "implementer")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    monkeypatch.delenv("FAKE_LOG", raising=False)
    (tmp_path / "src").mkdir()
    call = _call(tmp_path, "implementer", write=True, timeout_s=1, max_budget_usd=15.0)
    res = agents.ClaudeRunner(Config(d)).run(call)
    assert not res.ok and "timeout" in res.error
    assert res.cost_usd == 15.0 and res.cost_estimated


# ── hook feedback ───────────────────────────────────────────────────────────
def _red(n, detail=20):
    diags = [gates.Diagnostic("error", f"problem {i}", code="E0308", file="src/lib.rs", line=i,
                              rendered=f"error[E0308]: problem {i}\n" + "  | detail\n" * detail) for i in range(n)]
    return gates.GateResult("stop", False, None, steps=[gates.Step("clippy", False, ["cargo", "clippy"],
                                                                 diagnostics=diags)])


def test_feedback_truncation_keeps_the_first_errors():
    text = _red(40).feedback(max_chars=2000)
    assert len(text) < 2200
    assert text.startswith("### GATE FAILED: clippy") and "problem 0" in text
    assert "problem 39" not in text and "cut" in text


def test_stop_hook_reason_starts_with_the_first_error(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    monkeypatch.setattr(hooks.gates, "find_root", lambda *a: tmp_path)
    monkeypatch.setattr(hooks.gates, "run_gates", lambda *a, **k: _red(60, detail=80))
    monkeypatch.setenv("RUSTFLOW_ROLE", "implementer")
    out = []
    monkeypatch.setattr(hooks, "_emit", lambda obj: out.append(obj) or 0)
    hooks.stop({"session_id": "s"})
    reason = out[0]["reason"]
    assert "### GATE FAILED: clippy" in reason and "problem 0" in reason and len(reason) < 9000


def _hook(repo, sub, payload):
    import os
    env = {k: v for k, v in os.environ.items() if k != "RUSTFLOW_ROLE"}
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    return subprocess.run([sys.executable, str(repo / ".rustflow" / "hooks.py"), sub], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=repo)


def test_post_edit_formats_only_the_edited_file(rust_repo):
    other = "pub fn   ugly( )->i32{1}\n"
    (rust_repo / "src" / "other.rs").write_text(other)
    lib = rust_repo / "src" / "lib.rs"
    lib.write_text("//! demo crate\npub mod other;\npub fn  f( )->i32{2}\n")
    r = _hook(rust_repo, "post-edit", {"session_id": "e1", "tool_input": {"file_path": str(lib)}})
    assert r.returncode == 0, r.stderr
    assert "pub fn f() -> i32 {" in lib.read_text()                       # the edited file is formatted
    assert (rust_repo / "src" / "other.rs").read_text() == other          # its child module is not


def test_post_edit_repeats_unchanged_errors_briefly(rust_repo):
    lib = rust_repo / "src" / "lib.rs"
    lib.write_text("".join(f"pub fn f{i}() -> i32 {{\n    \"x\"\n}}\n" for i in range(4)))   # mid-refactor
    payload = {"session_id": "e2", "tool_input": {"file_path": str(lib)}}
    first, second = _hook(rust_repo, "post-edit", payload), _hook(rust_repo, "post-edit", payload)
    assert first.returncode == second.returncode == 2
    assert "E0308" in first.stderr and "still the same 4 compile error" in second.stderr
    assert len(second.stderr) < len(first.stderr) / 2
    lib.write_text("pub fn f() -> i32 {\n    1\n}\n")
    assert _hook(rust_repo, "post-edit", payload).returncode == 0
    lib.write_text("pub fn f() -> i32 {\n    \"x\"\n}\n")                     # green in between: full report again
    assert "E0308" in _hook(rust_repo, "post-edit", payload).stderr


# ── continuing a finished run ───────────────────────────────────────────────
def test_stalled_run_continues_with_extra_rounds(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "stall", monkeypatch)
    assert st["status"] == "stalled"
    act = runs.next_actions(st)[0]
    assert act["command"] == f"rustflow resume {st['run_id']} --extra-rounds 2 --detach"
    rc2, st2 = _resume(rust_repo, monkeypatch, "happy", extra_rounds=2)
    assert st2["status"].startswith("approved"), st2.get("stop_reason")
    assert git(rust_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == st["branch"]
    assert "fix round" in git(rust_repo, "log", "--format=%s", "-1")     # checkpoints continue on the run branch


def test_extra_rounds_refused_for_an_approved_run(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "happy", monkeypatch)
    assert st["status"].startswith("approved")
    rc2, st2 = _resume(rust_repo, monkeypatch, "happy", extra_rounds=1)
    assert rc2 == 2 and st2["status"] == st["status"]


def test_worktree_run_continues_in_its_worktree_and_offers_merge(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "stall", monkeypatch, isolation="worktree")
    assert st["status"] == "stalled"
    rc2, st2 = _resume(rust_repo, monkeypatch, "happy", extra_rounds=2)
    assert st2["status"].startswith("approved"), st2.get("stop_reason")
    assert ".trim()" in (Path(st2["workdir"]) / "src" / "lib.rs").read_text()
    merge = next(a for a in runs.next_actions(st2) if a["action"] == "merge")
    assert merge["command"] == f"git merge --squash {st2['branch']}" and merge["needs_user"]
    assert "git worktree remove" in merge["note"]


def test_cli_and_mcp_forward_extra_rounds(monkeypatch, tmp_path):
    from rustflow import cli, mcp_server
    seen = []
    monkeypatch.setattr(runs, "spawn_detached", lambda repo, args, task, run_id=None: seen.append(args) or
                        {"status": "started", "run_id": run_id, "pid": 1})
    monkeypatch.setattr(runs, "find_run", lambda repo, ref: tmp_path / "20261026-x")
    monkeypatch.setattr(mcp_server, "_repo", lambda: tmp_path)
    mcp_server.t_resume({"run": "x", "extra_rounds": 2})
    assert seen[-1] == ["resume", "20261026-x", "--extra-rounds", "2"]
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "rustflow_resume")
    assert "extra_rounds" in tool["inputSchema"]["properties"]
    monkeypatch.setattr("rustflow.orchestrator.find_run", lambda repo, ref: tmp_path / "20261026-x")
    cli.main(["resume", "x", "--extra-rounds", "3", "--detach", "--repo", str(tmp_path)])
    assert seen[-1][:4] == ["resume", "20261026-x", "--extra-rounds", "3"]


# ── re-review payload ───────────────────────────────────────────────────────
def test_rereview_inlines_the_delta_not_the_whole_diff(rust_repo, monkeypatch, tmp_path):
    cfg = tmp_path / "rf.toml"
    cfg.write_text((rust_repo / "rustflow.toml").read_text(encoding="utf-8") + "\n", encoding="utf-8")
    text = cfg.read_text(encoding="utf-8").replace("rereview_full_diff_max_kb = 16", "rereview_full_diff_max_kb = 0", 1)
    cfg.write_text(text, encoding="utf-8")
    rc, st = _run(rust_repo, "happy", monkeypatch, config=cfg)
    assert st["status"].startswith("approved"), st.get("stop_reason")
    agents_dir = find_run(rust_repo, "latest") / "agents"
    r1 = (agents_dir / "r1-reviewer_a.prompt.md").read_text(encoding="utf-8")
    r2 = (agents_dir / "r2-reviewer_a.prompt.md").read_text(encoding="utf-8")
    assert "## DIFF\n\n```diff" in r1
    assert "## DIFF\n\n```diff" not in r2 and "## DELTA SINCE PREVIOUS REVIEW" in r2
    assert "```diff" in r2.split("## DELTA SINCE PREVIOUS REVIEW")[1].split("## DIFF")[0]


def test_failed_json_repair_is_not_retried_a_second_time(tmp_path, fake_bin, monkeypatch):
    """Repair IS the retry for a structured-output failure: no full rerun (+ another repair) on top of it."""
    d = load_defaults()
    d["providers"]["claude"]["bin"] = str(fake_bin / "claude.py")
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SCENARIO", "json_always")
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    from rustflow import schemas
    call = agents.AgentCall("reviewer_a", "r1-reviewer_a", AgentSpec.parse("claude:opus:high"), "p", False,
                            schemas.REVIEW_SCHEMA, tmp_path, tmp_path / "logs")
    res = agents.run_with_retry(agents.ClaudeRunner(Config(d)), call, retries=1)
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(calls) == 2 and "--resume" in calls[1]["argv"]
    assert not res.ok and res.json_repair and "JSON repair failed" in res.error
    assert res.cost_usd == pytest.approx(0.2)
