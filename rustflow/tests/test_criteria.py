"""Mechanical acceptance criteria (named tests), tree oscillation, gate test-name parsing."""

import json

import pytest

from rustflow import criteria as cm
from rustflow import gates
from rustflow.config import Config
from rustflow.orchestrator import Options, Orchestrator, find_run

TASK = "Add `parse_kv(input: &str) -> Vec<(String, String)>` to src/lib.rs. Trim whitespace. Add tests."


# ── units ────────────────────────────────────────────────────────────────────
CARGO_OUT = """
running 3 tests
test tests::parses_pairs ... ok
test tests::trims_whitespace ... FAILED
test tests::slow ... ignored
test parser::tests::trims_whitespace ... ok

running 1 test
test src/lib.rs - parse_kv (line 3) ... ok
"""

NEXTEST_OUT = """
    Starting 3 tests across 2 binaries
        PASS [   0.004s] demo tests::parses_pairs
        PASS [   0.003s] (2/3) demo::handlers routes::ok_page
        FAIL [   0.010s] demo tests::trims_whitespace
"""

# Verbatim from cargo-nextest 0.9.149: integration test at the crate root of tests/handlers.rs.
NEXTEST_REAL = """        PASS [   0.009s] (1/2) nx tests::unit_ok
        PASS [   0.009s] (2/2) nx::handlers integ_ok
     Summary [   0.009s] 2 tests run: 2 passed, 1 skipped
"""


def test_parse_passed_tests_libtest_and_nextest():
    assert gates.parse_passed_tests(CARGO_OUT) == [
        "tests::parses_pairs", "parser::tests::trims_whitespace", "src/lib.rs - parse_kv (line 3)"]
    assert gates.parse_passed_tests(NEXTEST_OUT) == ["demo tests::parses_pairs", "demo::handlers routes::ok_page"]


def test_test_status_exact_short_failed_and_missing():
    passed = gates.parse_passed_tests(CARGO_OUT)
    failed = gates.parse_failed_tests(CARGO_OUT)
    assert cm.test_status("tests::parses_pairs", passed, failed)[0] == "met"
    assert cm.test_status("parses_pairs", passed, failed)[0] == "met"           # short name, unique
    # "trims_whitespace" matches a passing and a failing test -> not met
    assert cm.test_status("trims_whitespace", passed, failed) == ("not_met", "failed: tests::trims_whitespace")
    assert cm.test_status("parser::tests::trims_whitespace", passed, failed)[0] == "met"
    assert cm.test_status("tests::slow", passed, failed)[0] == "not_met"        # ignored != passed
    assert cm.test_status("tests::nope", passed, failed)[1].startswith("did not run")
    assert cm.test_status("src/lib.rs - parse_kv (line 3)", passed, failed)[0] == "met"
    np = gates.parse_passed_tests(NEXTEST_OUT)
    assert cm.test_status("tests::parses_pairs", np, [])[0] == "met"
    assert cm.test_status("handlers::routes::ok_page", np, [])[0] == "met"     # binary-id::test form
    assert cm.test_status("ok_page", np, [])[0] == "met"
    real = gates.parse_passed_tests(NEXTEST_REAL)
    assert real == ["nx tests::unit_ok", "nx::handlers integ_ok"]
    for name in ("integ_ok", "handlers::integ_ok", "tests::unit_ok", "unit_ok"):
        assert cm.test_status(name, real, [])[0] == "met", name
    assert cm.test_status("skipped", real, [])[0] == "not_met"


def test_evaluate_reports_skipped_tests_step():
    steps = [gates.Step("tests", False, skipped=True, reason="skipped: code does not compile")]
    res = cm.evaluate([cm.Criterion("C1", "trims", "tests::trims_whitespace")], steps)
    assert res[0]["status"] == "not_met" and "code does not compile" in res[0]["detail"]
    assert cm.evaluate([cm.Criterion("C2", "semantic only")], steps) == []


def test_load_file_and_errors(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[[criteria]]\ntext = "trims"\ntest = "tests::trims_whitespace"\n'
                 '[[criteria]]\nid = "api"\ntext = "public API unchanged"\n', encoding="utf-8")
    got = cm.load_file(f)
    assert [(c.id, c.test, c.source) for c in got] == [("C1", "tests::trims_whitespace", "file"), ("api", "", "file")]
    for bad, msg in (('[[criteria]]\ntext = ""\n', "non-empty"), ('x = 1\n', "at least one"),
                     ('[[criteria]]\ntext = "a"\nfoo = 1\n', "unknown keys"),
                     ('[[criteria]]\nid = "a"\ntext = "a"\n[[criteria]]\nid = "a"\ntext = "b"\n', "duplicate")):
        f.write_text(bad, encoding="utf-8")
        with pytest.raises(cm.CriteriaError, match=msg):
            cm.load_file(f)
    with pytest.raises(cm.CriteriaError, match="not found"):
        cm.load_file(tmp_path / "missing.toml")


