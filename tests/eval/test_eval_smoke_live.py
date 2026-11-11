"""Live smoke test of the evaluation (spec 23 B4). Marker `live`: it calls Gemini and BigQuery.

Run by hand: `uv run pytest -m live tests/eval/test_eval_smoke_live.py`. The smoke set plans at most
10 Gemini calls (no judge), so it needs no "yes". BigQuery: about 0.2 GB.
"""

from __future__ import annotations

import pytest

from opsfleet_agent.evals.cases import load_set


@pytest.mark.live
def test_smoke_run_finishes_and_writes_a_report():
    from opsfleet_agent.evals.cli import _env
    from opsfleet_agent.evals.orchestrate import execute_run, plan_only

    assert sum(c.expected_calls for c in load_set("smoke").cases) <= 10
    env = _env()
    plan = plan_only(env, "smoke", None, None, judge=False)
    assert plan.total_calls <= 10 and not plan.needs_yes
    outcome = execute_run(env, set_name="smoke")
    assert outcome.status in ("done", "data_changed"), outcome.message
    assert outcome.report_path is not None and outcome.report_path.exists()
