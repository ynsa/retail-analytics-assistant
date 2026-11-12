"""What the runner records for each turn of a case (spec 23, B2.3).

The agent adapter fills these records. The scorers and the safety checks read only these records,
so a change of the agent API changes only the adapter.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Errors of an outside service that a second run can avoid (spec 23, B2.9): a 5xx answer, a
# timeout, a connection error, or a circuit breaker that such errors opened. A rate limit (429) has
# its own wait. A wrong key or a safety block of the model is not in this group.
EXTERNAL_ERROR_TYPES = frozenset({"llm_error", "llm_timeout"})
_EXTERNAL_DETAIL = re.compile(
    r"\b(?:50[0-4]|UNAVAILABLE|INTERNAL|DEADLINE_EXCEEDED)\b|circuit open|timeout|timed out",
    re.IGNORECASE,
)


def is_external_detail(detail: str) -> bool:
    return bool(_EXTERNAL_DETAIL.search(detail))


@dataclass
class QueryRecord:
    """One SQL that the agent sent to BigQuery, with its result (at most 200 rows)."""

    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    bytes_billed: int = 0
    error_type: str | None = None
    purpose: str = ""

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]


@dataclass
class LlmRecord:
    """One Gemini call: the model input and output, the tokens and the latency."""

    purpose: str
    model: str
    system: str
    prompt: str
    output: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    error_type: str | None = None


@dataclass
class TurnRecord:
    """One user message and everything that happened in its turn."""

    text: str
    answer: str = ""
    kind: str = "answer"  # answer | confirmation (a delete waits for the typed text)
    error_type: str | None = None
    trace_id: str | None = None
    route: str | None = None  # the route intent (spec 31), when the agent exposes it
    latency_ms: float = 0.0
    totals: dict[str, float | int] = field(default_factory=dict)
    queries: list[QueryRecord] = field(default_factory=list)
    llm_calls: list[LlmRecord] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    identity_before: dict[str, Any] | None = None
    identity_after: dict[str, Any] | None = None
    confirmation: dict[str, Any] | None = None  # the pending confirmation (spec 33)
    reports_before: dict[str, str | None] | None = None  # report_id -> deleted_at
    reports_after: dict[str, str | None] | None = None
    delete_events: int = 0  # new `delete_confirmed` audit events in this turn
    report_text: str | None = (
        None  # the body of a report that the turn created (spec 32)
    )
    files: list[str] = field(default_factory=list)  # report files that the turn created
    # Structured fields of the session's TurnResult (specs 31 to 33).
    footer: str = ""  # the answer footer: period, brands, definitions
    steps: list[dict[str, Any]] = field(default_factory=list)  # the query steps
    report_id: str | None = None  # the report that the turn saved
    delete_outcome: str | None = (
        None  # deleted, wrong_number, not_exact, other_text, ...
    )
    batch_id: str | None = None  # the delete batch of the turn
    replayed: bool = False  # the text cancelled a delete and ran again as a question

    @property
    def llm_call_count(self) -> int:
        return int(self.totals.get("llm_calls", len(self.llm_calls)))

    @property
    def tokens_in(self) -> int:
        return int(
            self.totals.get("tokens_in", sum(c.tokens_in for c in self.llm_calls))
        )

    @property
    def tokens_out(self) -> int:
        return int(
            self.totals.get("tokens_out", sum(c.tokens_out for c in self.llm_calls))
        )

    @property
    def bytes_billed(self) -> int:
        return int(
            self.totals.get("bytes_billed", sum(q.bytes_billed for q in self.queries))
        )

    def error_types(self) -> list[str]:
        """All error types of the turn: the final one and the ones in the spans."""
        found = [s["attributes"].get("error_type") for s in self.spans]
        found.append(self.error_type)
        return sorted({e for e in found if e})

    def self_corrections(self) -> int:
        """Queries or guard checks that failed and were followed by another attempt."""
        failed = [q for q in self.queries if q.error_type]
        return len(failed)

    def external_error(self) -> str | None:
        """A short text of the outside error that ended the turn, or None."""
        if self.error_type not in EXTERNAL_ERROR_TYPES:
            return None
        for s in self.spans:
            attrs = s.get("attributes") or {}
            detail = str(attrs.get("error_detail") or "")
            if attrs.get("error_temporary") or is_external_detail(detail):
                return f"{self.error_type}: {detail or 'temporary'}"[:160]
        return "llm_timeout" if self.error_type == "llm_timeout" else None

    def tool_calls(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.spans:
            if s.get("kind") == "tool":
                counts[s["name"]] = counts.get(s["name"], 0) + 1
        return counts

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TurnRecord:
        data = dict(data)
        data["queries"] = [QueryRecord(**q) for q in data.get("queries", [])]
        data["llm_calls"] = [LlmRecord(**c) for c in data.get("llm_calls", [])]
        return cls(**data)


@dataclass
class CaseRecord:
    """All turns of one case, plus the fixture data that the checks need."""

    case_id: str
    user: str
    conversation_id: str | None = None
    turns: list[TurnRecord] = field(default_factory=list)
    fixture_ids: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None  # an exception in the runner or the adapter
    # The outside errors of earlier runs of this case that the runner ran again (spec 23, B2.9).
    external_retries: list[str] = field(default_factory=list)

    def external_error(self) -> str | None:
        """The first outside error that ended a turn of this case, or None."""
        return next((e for t in self.turns if (e := t.external_error())), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "user": self.user,
            "conversation_id": self.conversation_id,
            "turns": [t.to_json() for t in self.turns],
            "fixture_ids": self.fixture_ids,
            "error": self.error,
            "external_retries": self.external_retries,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CaseRecord:
        return cls(
            case_id=data["case_id"],
            user=data["user"],
            conversation_id=data.get("conversation_id"),
            turns=[TurnRecord.from_json(t) for t in data.get("turns", [])],
            fixture_ids=data.get("fixture_ids", {}),
            error=data.get("error"),
            external_retries=list(data.get("external_retries", [])),
        )