def test_from_plan_accepts_strings_and_objects_and_dedupes():
    existing = [cm.Criterion("C1", "Trim whitespace", "tests::trims", "file")]
    plan = {"acceptance_criteria": ["trim whitespace", {"text": "empty input -> empty vec", "test": "tests::trims"},
                                    {"text": "errors are typed", "test": "`tests::typed`"}]}
    got = cm.from_plan(plan, existing)
    assert [(c.id, c.text, c.test) for c in got] == [("C2", "empty input -> empty vec", ""),
                                                     ("C3", "errors are typed", "tests::typed")]


# ── end to end ───────────────────────────────────────────────────────────────
def run(repo, scenario, monkeypatch, **opts):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    rc = Orchestrator(repo, Config.load(repo), Options(**opts)).start(TASK)
    run_dir = find_run(repo, "latest")
    return rc, json.loads((run_dir / "state.json").read_text(encoding="utf-8")), run_dir


def criteria_file(tmp_path, test):
    f = tmp_path / "criteria.toml"
    f.write_text(f'[[criteria]]\ntext = "keys and values are trimmed"\ntest = "{test}"\n', encoding="utf-8")
    return str(f)


def test_required_test_blocks_until_it_exists(rust_repo, monkeypatch, tmp_path):
    rc, st, run_dir = run(rust_repo, "happy", monkeypatch,
                          criteria_file=criteria_file(tmp_path, "tests::trims_whitespace"))
    assert st["status"].startswith("approved"), st.get("stop_reason")
    r1 = st["rounds"][0]
    t = next(f for f in r1["findings"] if f["id"] == "T-C1")
    assert t["status"] == "open" and t["reviewers"] == ["orchestrator"] and "did not run" in t["title"]
    assert r1["mechanical"][0]["status"] == "not_met"
    [m] = st["mechanical"]
    assert (m["id"], m["test"], m["status"]) == ("C1", "tests::trims_whitespace", "met")
    assert m["detail"].endswith("tests::trims_whitespace")  # "demo tests::..." under nextest
    impl_prompt = (run_dir / "agents" / "implement.prompt.md").read_text(encoding="utf-8")
    assert "required test `tests::trims_whitespace`" in impl_prompt
    review_prompt = (run_dir / "agents" / "r1-reviewer_a.prompt.md").read_text(encoding="utf-8")
    assert "[C1] keys and values are trimmed" in review_prompt
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Kryteria akceptacji" in report and "`tests::trims_whitespace`: **OK**" in report


def test_missing_required_test_is_never_approved(rust_repo, monkeypatch, tmp_path):
    rc, st, _ = run(rust_repo, "happy", monkeypatch, criteria_file=criteria_file(tmp_path, "tests::never_written"))
    assert rc != 0
    assert st["status"] in ("stalled", "max_rounds"), st["status"]
    assert any(f["id"] == "T-C1" and f["status"] == "open" for f in st["findings"])
    # Reviewers approved the code itself in round 2; only the orchestrator's check blocks.
    assert st["rounds"][1]["verdicts"] == {"reviewer_a": "APPROVE", "reviewer_b": "APPROVE"}


def test_planner_names_the_required_test(rust_repo, monkeypatch):
    monkeypatch.setenv("FAKE_PLAN_TEST", "trims_whitespace")
    rc, st, _ = run(rust_repo, "happy", monkeypatch, difficulty="hard")
    assert st["status"].startswith("approved"), st.get("stop_reason")
    assert st["criteria"] == [{"id": "C1", "text": "trim whitespace", "test": "trims_whitespace", "source": "plan"}]
    assert st["mechanical"][0]["status"] == "met"


def test_bad_criteria_file_fails_before_anything_runs(rust_repo, monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("nothing = 1\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_SCENARIO", "happy")
    rc = Orchestrator(rust_repo, Config.load(rust_repo), Options(criteria_file=str(bad))).start(TASK)
    assert rc == 2
    assert not (rust_repo / ".rustflow" / "runs").exists() or not any((rust_repo / ".rustflow" / "runs").iterdir())


def test_fixer_oscillation_stops_the_loop(rust_repo, monkeypatch):
    rc, st, _ = run(rust_repo, "oscillate", monkeypatch, max_rounds=5)
    assert st["status"] == "stalled", st["status"]
    assert len(st["rounds"]) == 2  # stopped after the second fix returned to the round-1 tree
