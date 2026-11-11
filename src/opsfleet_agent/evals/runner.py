"""The run loop (spec 23, B2.1 and B2.9).

One case after the other, never in parallel. Each case runs in a new conversation, with its
fixture, and its turns go through the recorders. The runner scores the case, appends one line to
`results.jsonl`, and stops in a controlled way at the hard cap, at the daily limit, or after
3 rate-limit errors in a row. `--resume` continues a paused run without a second run of the scored
cases.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsfleet_agent.evals.agent_adapter import AgentAdapter
from opsfleet_agent.evals.cases import AccuracyCase, Case
from opsfleet_agent.evals.fixtures import CapabilityMissing
from opsfleet_agent.evals.recorder import CallCap
from opsfleet_agent.evals.records import CaseRecord
from opsfleet_agent.evals.score import ScoringContext, score_case, turn_metrics
from opsfleet_agent.evals.storage import CallStats, RunStore

RATE_LIMIT_PAUSE_AFTER = 3


class Paused(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _RateLimited(Exception):
    pass


@dataclass
class RunState:
    run_id: str
    store: RunStore
    adapter: AgentAdapter
    scoring: ScoringContext
    cap: CallCap
    ledger: Any  # limits.DailyLedger (the real daily counter)
    daily_max_calls: int
    set_name: str
    rate_limit_wait_s: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print
    stats: CallStats | None = None
    rate_limit_streak: int = 0
    scored: set[str] = field(default_factory=set)


def _result_line(
    case: Case,
    set_name: str,
    record: CaseRecord,
    scores: dict[str, Any],
    status: str = "scored",
) -> dict[str, Any]:
    passed = bool(scores) and all(s.passed for s in scores.values())
    line: dict[str, Any] = {
        "case_id": case.id,
        "set": set_name,
        "user": case.user,
        "kind": case.kind,
        "status": status,
        "passed": passed,
        "scorers": {k: v.to_json() for k, v in scores.items()},
        "turns": [turn_metrics(t) for t in record.turns],
        "error": record.error,
        "reviewed_by": case.reviewed_by,
    }
    if isinstance(case, AccuracyCase):
        line.update(
            capability=case.capability,
            difficulty=case.difficulty,
            query_types=case.query_types,
            has_reference=case.reference_sql is not None,
        )
    else:
        line.update(capability=case.category, difficulty=None, owasp=case.owasp)
    return line


def _check_limits(state: RunState) -> None:
    if state.cap.reached:
        raise Paused(
            "paused_cap",
            f"The run reached its cap of {state.cap.cap} calls. {len(state.scored)} cases are "
            f"scored. Continue with: uv run opsfleet-eval run --resume {state.run_id}",
        )
    if state.ledger.today()["llm_calls"] >= state.daily_max_calls:
        raise Paused(
            "paused_daily_limit",
            "The daily limit is reached. Continue tomorrow with: "
            f"uv run opsfleet-eval run --resume {state.run_id}",
        )


def _run_turns(state: RunState, case: Case, case_dir: Path) -> CaseRecord:
    record = CaseRecord(case_id=case.id, user=case.user)
    fixture = getattr(case, "fixture", None)
    args = getattr(case, "fixture_args", None)
    try:
        conversation = state.adapter.start(case.id, case.user, case_dir, fixture, args)
    except CapabilityMissing as exc:
        record.error = f"capability missing: {exc}"
        return record
    record.conversation_id = conversation.conversation_id
    record.fixture_ids = conversation.fixture_ids
    try:
        for text in case.turn_texts:
            turn = conversation.send(text)
            record.turns.append(turn)
            if turn.error_type == "llm_rate_limited":
                raise _RateLimited()
            if turn.error_type == "limit_exceeded":
                _check_limits(
                    state
                )  # a cap or a daily limit pauses; a turn limit is a result
    finally:
        conversation.close()
    return record


def _judge_error(scores: dict[str, Any], error_type: str) -> bool:
    return any(
        s.reason == "judge_error" and s.details.get("error_type") == error_type
        for s in scores.values()
    )


def _rate_limit_wait(state: RunState, case: Case) -> None:
    state.rate_limit_streak += 1
    if state.rate_limit_streak >= RATE_LIMIT_PAUSE_AFTER:
        raise Paused(
            "paused_rate_limit",
            "Gemini refuses calls for rate. The run is paused. Continue later with: "
            f"uv run opsfleet-eval run --resume {state.run_id}",
        )
    state.log(
        f"  {case.id}: rate limit, wait {state.rate_limit_wait_s:g} s and try again"
    )
    state.sleep(state.rate_limit_wait_s)


def run_one(state: RunState, case: Case) -> dict[str, Any]:
    """Run and score one case. Raises Paused when the run must stop."""
    case_dir = state.store.run_dir(state.run_id) / "cases" / case.id
    # A record without a result comes from a run that paused in the judge step: score it only.
    record = state.store.record(state.run_id, case.id)
    while record is None:
        _check_limits(state)
        try:
            record = _run_turns(state, case, case_dir)
        except _RateLimited:
            _rate_limit_wait(state, case)
            continue
        state.rate_limit_streak = 0
        state.store.save_record(state.run_id, record)
    for _attempt in range(RATE_LIMIT_PAUSE_AFTER):
        scores = score_case(case, record, state.scoring)
        if _judge_error(scores, "limit_exceeded"):
            _check_limits(state)  # the cap or the daily limit stopped the judge: pause
        if not _judge_error(scores, "llm_rate_limited"):
            break
        _rate_limit_wait(state, case)
    else:  # pragma: no cover - _rate_limit_wait raises Paused first
        raise Paused("paused_rate_limit", "Gemini refuses calls for rate.")
    line = _result_line(case, state.set_name, record, scores)
    state.store.append_result(state.run_id, line)
    state.scored.add(case.id)
    if state.stats is not None and record.turns:
        state.stats.add(
            case.id,
            llm_calls=sum(t.llm_call_count for t in record.turns),
            bq_bytes=sum(t.bytes_billed for t in record.turns),
        )
    return line


def run_cases(state: RunState, cases: list[Case]) -> tuple[str, str]:
    """Run the cases that are not scored yet. Returns (status, message)."""
    state.scored |= {r["case_id"] for r in state.store.results(state.run_id)}
    for case in cases:
        if case.id in state.scored:
            continue
        start = time.monotonic()
        before = state.cap.used
        try:
            line = run_one(state, case)
        except Paused as pause:
            return pause.status, pause.message
        seconds = time.monotonic() - start
        mark = "pass" if line["passed"] else "FAIL"
        state.log(
            f"{case.id:8} {mark}  {seconds:6.1f} s  {state.cap.used - before:2d} calls"
            f"  ({state.cap.used}/{state.cap.cap})"
        )
    return "done", f"All {len(cases)} cases are scored."


def rescore(state: RunState, cases: list[Case]) -> int:
    """Score the stored records again (after a snapshot refresh). No Gemini call: judge results
    are reused from the last result line of each case."""
    old = {r["case_id"]: r for r in state.store.results(state.run_id)}
    count = 0
    for case in cases:
        record = state.store.record(state.run_id, case.id)
        if record is None or case.id not in old:
            continue
        reuse = {k: v for k, v in old[case.id]["scorers"].items() if "judge" in k}
        scores = score_case(case, record, state.scoring, reuse=reuse)
        line = _result_line(case, state.set_name, record, scores)
        line["rescored_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        state.store.append_result(state.run_id, line)
        count += 1
    return count
