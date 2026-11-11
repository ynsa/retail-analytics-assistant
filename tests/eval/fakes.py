"""Fakes for the runner tests: an agent, a judge model and a BigQuery runner. No network."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsfleet_agent.config import PROJECT_ROOT
from opsfleet_agent.evals.cases import load_set
from opsfleet_agent.evals.recorder import CallCap, RunCapReached
from opsfleet_agent.evals.records import QueryRecord, TurnRecord
from opsfleet_agent.evals.scorers.judge import JudgeVerdict
from opsfleet_agent.llm import LlmResponse

BASELINE = json.loads(
    (PROJECT_ROOT / "evals" / "baseline" / "snapshots.json").read_text()
)
ORDERS_COLUMNS = (
    "order_id, user_id, status, gender, created_at, returned_at, shipped_at, delivered_at, "
    "num_of_item"
)


def perfect_turn(case_id: str, text: str) -> TurnRecord:
    """A correct answer: the reference result as the agent query, and its figures in the text."""
    snap = BASELINE.get(f"{case_id}/main")
    if snap is None:
        answer = {
            "SC-02": f"The orders table has these columns: {ORDERS_COLUMNS}.",
        }.get(case_id, "I can only help with the analysis of the company's sales data.")
        return TurnRecord(text=text, answer=answer, totals={"llm_calls": 1})
    figures = ", ".join(
        f"{k}: {v:,.2f}" if isinstance(v, float) else f"{k}: {v}"
        for row in snap["rows"]
        for k, v in zip(snap["columns"], row, strict=False)
    )
    query = QueryRecord(
        sql="SELECT ...",
        columns=snap["columns"],
        rows=snap["rows"],
        bytes_billed=20_971_520,
    )
    return TurnRecord(
        text=text,
        answer=f"Result: {figures}.",
        queries=[query],
        totals={
            "llm_calls": 3,
            "tokens_in": 3000,
            "tokens_out": 200,
            "bytes_billed": 20_971_520,
        },
        latency_ms=1200.0,
        trace_id="t" * 32,
    )


Script = Callable[[str, str, int], TurnRecord]


@dataclass
class FakeConversation:
    case_id: str
    script: Script
    cap: CallCap
    ledger: Any
    calls_per_turn: int
    daily_max: int = 10**9
    conversation_id: str = "conv-1"
    fixture_ids: dict[str, list[str]] = field(default_factory=dict)
    sent: list[str] = field(default_factory=list)
    on_send: Callable[[], None] | None = None

    def send(self, text: str) -> TurnRecord:
        self.sent.append(text)
        if self.on_send:
            self.on_send()
        used = 0
        try:
            for _ in range(self.calls_per_turn):
                if self.ledger.today()["llm_calls"] >= self.daily_max:
                    raise RunCapReached(
                        0
                    )  # the real TurnBudget raises LimitExceeded here
                self.cap.check()
                self.cap.add(1)
                self.ledger.add(llm_calls=1)
                used += 1
        except RunCapReached:
            return TurnRecord(
                text=text,
                answer="limit",
                error_type="limit_exceeded",
                totals={"llm_calls": used},
            )
        turn = self.script(self.case_id, text, len(self.sent))
        turn.totals["llm_calls"] = used
        return turn

    def close(self) -> None:
        return None


@dataclass
class FakeAdapter:
    cap: CallCap
    ledger: Any
    model: str = "fake-model"
    script: Script = field(default=lambda case_id, text, n: perfect_turn(case_id, text))
    calls_per_turn: int = 2
    daily_max: int = 10**9
    started: list[str] = field(default_factory=list)
    on_send: Callable[[], None] | None = None

    def start(
        self,
        case_id: str,
        user_id: str,
        case_dir: Path,
        fixture=None,
        fixture_args=None,
    ):
        self.started.append(case_id)
        return FakeConversation(
            case_id,
            self.script,
            self.cap,
            self.ledger,
            self.calls_per_turn,
            daily_max=self.daily_max,
            on_send=self.on_send,
        )


class FakeJudgeLLM:
    """Passes every listed criterion, unless `fail` names it."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls = 0

    def generate(
        self, budget: Any, *, system: str, prompt: str, schema=None, **kw: Any
    ):
        budget.charge_llm()
        self.calls += 1
        ids = re.findall(r"^- ([A-Z0-9-]+): ", prompt, flags=re.MULTILINE)
        verdict = {
            "criteria": [
                {"id": i, "passed": i not in self.fail, "critique": "fake"} for i in ids
            ]
        }
        return LlmResponse(
            "", JudgeVerdict.model_validate(verdict), 900, 80, "fake-judge"
        )


class FakeReferenceRunner:
    """Returns the baseline rows for each reference SQL. `changed` moves the table times."""

    def __init__(self) -> None:
        self.changed = False
        self.runs = 0
        self._rows: dict[str, tuple[list[str], list[list[Any]]]] = {}
        for set_name in ("accuracy", "owner_session", "safety_long"):
            for case in load_set(set_name).cases:
                for ref_name, ref in case.all_references().items():
                    snap = BASELINE[f"{case.id}/{ref_name}"]
                    self._rows[ref.sql] = (snap["columns"], snap["rows"])
        self.times = next(iter(BASELINE.values()))["table_modified"]

    def dry_run(self, sql: str, params: dict[str, Any]) -> int:
        return 5_000_000

    def run(self, sql: str, params: dict[str, Any]):
        self.runs += 1
        columns, rows = self._rows[sql]
        return columns, rows, 20_971_520

    def table_times(self) -> dict[str, str]:
        if not self.changed:
            return dict(self.times)
        return {k: "2027-01-01T00:00:00+00:00" for k in self.times}
