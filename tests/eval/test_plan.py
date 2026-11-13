"""The plan and the approval rule (spec 23 B2.2, A4). No Gemini call."""

from __future__ import annotations

from opsfleet_agent.evals.cases import load_set
from opsfleet_agent.evals.plan import approve, make_plan, render_plan
from opsfleet_agent.evals.storage import CallStats


def plan_for(config, ids: list[str], judge: bool = True, today: int = 0, stats=None):
    cases = load_set("accuracy").select(ids)
    return make_plan(
        set_name="accuracy",
        cases=cases,
        skipped={},
        settings=config.settings,
        agent_model="gemini-3.5-flash-lite",
        judge_enabled=judge,
        ledger_today={"llm_calls": today},
        stats=stats,
    )


def test_sum_cap_and_judge_calls(config):
    plan = plan_for(config, ["PP-01", "PP-10", "SC-01"])
    assert plan.agent_calls == 3 + 5 + 2
    assert plan.judge_calls == 2  # PP-10 and SC-01 have a judge scorer
    assert plan.cap == 15  # 12 * 1.2 = 14.4, rounded up
    assert plan.judge_model == "gemini-3.5-flash-lite"
    no_judge = plan_for(config, ["PP-01", "PP-10", "SC-01"], judge=False)
    assert no_judge.judge_calls == 0 and no_judge.judge_model is None


def test_cost_estimate_uses_paid_prices(config):
    plan = plan_for(config, ["PP-01"])
    ev = config.settings.eval
    expected = (
        3 * ev.tokens_in_per_call * 0.30 + 3 * ev.tokens_out_per_call * 2.50
    ) / 1e6
    assert plan.cost_usd == round(expected, 2)
    text = render_plan(plan)
    assert "Gemini calls: 3 planned" in text and "Free tier: $0" in text


def test_call_stats_replace_the_estimate(config, tmp_path):
    stats = CallStats(tmp_path / "s.json")
    for calls in (5, 6, 7):
        stats.add("PP-01", llm_calls=calls, bq_bytes=1)
    plan = plan_for(config, ["PP-01"], stats=stats)
    assert plan.agent_calls == 6 and plan.bq_agent_bytes == 1


def test_daily_check(config):
    limit = config.settings.limits.per_day.max_llm_calls
    assert plan_for(config, ["PP-01"], today=limit - 3).fits_today
    late = plan_for(config, ["PP-01"], today=limit - 2)
    assert not late.fits_today and "continue tomorrow" in render_plan(late)


def test_yes_rule(config):
    small = plan_for(config, ["PP-01", "TM-01"])  # 6 calls
    assert approve(small, False, None).startswith("auto")
    big = plan_for(config, ["PP-01", "TM-01", "CB-01", "CB-02"])  # 12 calls
    assert big.needs_yes
    assert approve(big, False, lambda _: "no") is None
    assert approve(big, False, lambda _: "yes") == "typed yes"
    assert approve(big, True, None).startswith("--yes")
    assert approve(big, False, None) is None
