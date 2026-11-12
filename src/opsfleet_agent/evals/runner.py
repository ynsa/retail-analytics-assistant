"""The run loop (spec 23, B2.1 and B2.9).

One case after the other, never in parallel. Each case runs in a new conversation, with its
fixture, and its turns go through the recorders. The runner scores the case, appends one line to
`results.jsonl`, and stops in a controlled way at the hard cap, at the daily limit, or after
3 rate-limit errors in a row. `--resume` continues a paused run without a second run of the scored
cases.

An outside error (a Gemini 503, a timeout, an open circuit breaker) in a turn or in a judge call
gets one more run after a wait, inside the hard cap. If the error comes again, the result line has
the status `external_error`: the report shows the case, and the quality rates leave it out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsfleet_agent.evals.agent_adapter import AgentAdapter
from opsfleet_agent.evals.cases import AccuracyCase, Case
from opsfleet_agent.evals.fixtures import CapabilityMissing
from opsfleet_agent.evals.metrics import EXTERNAL_STATUS
from opsfleet_agent.evals.recorder import CallCap
from opsfleet_agent.evals.records import CaseRecord
from opsfleet_agent.evals.score import ScoringContext, score_case, turn_metrics
from opsfleet_agent.evals.storage import CallStats, RunStore

RATE_LIMIT_PAUSE_AFTER = 3
EXTERNAL_RETRIES = 1  # one more run of a case or a judge call after an outside error


class Paused(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _RateLimited(Exception):
    def __init__(self, record: CaseRecord) -> None:
        super().__init__("rate limited")
        self.record = record  # the discarded run, for the call count


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
    external_wait_s: float = 60.0  # the open time of the circuit breaker (spec 36)
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
                raise _RateLimited(record)
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


def _judge_external(scores: dict[str, Any]) -> str | None:
    """The outside error of a judge call (503, timeout), or None."""
    for s in scores.values():
        if s.reason != "judge_error":
            continue
        if s.details.get("temporary") or s.details.get("error_type") == "llm_timeout":
            return f"judge: {s.details.get('error_type')}"
    return None


def _only_external(scores: dict[str, Any]) -> bool:
    """True when every failed scorer failed only because of an outside error of the judge."""
    failed = [s for s in scores.values() if not s.passed]
    return bool(failed) and all(
        s.reason == "judge_error"
        and (s.details.get("temporary") or s.details.get("error_type") == "llm_timeout")
        for s in failed
    )


def _logged_judge_results(state: RunState, case_id: str) -> dict[str, Any]:
    """Judge results of this case from `judge.jsonl` (a run that paused in the judge step)."""
    out: dict[str, Any] = {}
    for item in state.store.judge_items(state.run_id):
        if item["case_id"] == case_id and not item.get("error"):
            out[item["key"]] = {
                "pass": item["passed"],
                "reason": None if item["passed"] else "judge_failed",
                "details": {"criteria": item["criteria"], "rubric": item["rubric"]},
            }
    return out


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


def _external_wait(state: RunState, case: Case, problem: str) -> None:
    state.log(
        f"  {case.id}: outside error ({problem[:80]}), wait {state.external_wait_s:g} s "
        "and try again"
    )
    state.sleep(state.external_wait_s)


def _record_of(state: RunState, case: Case, case_dir: Path) -> CaseRecord:
    """Run the turns of the case. After an outside error, run the case again one time.

    The record keeps the totals of the discarded runs, so the report counts all Gemini calls.
    """
    retries: list[str] = []
    discarded = CaseRecord(case_id=case.id, user=case.user)
    while True:
        _check_limits(state)
        try:
            record = _run_turns(state, case, case_dir)
        except _RateLimited as limited:
            discarded.add_discarded(limited.record)
            _rate_limit_wait(state, case)
            continue
        state.rate_limit_streak = 0
        problem = record.external_error()
        if problem and len(retries) < EXTERNAL_RETRIES:
            retries.append(problem)
            discarded.add_discarded(record)
            _external_wait(state, case, problem)
            continue
        record.external_retries = retries
        record.discarded = discarded.discarded
        state.store.save_record(state.run_id, record)
        return record


def run_one(state: RunState, case: Case) -> dict[str, Any]:
    """Run and score one case. Raises Paused when the run must stop."""
    case_dir = state.store.run_dir(state.run_id) / "cases" / case.id
    # A record without a result comes from a run that paused in the judge step: score it only.
    record = state.store.record(state.run_id, case.id)
    if record is None:
        record = _record_of(state, case, case_dir)
    external = record.external_error()
    # The judge does not judge the text of an outside error: it says nothing about the agent.
    scoring = (
        dataclasses.replace(state.scoring, judge=None) if external else state.scoring
    )
    reuse = _logged_judge_results(state, case.id)
    judge_retries = 0
    for _attempt in range(RATE_LIMIT_PAUSE_AFTER + EXTERNAL_RETRIES):
        scores = score_case(case, record, scoring, reuse=reuse)
        reuse.update(  # a judge result that worked is not asked again
            {
                k: v.to_json()
                for k, v in scores.items()
                if "judge" in k and v.reason != "judge_error"
            }
        )
        if _judge_error(scores, "limit_exceeded"):
            _check_limits(state)  # the cap or the daily limit stopped the judge: pause
        if _judge_error(scores, "llm_rate_limited"):
            _rate_limit_wait(state, case)
            continue
        problem = _judge_external(scores)
        if problem and judge_retries < EXTERNAL_RETRIES:
            judge_retries += 1
            _check_limits(state)
            _external_wait(state, case, problem)
            continue
        break
    else:  # pragma: no cover - _rate_limit_wait raises Paused first
        raise Paused("paused_rate_limit", "Gemini refuses calls for rate.")
    if not external and _only_external(scores):
        external = _judge_external(scores)
    line = _result_line(
        case,
        state.set_name,
        record,
        scores,
        status=EXTERNAL_STATUS if external else "scored",
    )
    if external:
        line["external_error"] = external
    if record.external_retries:
        line["external_retries"] = record.external_retries
    if record.discarded:
        line["discarded"] = record.discarded
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
        mark = (
            "EXT "
            if line["status"] == EXTERNAL_STATUS
            else ("pass" if line["passed"] else "FAIL")
        )
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
        external = record.external_error() or (
            _judge_external(scores) if _only_external(scores) else None
        )
        line = _result_line(
            case,
            state.set_name,
            record,
            scores,
            status=EXTERNAL_STATUS if external else "scored",
        )
        if external:
            line["external_error"] = external
        line["rescored_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        state.store.append_result(state.run_id, line)
        count += 1
    return count
