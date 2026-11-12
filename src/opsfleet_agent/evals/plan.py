"""The plan of a run: calls, tokens, cost, bytes, time and the hard cap (spec 23, B2.2).

The plan makes no Gemini call. A run with more planned calls than `eval.ask_above_calls` starts
only after a typed "yes", or with `--yes` after the owner approved the plan in the conversation.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from opsfleet_agent.config import EvalPrices, Settings
from opsfleet_agent.evals.cases import Case
from opsfleet_agent.evals.storage import CallStats

BQ_SECONDS_PER_CASE = 2.0


@dataclass
class RunPlan:
    set_name: str
    agent_model: str
    judge_model: str | None
    case_ids: list[str]
    skipped: dict[str, str]
    agent_calls: int
    judge_calls: int
    cap: int
    daily_calls_left: int
    daily_bytes_left: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    price_note: str
    bq_snapshot_bytes: int
    bq_agent_bytes: int
    seconds: float
    ask_above: int
    call_interval_s: float = 4.0
    unreviewed: list[str] = field(
        default_factory=list
    )  # reviewed_by is empty or claude-draft
    per_case_calls: dict[str, int] = field(default_factory=dict)

    @property
    def total_calls(self) -> int:
        return self.agent_calls + self.judge_calls

    @property
    def fits_today(self) -> bool:
        return self.total_calls <= self.daily_calls_left

    @property
    def needs_yes(self) -> bool:
        return self.total_calls > self.ask_above

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "total_calls": self.total_calls}


def _price(settings: Settings, model: str) -> tuple[EvalPrices, str]:
    prices = settings.eval.prices
    if model in prices:
        return prices[model], f"paid-tier price of {model}"
    fallback = next(iter(prices.values()), EvalPrices())
    return (
        fallback,
        f"no price for {model} in settings: the estimate uses the first listed price",
    )


def case_calls(case: Case, stats: CallStats | None) -> int:
    median = stats.median(case.id, "llm_calls") if stats else None
    return math.ceil(median) if median is not None else case.expected_calls


def make_plan(
    *,
    set_name: str,
    cases: list[Case],
    skipped: dict[str, str],
    settings: Settings,
    agent_model: str,
    judge_enabled: bool,
    ledger_today: dict[str, int],
    stats: CallStats | None = None,
    snapshot_bytes: int = 0,
) -> RunPlan:
    ev = settings.eval
    per_case = {c.id: case_calls(c, stats) for c in cases}
    agent_calls = sum(per_case.values())
    judge_calls = sum(c.judged_turns() for c in cases) if judge_enabled else 0
    total = agent_calls + judge_calls
    tokens_in = (
        agent_calls * ev.tokens_in_per_call + judge_calls * ev.judge_tokens_in_per_call
    )
    tokens_out = (
        agent_calls * ev.tokens_out_per_call
        + judge_calls * ev.judge_tokens_out_per_call
    )
    agent_price, note = _price(settings, agent_model)
    judge_price, _ = _price(settings, ev.judge_model)
    cost = (
        agent_calls * ev.tokens_in_per_call * agent_price.input_per_m
        + agent_calls * ev.tokens_out_per_call * agent_price.output_per_m
        + judge_calls * ev.judge_tokens_in_per_call * judge_price.input_per_m
        + judge_calls * ev.judge_tokens_out_per_call * judge_price.output_per_m
    ) / 1e6
    agent_bytes = 0
    for c in cases:
        median = stats.median(c.id, "bq_bytes") if stats else None
        default = ev.default_case_bytes * len(c.turn_texts)  # an estimate for each turn
        agent_bytes += int(median if median is not None else default)
    limits = settings.limits.per_day
    return RunPlan(
        set_name=set_name,
        agent_model=agent_model,
        judge_model=ev.judge_model if judge_enabled else None,
        case_ids=[c.id for c in cases],
        skipped=skipped,
        agent_calls=agent_calls,
        judge_calls=judge_calls,
        cap=math.ceil(total * (1 + ev.cap_margin)),
        daily_calls_left=max(
            0, limits.max_llm_calls - ledger_today.get("llm_calls", 0)
        ),
        daily_bytes_left=max(
            0, limits.max_bq_bytes_billed - ledger_today.get("bq_bytes_billed", 0)
        ),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(cost, 2),
        price_note=note,
        bq_snapshot_bytes=snapshot_bytes,
        bq_agent_bytes=agent_bytes,
        seconds=total * ev.call_interval_s + len(cases) * BQ_SECONDS_PER_CASE,
        ask_above=ev.ask_above_calls,
        call_interval_s=ev.call_interval_s,
        unreviewed=[
            c.id for c in cases if not c.reviewed_by or c.reviewed_by == "claude-draft"
        ],
        per_case_calls=per_case,
    )


def render_plan(plan: RunPlan) -> str:
    skipped = len(plan.skipped)
    judge = (
        f", judge {plan.judge_calls} on {plan.judge_model}"
        if plan.judge_model
        else ", no judge"
    )
    fits = (
        " This run fits."
        if plan.fits_today
        else f" This run needs {plan.total_calls} calls. Select fewer cases with --cases,"
        " or continue tomorrow with --resume."
    )
    n = len(plan.case_ids)
    n_cases = f"{n} case" if n == 1 else f"{n} cases"
    total_gb = (plan.bq_snapshot_bytes + plan.bq_agent_bytes) / 1e9
    lines = [
        (
            f"Set: {plan.set_name} ({n_cases}, {skipped} skipped)"
            f"      Agent model: {plan.agent_model}"
        ),
        (
            f"Gemini calls: {plan.total_calls} planned (agent {plan.agent_calls}{judge})."
            f" Hard cap for this run: {plan.cap}."
        ),
        f"Daily budget left: {plan.daily_calls_left} calls.{fits}",
        (
            f"Tokens: about {plan.tokens_in / 1e6:.2f}M in, {plan.tokens_out / 1e6:.2f}M out."
            f" Price at the paid tier: about ${plan.cost_usd:.2f}. Free tier: $0."
        ),
        (
            f"BigQuery: {total_gb:.2f} GB planned"
            f" (snapshot refresh {plan.bq_snapshot_bytes / 1e9:.2f} GB,"
            f" agent {plan.bq_agent_bytes / 1e9:.2f} GB)."
        ),
        (
            f"Time: about {math.ceil(plan.seconds / 60)} minutes"
            f" (one call every {plan.call_interval_s:g} s)."
        ),
    ]
    if plan.skipped:
        lines.append(
            "Skipped: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(plan.skipped.items()))
        )
    if plan.unreviewed:
        lines.append(
            f"Not confirmed by a person yet (reviewed_by: claude-draft): {len(plan.unreviewed)} cases."
        )
    return "\n".join(lines)


def approve(
    plan: RunPlan, yes_flag: bool, ask: Callable[[str], str] | None
) -> str | None:
    """How the run was approved, or None. Small runs need no approval (spec 23, A2.2)."""
    if not plan.needs_yes:
        return f"auto: {plan.total_calls} calls <= {plan.ask_above}"
    if yes_flag:
        return "--yes (plan approved in the conversation)"
    if ask is None:
        return None
    answer = ask(
        f'This run plans {plan.total_calls} Gemini calls. Type "yes" to start: '
    )
    return "typed yes" if answer.strip().lower() == "yes" else None
