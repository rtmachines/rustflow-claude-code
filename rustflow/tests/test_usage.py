"""Measurements are checked against hand-counted provider fixtures, never invented zeros."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("raw,cost,status,total", [
    (None, None, "unknown", None),
    ({}, None, "unknown", None),
    ({"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0}, 0, "complete", 0),
    ({"input_tokens": 10}, None, "partial", None),
    ({"input_tokens": -1, "output_tokens": True, "cache_creation_input_tokens": "4",
      "cache_read_input_tokens": float("nan")}, -1, "unknown", None),
    ({"input_tokens": float("inf"), "output_tokens": 1.5}, "0.2", "unknown", None),
    ([], float("inf"), "unknown", None),
    ("bad", True, "unknown", None),
])
def test_measurement_completeness(raw, cost, status, total):
    from rustflow import usage
    measured = usage.normalize("claude", raw, cost)
    assert measured["measurement_status"] == status
    assert measured["tokens"]["total_tokens"] == total
    assert measured["measured_cost_usd"] == (0 if status == "complete" else None)


def test_cache_semantics_and_multiple_codex_turns():
    from rustflow import usage
    fixtures = Path(__file__).parent / "fixtures"
    claude = json.loads((fixtures / "claude_usage.json").read_text())[0]
    c = usage.normalize("claude", claude["usage"], claude["total_cost_usd"])
    assert c["tokens"]["total_tokens"] == 20  # 10 + 2 + 3 + 5
    raw = [json.loads(line)["usage"] for line in (fixtures / "codex_usage.jsonl").read_text().splitlines()
           if json.loads(line)["type"] == "turn.completed"]
    d = usage.normalize("codex", raw, None)
    assert d["tokens"]["total_tokens"] == 170  # input already includes cached input
    assert d["tokens"]["cache_read_input_tokens"] == 110
    assert d["measurement_status"] == "partial"  # no USD measurement


def test_partial_turn_does_not_invent_a_complete_total():
    from rustflow import usage
    measured = usage.normalize("codex", [{"input_tokens": 10, "output_tokens": 2}, {"input_tokens": 20}], None)
    assert measured["tokens"]["output_tokens"] is None
    assert measured["tokens"]["total_tokens"] is None
    assert measured["known_tokens"]["output_tokens"] == 2
    assert measured["known_tokens"]["total_tokens"] == 32


def test_zero_in_a_partial_turn_is_still_a_measurement():
    from rustflow import usage
    measured = usage.normalize("codex", [{"input_tokens": 0}, None], None)
    assert measured["measurement_status"] == "partial"
    assert measured["tokens"]["input_tokens"] is None
    assert measured["known_tokens"]["input_tokens"] == 0


def _role_rows():
    from rustflow import usage
    return [
        {"role": "implementer", "provider": "claude", "estimated_cost_usd": None,
         **usage.normalize("claude", {"input_tokens": 2, "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 300, "output_tokens": 10}, 0.5)},
        {"role": "implementer", "provider": "claude", "estimated_cost_usd": None,
         **usage.normalize("claude", {"input_tokens": 0, "cache_creation_input_tokens": 50,
                                      "cache_read_input_tokens": 50, "output_tokens": 5}, 0.25)},
        {"role": "reviewer_b", "provider": "codex", "estimated_cost_usd": None,
         **usage.normalize("codex", [{"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}], None)},
    ]


def test_summary_breaks_usage_down_by_role():
    # Live runs: cache writes were ~54% of the Claude cost, invisible in the per-provider totals.
    from rustflow import usage
    roles = usage.summarize(_role_rows())["roles"]
    impl = roles["implementer"]
    assert impl["attempt_count"] == 2 and impl["cost_usd"]["total"] == 0.75
    assert impl["tokens"]["cache_creation_input_tokens"] == 150 and impl["tokens"]["output_tokens"] == 15
    assert impl["cache_read_share"] == pytest.approx(350 / 502)       # Claude: input + cache write + cache read
    assert roles["reviewer_b"]["cache_read_share"] == pytest.approx(0.8)  # Codex input already includes cached


def test_report_shows_usage_per_role(tmp_path):
    from rustflow import report, usage
    text = report.render({"run_id": "r", "status": "approved", "usage_summary": usage.summarize(_role_rows())}, tmp_path)
    section = text.split("## Zużycie per rola", 1)[1]
    assert "| implementer | 2 | $0.750000 | 150 | 350 | 15 | 70% |" in section


def test_summary_keeps_missing_costs_and_estimates_separate():
    from rustflow import usage
    rows = [
        {"provider": "claude", **usage.normalize("claude", {"input_tokens": 10}, 0.125),
         "estimated_cost_usd": None},
        {"provider": "claude", **usage.normalize("claude", None, None), "estimated_cost_usd": 5.0},
        {"provider": "codex", **usage.normalize("codex", [{"input_tokens": 100, "output_tokens": 20}], None)},
    ]
    s = usage.summarize(rows)
    assert s["schema_version"] == 1 and s["attempt_count"] == 3
    assert s["cost_usd"] == {"known": 0.125, "total": None, "missing_attempts": 2}
    assert s["estimated_cost_usd"] == 5.0
    assert s["providers"]["claude"]["measurements"] == {"complete": 0, "partial": 1, "unknown": 1}
    assert s["providers"]["codex"]["tokens"]["total_tokens"] == {
        "known": 120, "total": 120, "missing_attempts": 0}
