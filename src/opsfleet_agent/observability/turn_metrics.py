"""The figures of each turn (spec 35, B2.1 and B2.4).

`TurnMetricsProcessor` is an OpenTelemetry span processor. It keeps the finished spans of the
last traces in memory. At the end of a turn, the session takes the spans of its trace, and
`turn_metrics()` computes the metric columns of `conversation_turns`. The counts of model calls,
tokens, queries and bytes come from the `TurnBudget` totals of the turn, because they also count
the work of fakes and of code paths without a span. The other figures come from the spans.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import StatusCode

from opsfleet_agent.store.db import retry_locked

MAX_TRACES = 64  # traces kept in memory; a turn takes its spans at its end

# The error types of these span kinds are counted. A `node` or `turn` span repeats the error of
# its child, so it counts only when no child span has the error (B2.4 `errors`).
_LEAF_KINDS = frozenset({"llm", "sql", "guard", "tool", "limit", "confirm"})
_DELETE_CANCEL = frozenset(
    {
        "wrong_number",
        "not_exact",
        "other_text",
        "expired",
        "abandoned",
        "changed",
        "internal_error",
    }
)
_REFUSAL_INTENTS = frozenset({"off_topic", "unsafe"})


@dataclass(frozen=True)
class SpanRecord:
    """A finished span with its raw attributes (in memory only, never written)."""

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    status: str
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    events: tuple[tuple[str, dict[str, Any]], ...] = ()


def span_record(span: ReadableSpan) -> SpanRecord:
    attrs = dict(span.attributes or {})
    start = span.start_time or 0
    end = span.end_time or start
    return SpanRecord(
        trace_id=format(span.context.trace_id, "032x"),
        span_id=format(span.context.span_id, "016x"),
        parent_id=format(span.parent.span_id, "016x") if span.parent else None,
        name=span.name,
        kind=str(attrs.pop("kind", "node")),
        status="error" if span.status.status_code is StatusCode.ERROR else "ok",
        duration_ms=round((end - start) / 1e6, 1),
        attributes=attrs,
        events=tuple((e.name, dict(e.attributes or {})) for e in span.events),
    )


class TurnMetricsProcessor(SpanProcessor):
    """Collects the spans of each trace until the session takes them."""

    def __init__(self, max_traces: int = MAX_TRACES) -> None:
        self._traces: OrderedDict[str, list[SpanRecord]] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_traces

    def on_end(self, span: ReadableSpan) -> None:
        record = span_record(span)
        with self._lock:
            self._traces.setdefault(record.trace_id, []).append(record)
            self._traces.move_to_end(record.trace_id)
            while len(self._traces) > self._max:
                self._traces.popitem(last=False)

    def take(self, trace_id: str | None) -> list[SpanRecord]:
        if not trace_id:
            return []
        with self._lock:
            return self._traces.pop(trace_id, [])


TURN_SPANS = TurnMetricsProcessor()


# --- the figures of one turn ---------------------------------------------------------------------
@dataclass(frozen=True)
class TurnFacts:
    """What the session knows about the turn without the spans (from `TurnResult`)."""

    answer: str
    intent: str | None
    status: str  # ok, error, cancelled
    error_type: str | None
    totals: Mapping[str, float | int]
    steps: tuple[Mapping[str, Any], ...] = ()
    confirmation: bool = False
    delete_outcome: str | None = None
    report_id: str | None = None


def turn_kind(facts: TurnFacts, spans: Iterable[SpanRecord] = ()) -> str:
    names = {s.name for s in spans}
    if facts.intent == "delete" or facts.confirmation or facts.delete_outcome:
        return "delete"
    if facts.intent == "report" or facts.report_id or "node.report_flow" in names:
        return "report"
    if facts.intent == "analysis":
        return "question"
    return "other"


def turn_outcome(facts: TurnFacts, empty_kind: str | None) -> str:
    """The `outcome` column: the status values of B2.4."""
    if facts.confirmation:
        return "confirmation_shown"
    if facts.delete_outcome == "deleted":
        return "confirmed"
    if facts.delete_outcome in _DELETE_CANCEL or facts.status == "cancelled":
        return "cancelled"
    error = facts.error_type
    if error == "limit_exceeded":
        return "limit"
    if error == "guard_policy_block":
        return "blocked"
    if error == "confirmation_rejected":
        return "cancelled"
    if error:
        return "failed"
    if facts.intent in _REFUSAL_INTENTS:
        return "refused"
    if empty_kind == "true_zero":
        return "true_zero"
    return "answered"


def confirmation_outcome(facts: TurnFacts) -> str | None:
    if facts.confirmation:
        return "shown"
    if facts.delete_outcome == "deleted":
        return "confirmed"
    if facts.delete_outcome in _DELETE_CANCEL:
        return facts.delete_outcome
    return None


def error_counts(spans: list[SpanRecord], final_error: str | None) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for s in spans:
        error = s.attributes.get("error_type")
        if error and s.kind in _LEAF_KINDS:
            counts[str(error)] += 1
    if not counts:
        for (
            s
        ) in spans:  # an error that only a node span has, for example a bug in a node
            error = s.attributes.get("error_type")
            if error and s.kind == "node":
                counts[str(error)] += 1
                break
    if final_error and final_error not in counts:
        counts[final_error] += 1
    return dict(counts)


# Nodes that run in every turn. Their count tells nothing, so `tools_used` leaves them out.
_EVERY_TURN = frozenset({"load_context", "scan_output", "finish"})


def tools_used(spans: Iterable[SpanRecord]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for s in spans:
        if s.kind in ("node", "tool") and "." in s.name:
            name = s.name.split(".", 1)[1]
            if name not in _EVERY_TURN:
                counts[name] += 1
    return dict(sorted(counts.items()))


def turn_metrics(
    facts: TurnFacts, spans: list[SpanRecord], user_id: str | None = None
) -> dict[str, Any]:
    """The metric columns of `conversation_turns` for one turn."""
    totals = facts.totals
    turn_span = next((s for s in spans if s.kind == "turn"), None)
    llm_spans = [s for s in spans if s.kind == "llm"]
    sql_spans = [s for s in spans if s.kind == "sql"]
    guard = [s for s in spans if s.name == "guard.check"]
    blocks = [
        s.attributes.get("reason_code")
        for s in guard
        if s.attributes.get("outcome") == "policy_block"
    ]
    empty = [
        s.attributes.get("diagnosis") for s in spans if s.name == "guard.empty_check"
    ]
    empty_kind = str(empty[-1]) if empty and empty[-1] else None
    events = [name for s in spans for name, _ in s.events]
    models = [
        str(s.attributes["model"]) for s in llm_spans if s.attributes.get("model")
    ]
    versions = [
        str(s.attributes["prompt_version"])
        for s in llm_spans
        if s.attributes.get("prompt_version")
    ]
    attempts = [int(step.get("attempts") or 0) for step in facts.steps]
    seconds = float(totals.get("seconds") or 0)
    query_bytes = [
        int(s.attributes.get("bytes_billed") or 0)
        for s in sql_spans
        if "bytes_billed" in s.attributes
    ]
    fallback_calls = int(totals.get("fallback_calls") or 0)
    return {
        "user_id": user_id,
        "kind": turn_kind(facts, spans),
        "outcome": turn_outcome(facts, empty_kind),
        "duration_ms": turn_span.duration_ms if turn_span else round(seconds * 1000, 1),
        "answer_words": len(facts.answer.split()),
        "answer_chars": len(facts.answer),
        "llm_calls": int(totals.get("llm_calls") or 0),
        "llm_attempts": int(totals.get("llm_attempts") or 0),
        "tokens_in": int(totals.get("tokens_in") or 0),
        "tokens_out": int(totals.get("tokens_out") or 0),
        "bq_queries": int(totals.get("bq_queries") or 0),
        "bq_bytes_billed": int(totals.get("bytes_billed") or 0),
        "bq_query_bytes": query_bytes,
        "attempts_total": sum(attempts),
        "retried": int(any(a > 1 for a in attempts)),
        "errors": error_counts(spans, facts.error_type),
        "guard_block_reason": str(blocks[-1]) if blocks and blocks[-1] else None,
        "empty_result_kind": empty_kind,
        "confirmation_outcome": confirmation_outcome(facts),
        "tools_used": tools_used(spans),
        "model": models[-1] if models else None,
        "fallback_used": int(
            fallback_calls > 0
            or any(s.attributes.get("fallback_used") for s in llm_spans)
        ),
        "fallback_calls": fallback_calls,
        "retries": events.count("retry"),
        "circuit_open": events.count("circuit_open"),
        "masker_hits": sum(
            1
            for s in spans
            if s.name == "guard.mask" and s.attributes.get("masker_hit")
        ),
        "scanner_hits": sum(
            int(s.attributes.get("hits_total") or 0)
            for s in spans
            if s.name == "guard.scan"
        ),
        "prompt_version": versions[0] if versions else None,
    }


# --- the write ------------------------------------------------------------------------------------
_ADDED = (
    "duration_ms",
    "llm_calls",
    "llm_attempts",
    "tokens_in",
    "tokens_out",
    "bq_queries",
    "bq_bytes_billed",
    "attempts_total",
    "fallback_calls",
    "retries",
    "circuit_open",
    "masker_hits",
    "scanner_hits",
)
_JSON_COUNTS = ("errors", "tools_used")


def merge_metrics(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """The second part of a turn (the typed answer to a delete confirmation) adds its work to the
    first part. The outcome and the answer come from the second part."""
    merged = dict(new)
    for key in _ADDED:
        merged[key] = (old.get(key) or 0) + (new.get(key) or 0)
    for key in _JSON_COUNTS:
        counts: Counter[str] = Counter(json.loads(old.get(key) or "{}"))
        counts.update(new.get(key) or {})
        merged[key] = dict(sorted(counts.items()))
    merged["bq_query_bytes"] = list(
        json.loads(old.get("bq_query_bytes") or "[]")
    ) + list(new.get("bq_query_bytes") or [])
    merged["retried"] = int(bool(old.get("retried")) or bool(new.get("retried")))
    merged["fallback_used"] = int(
        bool(old.get("fallback_used")) or bool(new.get("fallback_used"))
    )
    for key in ("model", "prompt_version", "guard_block_reason", "empty_result_kind"):
        merged[key] = new.get(key) or old.get(key)
    if old.get("kind") == "delete":
        merged["kind"] = "delete"
    return merged


def write_turn_record(
    conn: sqlite3.Connection,
    conversation_id: str,
    turn_no: int,
    metrics: Mapping[str, Any],
    resumed: bool,
) -> None:
    """Write the metric columns of the turn row. The row exists already (spec 34 writes it)."""
    columns = [k for k in metrics if k != "user_id" or metrics[k]]

    def write() -> None:
        with conn:
            values = dict(metrics)
            if resumed:
                old = conn.execute(
                    "SELECT * FROM conversation_turns WHERE conversation_id = ? AND turn_no = ?",
                    (conversation_id, turn_no),
                ).fetchone()
                if old is not None and old["outcome"] is not None:
                    values = merge_metrics(dict(old), metrics)
            row = {
                k: json.dumps(values[k])
                if isinstance(values[k], dict | list)
                else values[k]
                for k in columns
            }
            assignments = ", ".join(f"{k} = ?" for k in row)
            conn.execute(
                f"UPDATE conversation_turns SET {assignments} "
                "WHERE conversation_id = ? AND turn_no = ?",
                (*row.values(), conversation_id, turn_no),
            )

    retry_locked(write)
