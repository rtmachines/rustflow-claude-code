"""Permanent vs transient agent failures, observed model, duplicate evidence, review lenses."""

import json

import pytest

from conftest import read_log
from rustflow import agents, findings as fnd
from rustflow.config import Config, ConfigError
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


@pytest.mark.parametrize("text,status,permanent", [
    ("Invalid API key · Please run /login", None, True),
    ("API Error: 404 {\"type\":\"not_found_error\",\"message\":\"model: claude-opus-9\"}", None, True),
    ("error: unknown option '--exclude-dynamic-system-prompt-sections'", None, True),
    ("The model `gpt-7` does not exist or you do not have access to it", None, True),
    ("anything", 401, True),
    ("API Error: 529 overloaded_error", 529, False),
    ("Request timed out / connection reset", None, False),
    ("invalid structured output: $.findings: expected array", None, False),
])
def test_classify_permanent(text, status, permanent):
    assert agents.classify_permanent(text, status) is permanent


class _Runner:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def run(self, call):
        self.calls += 1
        return self.results.pop(0)


def _call(tmp_path):
    from rustflow.config import AgentSpec
    return agents.AgentCall("reviewer_a", "r1-reviewer_a", AgentSpec.parse("claude:opus:high"), "p", False, None,
                            tmp_path, tmp_path)


def test_run_with_retry_skips_permanent_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    perm = agents.AgentResult("x", "reviewer_a", "s", ok=False, error="not logged in", retryable=False)
    r = _Runner([perm])
    assert agents.run_with_retry(r, _call(tmp_path), retries=1) is perm and r.calls == 1
    flaky = agents.AgentResult("x", "reviewer_a", "s", ok=False, error="boom")
    good = agents.AgentResult("x", "reviewer_a", "s", ok=True)
    r = _Runner([flaky, good])
    assert agents.run_with_retry(r, _call(tmp_path), retries=1).ok and r.calls == 2


def test_observed_model_and_match():
    payload = {"modelUsage": {"claude-haiku-5-5": {"outputTokens": 10},
                              "claude-opus-5-5-20261001": {"outputTokens": 900, "canonicalModel": "claude-opus-5-5"}}}
    assert agents.observed_claude_model(payload) == "claude-opus-5-5"
    assert agents.observed_claude_model({}) == ""
    assert agents.model_matches("opus", "claude-opus-5-5")
    assert agents.model_matches("claude-opus-5-5", "claude-opus-5-5-20261001")
    assert agents.model_matches("gpt-6.1-sol", "gpt-6.1-sol")
    assert agents.model_matches("opus", "")          # unknown = no claim
    assert not agents.model_matches("opus", "claude-sonnet-5-5")
    assert not agents.model_matches("gpt-6-sol", "gpt-6.1-sol")


def test_merge_keeps_the_other_reviewers_scenario():
    a = fnd.Finding("", "P2", "correctness", "src/lib.rs", 10, "Key not trimmed", trigger="' a=1'",
                    reviewers=["reviewer_a"])
    b = fnd.Finding("", "P1", "correctness", "src/lib.rs", 11, "Value keeps trailing space", trigger="'a= 1 '",
                    reviewers=["reviewer_b"])
    fnd.merge_into(a, b)
    assert a.severity == "P1" and a.title == "Value keeps trailing space"
    assert a.also == [{"reviewer": "reviewer_a", "file": "src/lib.rs", "line": 10, "title": "Key not trimmed",
                       "trigger": "' a=1'", "impact": ""}]
    text = a.for_prompt()
    assert "Also reported by reviewer_a at src/lib.rs:10: Key not trimmed - trigger: ' a=1'" in text
    # Old state.json without `also` still loads.
    assert fnd.Finding.from_dict({k: v for k, v in a.to_dict().items() if k != "also"}).also == []


def test_lens_is_validated(tmp_path):
    (tmp_path / "rustflow.toml").write_text('[roles.reviewer_b]\nlens = "vibes"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="lens"):
        Config.load(tmp_path)
    assert Config.load(tmp_path / "nothing").get("roles.reviewer_b.lens") == "regressions"


def _run(repo, scenario, monkeypatch, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    rc = Orchestrator(repo, Config.load(repo), Options(**opts)).start(TASK)
    return rc, json.loads((find_run(repo, "latest") / "state.json").read_text(encoding="utf-8"))


def test_permanent_codex_failure_is_not_retried(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "codex_auth", monkeypatch)
    codex_calls = [c for c in read_log(rust_repo) if c["provider"] == "codex"]
    rounds = len(st["rounds"])
    assert rounds >= 1 and len(codex_calls) == rounds          # one attempt per round, no retry
    assert all(a["retryable"] is False for a in st["agents"] if a["role"] == "reviewer_b")


def test_transient_codex_failure_is_retried(rust_repo, monkeypatch):
    rc, st = _run(rust_repo, "codex_down", monkeypatch)
    codex_calls = [c for c in read_log(rust_repo) if c["provider"] == "codex"]
    assert len(codex_calls) == 2 * len(st["rounds"])            # first try + one retry per round


def test_model_mismatch_is_reported(rust_repo, monkeypatch):
    monkeypatch.setenv("FAKE_MODEL_REPORT", "claude-haiku-5-5")
    rc, st = _run(rust_repo, "happy", monkeypatch)
    impl = next(a for a in st["agents"] if a["role"] == "implementer")
    assert impl["observed_model"] == "claude-haiku-5-5"
    assert impl["model_mismatch"] == "requested sonnet, CLI reports claude-haiku-5-5"
    assert any(e["kind"] == "model_mismatch" for e in st["events"])


def test_setting_sources_option(tmp_path, fake_defaults):
    from rustflow.config import AgentSpec
    d = fake_defaults
    call = agents.AgentCall("reviewer_a", "x", AgentSpec.parse("claude:opus:high"), "p", False, None, tmp_path,
                            tmp_path / "logs")
    assert "--setting-sources" not in agents.ClaudeRunner(Config(d)).build_argv(call)
    d["providers"]["claude"]["setting_sources"] = "project, local"
    argv = agents.ClaudeRunner(Config(d)).build_argv(call)
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    d["providers"]["claude"]["setting_sources"] = "project,global"
    with pytest.raises(ConfigError, match="setting_sources"):
        Config(d)


def test_doctor_hints_at_user_level_hooks(tmp_path, monkeypatch):
    from rustflow.doctor import Doctor
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"hooks": {"PreToolUse": []}}', encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    lines: list[str] = []
    Doctor(tmp_path, Config.load(tmp_path / "x"), out=lines.append).check_user_settings()
    assert any("[INFO]" in l and "setting_sources" in l for l in lines)
    from rustflow.config import load_defaults
    d = load_defaults()
    d["providers"]["claude"]["setting_sources"] = "project,local"
    lines.clear()
    Doctor(tmp_path, Config(d), out=lines.append).check_user_settings()
    assert lines == []
