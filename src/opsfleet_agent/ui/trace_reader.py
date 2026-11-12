"""Read the spans of a turn from `data/traces/*.jsonl` for the steps pane (spec 40, A2.2).

Spec 35 builds `/trace` with its own reader. This small reader uses only the record format of
spec 00 (`trace_id`, `parent_span_id`, `name`, `kind`, `start`, `duration_ms`, `status`,
`attributes`). It finds the steps from the `node.*` spans and their child spans, so it keeps
working when spec 35 renames a span (for example `bq.query` to `sql.query`) or drops an
attribute. A missing attribute only removes a detail from the text.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_FILES = 31  # the trace files of the last 31 days (spec 35 keeps 30 days)

Span = dict[str, Any]

INTENT_TEXT = {
    "analysis": "data question",
    "schema": "question about the data",
    "clarify": "clarifying question",
    "report": "report",
    "delete": "delete reports",
    "chitchat": "chat",
    "off_topic": "off topic",
    "unsafe": "unsafe request",
}
GUARD_TEXT = {"pass": "pass", "policy_block": "block", "parse_error": "parse error"}
STEP_TITLES = {
    "route": "Route",
    "write_sql": "Write SQL",
    "guard": "SQL guard",
    "run_query": "BigQuery",
    "check_empty": "Check the empty result",
    "write_answer": "Write the answer",
    "schema_answer": "Answer about the data",
    "prepared_reply": "Prepared reply",
    "give_up": "Stop without an answer",
    "explain_block": "Explain the block",
    "plan_report": "Plan the report",
    "write_report": "Write the report",
    "build_report": "Build the report",
    "save_report": "Save the report",
    "render": "Write the file",
    "parse_delete": "Read the delete request",
    "find_reports": "Find the reports",
    "confirm_delete": "Confirmation",
    "apply_delete": "Delete",
    "undo_delete": "Undo",
    "summarize": "Summary of older turns",
}
# Shown only when they called Gemini or BigQuery, or failed.
OPTIONAL_STEPS = frozenset({"check_empty"})


@dataclass(frozen=True)
class Step:
    title: str
    meta: tuple[str, ...] = ()
    note: str | None = None
    status: str = "ok"
    sql: str | None = None


@dataclass(frozen=True)
class TurnTrace:
    trace_id: str
    user_id: str
    intent: str | None
    error_type: str | None
    reason_code: str | None
    steps: tuple[Step, ...]
    totals: dict[str, float]
    spans: tuple[Span, ...] = field(default=(), repr=False)


def valid_trace_id(trace_id: str) -> bool:
    return bool(TRACE_ID.match(trace_id or ""))


def read_spans(trace_dir: Path, trace_ids: Iterable[str]) -> dict[str, list[Span]]:
    """The spans of each trace ID, in start order. One pass over the newest trace files."""
    wanted = {t for t in trace_ids if valid_trace_id(t)}
    found: dict[str, list[Span]] = defaultdict(list)
    if not wanted or not trace_dir.is_dir():
        return {}
    for path in sorted(trace_dir.glob("*.jsonl"), reverse=True)[:MAX_FILES]:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not any(t in line for t in wanted):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a half-written line
                if record.get("trace_id") in wanted:
                    found[record["trace_id"]].append(record)
    for spans in found.values():
        spans.sort(key=lambda s: s.get("start") or "")
    return dict(found)


def load_turns(
    trace_dir: Path, trace_ids: Iterable[str], user_id: str, scope: str = ""
) -> dict[str, TurnTrace]:
    """The turns of these traces that belong to `user_id`. A trace of another user is missing
    from the result, the same as an unknown trace (spec 35: "Trace not found.")."""
    out: dict[str, TurnTrace] = {}
    for trace_id, spans in read_spans(trace_dir, trace_ids).items():
        turn = next((s for s in spans if s.get("kind") == "turn"), None)
        if turn is None or _attrs(turn).get("user_id") != user_id:
            continue
        out[trace_id] = build_turn(trace_id, user_id, turn, spans, scope)
    return out


def load_turn(
    trace_dir: Path, trace_id: str, user_id: str, scope: str = ""
) -> TurnTrace | None:
    return load_turns(trace_dir, [trace_id], user_id, scope).get(trace_id)


def build_turn(
    trace_id: str, user_id: str, turn: Span, spans: list[Span], scope: str
) -> TurnTrace:
    attrs = _attrs(turn)
    children: dict[str | None, list[Span]] = defaultdict(list)
    for s in spans:
        children[s.get("parent_span_id")].append(s)
    steps = tuple(
        step
        for node in spans
        if node.get("kind") == "node"
        and (step := _step(node, children[node.get("span_id")], scope)) is not None
    )
    route = next((s for s in spans if s.get("name") == "node.route"), None)
    guard = [
        s for s in spans if s.get("kind") == "guard" and _attrs(s).get("reason_code")
    ]
    error_type = attrs.get("error_type") or next(
        (_attrs(s).get("error_type") for s in spans if _attrs(s).get("error_type")),
        None,
    )
    return TurnTrace(
        trace_id=trace_id,
        user_id=user_id,
        intent=attrs.get("intent") or (_attrs(route).get("intent") if route else None),
        error_type=error_type,
        reason_code=_attrs(guard[-1]).get("reason_code") if guard else None,
        steps=steps,
        totals=totals_of(turn, spans),
        spans=tuple(spans),
    )


def totals_of(turn: Span, spans: list[Span]) -> dict[str, float]:
    """The cost of the turn: the attributes of the `turn` span, or the sums of the child spans
    when spec 35 drops these attributes."""
    attrs = _attrs(turn)
    llm = [_attrs(s) for s in spans if s.get("kind") == "llm"]
    queries = [_attrs(s) for s in spans if _is_query(s)]

    def pick(name: str, fallback: float) -> float:
        value = attrs.get(name)
        return float(value) if isinstance(value, int | float) else float(fallback)

    return {
        "llm_calls": pick("llm_calls", len(llm)),
        "tokens_in": pick("tokens_in", sum(a.get("tokens_in") or 0 for a in llm)),
        "tokens_out": pick("tokens_out", sum(a.get("tokens_out") or 0 for a in llm)),
        "bq_queries": pick("bq_queries", len(queries)),
        "bytes_billed": pick(
            "bytes_billed", sum(a.get("bytes_billed") or 0 for a in queries)
        ),
        "seconds": pick("seconds", round((turn.get("duration_ms") or 0) / 1000, 2)),
    }


# --- steps ---------------------------------------------------------------------------------------
def _step(node: Span, kids: list[Span], scope: str) -> Step | None:
    name = str(node.get("name") or "").removeprefix("node.")
    attrs = _attrs(node)
    llm = [k for k in kids if k.get("kind") == "llm"]
    queries = [k for k in kids if _is_query(k)]
    failed = node.get("status") == "error"
    if name not in STEP_TITLES and not (llm or queries or failed):
        return None
    if name in OPTIONAL_STEPS and not (llm or queries or failed):
        return None
    title = STEP_TITLES.get(name, name.replace("_", " ").capitalize())
    meta: list[str] = []
    note: str | None = None
    if name == "route" and attrs.get("intent"):
        title = f"Route: {INTENT_TEXT.get(attrs['intent'], attrs['intent'])}"
    elif name == "write_sql" and int(attrs.get("attempt") or 0) > 1:
        title = f"Write SQL (attempt {attrs['attempt']})"
    elif name == "guard":
        title, note = _guard_text(kids, scope)
    elif name == "plan_report" and attrs.get("planned_queries"):
        title = f"Plan the report: {attrs['planned_queries']} queries"
    elif name == "build_report" and attrs.get("charts") is not None:
        title = f"Build the report: {attrs['charts']} charts"
    elif name == "save_report":
        saved = _child_attr(kids, "report_id")
        title = f"Save the report: {saved}" if saved else title
    elif name in ("confirm_delete", "apply_delete"):
        outcome = attrs.get("confirm_outcome") or _child_attr(kids, "outcome")
        title = f"{title}: {outcome}" if outcome else title
    if llm:
        fallback = any(_attrs(k).get("fallback_used") for k in llm)
        meta.append("Gemini (fallback model)" if fallback else "Gemini")
    if queries:
        count = len(queries)
        billed = sum(_attrs(q).get("bytes_billed") or 0 for q in queries)
        title = f"{title}: {count} {'query' if count == 1 else 'queries'}"
        meta.append(megabytes(billed))
    seconds = (node.get("duration_ms") or 0) / 1000
    if seconds >= 0.05 or llm or queries:
        meta.append(f"{seconds:.1f} s")
    if failed:
        note = attrs.get("error_type") or "error"
    sql = next(
        (
            str(_attrs(q).get("sql_masked"))
            for q in queries
            if _attrs(q).get("sql_masked")
        ),
        None,
    )
    return Step(
        title=title,
        meta=tuple(meta),
        note=note,
        status="error" if failed else "ok",
        sql=sql,
    )


def _guard_text(kids: list[Span], scope: str) -> tuple[str, str | None]:
    check = next((k for k in kids if k.get("kind") == "guard"), None)
    attrs = _attrs(check) if check else {}
    outcome = str(attrs.get("outcome") or "")
    title = f"SQL guard: {GUARD_TEXT.get(outcome, outcome)}" if outcome else "SQL guard"
    reason = attrs.get("reason_code")
    if reason:
        return title, f"reason: {str(reason).replace('_', ' ')}"
    return title, (f"scope: {scope}" if scope and outcome == "pass" else None)


def megabytes(value: float) -> str:
    """The same unit as the CLI footer: 1 MB = 1,000,000 bytes."""
    return f"{value / 1e6:.1f} MB"


def _is_query(span: Span) -> bool:
    """A BigQuery job: a `sql` span with billed bytes (not a dry run)."""
    return (
        span.get("kind") == "sql"
        and "dry_run" not in str(span.get("name"))
        and "bytes_billed" in _attrs(span)
    )


def _child_attr(kids: list[Span], name: str) -> Any:
    return next((_attrs(k)[name] for k in kids if _attrs(k).get(name)), None)


def _attrs(span: Span | None) -> dict[str, Any]:
    return dict((span or {}).get("attributes") or {})
