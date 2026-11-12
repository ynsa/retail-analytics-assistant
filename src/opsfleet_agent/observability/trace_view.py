"""The steps of one turn from the JSONL traces (spec 35, A2.1 and A2.2).

`load_trace(trace_dir, trace_id, user_id)` reads the masked spans of the trace and builds a tree.
A short trace ID (the 8 characters of an error message) is enough. The trace must belong to the
user: its `turn` span has the user's ID. Else the result is None ("Trace not found.").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_ID_CHARS = 8
SQL_SHOWN_CHARS = (
    400  # the tree shows the start of the SQL; the JSONL file has 2,000 characters
)
_ONLY_ABOVE_1 = frozenset({"attempt"})  # attempt 1 is the normal case
_HEX = re.compile(r"^[0-9a-f]+$")
# The attributes that the tree shows for each span kind, in this order.
_SHOWN: dict[str, tuple[str, ...]] = {
    "turn": ("intent", "status", "error_type", "turn_no"),
    "node": ("attempt", "step_index", "tokens_estimated"),
    "llm": ("model", "fallback_used", "tokens_in", "tokens_out"),
    "tool": ("tool", "side_effect", "ok"),
    "sql": ("job_id", "bytes_billed", "rows", "truncated", "bytes_estimated"),
    "guard": ("outcome", "reason_code", "rewritten", "hits_total", "diagnosis"),
    "limit": ("limit_name",),
    "confirm": ("outcome", "reason", "count"),
}


@dataclass
class TraceNode:
    name: str
    kind: str
    status: str
    start: str
    duration_ms: float
    attributes: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    children: list[TraceNode] = field(default_factory=list)

    def label(self) -> str:
        """`node.route · 812 ms · intent=analysis`, with `ERROR <type>` for a failed step."""
        parts = [self.name, _duration(self.duration_ms)]
        for key in _SHOWN.get(self.kind, ()):
            value = self.attributes.get(key)
            if value in (None, "", False) or (key in _ONLY_ABOVE_1 and value == 1):
                continue
            parts.append(f"{key}={_value(key, value)}")
        error = self.attributes.get("error_type")
        if self.status == "error" or (error and self.kind != "turn"):
            parts.append(f"ERROR {error or 'unknown'}")
        return " · ".join(parts)

    def details(self) -> list[str]:
        """Extra lines under the label: the masked SQL, the events."""
        lines = []
        sql = self.attributes.get("sql_masked")
        if (
            sql and self.name == "guard.check"
        ):  # the SQL of the model, before the scope rewrite
            lines.append(f"SQL: {_cut(str(sql), SQL_SHOWN_CHARS)}")
        for event in self.events:
            attrs = event.get("attributes") or {}
            text = ", ".join(f"{k}={v}" for k, v in attrs.items())
            lines.append(f"event {event.get('name')}" + (f": {text}" if text else ""))
        return lines


@dataclass(frozen=True)
class TraceTree:
    trace_id: str
    user_id: str
    root: TraceNode
    span_count: int


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _duration(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _value(key: str, value: Any) -> str:
    if key.startswith("bytes") and isinstance(value, int | float):
        return f"{value / 1e6:.1f} MB"
    return str(value)


def normalize_trace_id(text: str) -> str | None:
    value = text.strip().lower().removeprefix("trace").strip(" :")
    if len(value) < MIN_ID_CHARS or len(value) > 32 or not _HEX.match(value):
        return None
    return value


def _read_spans(trace_dir: Path, prefix: str) -> list[dict[str, Any]]:
    """The spans of the newest trace with this prefix. The files are read newest first. A trace
    can cross midnight, so the file before the first match is also read."""
    needle = f'"trace_id": "{prefix}'
    found: list[dict[str, Any]] = []
    files = (
        sorted(trace_dir.glob("*.jsonl"), reverse=True) if trace_dir.is_dir() else []
    )
    extra = None
    for path in files:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if needle in line:
                    found.append(json.loads(line))
        if found:
            if extra is not None:
                break
            extra = path
    if not found:
        return []
    newest = max(found, key=lambda s: s.get("start") or "")["trace_id"]
    return [s for s in found if s["trace_id"] == newest]


def _node(record: dict[str, Any]) -> TraceNode:
    return TraceNode(
        name=str(record.get("name")),
        kind=str(record.get("kind")),
        status=str(record.get("status")),
        start=str(record.get("start") or ""),
        duration_ms=float(record.get("duration_ms") or 0),
        attributes=dict(record.get("attributes") or {}),
        events=list(record.get("events") or []),
    )


def load_trace(trace_dir: Path, trace_id: str, user_id: str) -> TraceTree | None:
    prefix = normalize_trace_id(trace_id)
    if prefix is None:
        return None
    records = _read_spans(trace_dir, prefix)
    turn = next((r for r in records if r.get("kind") == "turn"), None)
    if turn is None or (turn.get("attributes") or {}).get("user_id") != user_id:
        return None  # another user's trace gives the same answer as an unknown trace
    nodes = {r["span_id"]: _node(r) for r in records}
    root = nodes[turn["span_id"]]
    for record in sorted(records, key=lambda r: r.get("start") or ""):
        parent = record.get("parent_span_id")
        if record is turn or parent not in nodes:
            continue
        nodes[parent].children.append(nodes[record["span_id"]])
    return TraceTree(
        trace_id=turn["trace_id"], user_id=user_id, root=root, span_count=len(records)
    )


def trace_text(tree: TraceTree) -> str:
    """The tree as indented plain text."""
    lines = [f"Trace {tree.trace_id[:8]} ({tree.span_count} steps)"]

    def walk(node: TraceNode, depth: int) -> None:
        pad = "  " * depth
        lines.append(f"{pad}{node.label()}")
        lines.extend(f"{pad}    {d}" for d in node.details())
        for child in node.children:
            walk(child, depth + 1)

    walk(tree.root, 1)
    return "\n".join(lines)
