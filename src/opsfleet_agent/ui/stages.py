"""The stage of a running answer, for the progress line of the page (spec 40, A2.3).

`StageWatcher` is an OpenTelemetry span processor. The session already writes a span for each
graph node, so the UI needs no change in the agent: when a `node.*` span starts in the turn of a
watched conversation, the watcher calls the callback with a short text, for example
"Running query 2 of 4".
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

STAGE_TEXT = {
    "load_context": "Reading the question",
    "route": "Reading the question",
    "prepared_reply": "Writing the answer",
    "schema_answer": "Writing the answer",
    "write_sql": "Writing SQL",
    "guard": "Checking the SQL",
    "check_empty": "Checking the empty result",
    "mask_result": "Checking the result",
    "write_answer": "Writing the answer",
    "scan_output": "Checking the answer",
    "plan_report": "Planning the report",
    "write_report": "Writing the report",
    "build_report": "Drawing the charts",
    "save_report": "Saving the report",
    "render": "Writing the file",
    "parse_delete": "Reading the delete request",
    "find_reports": "Finding the reports",
    "apply_delete": "Deleting the reports",
    "undo_delete": "Restoring the reports",
    "summarize": "Updating the conversation memory",
}


@dataclass(frozen=True)
class Stage:
    text: str
    limit: str | None = None  # "Limit for a report: 150 s", set when the limit changes


StageCallback = Callable[[Stage], None]


@dataclass
class _Turn:
    callback: StageCallback
    planned_queries: int = 0


class StageWatcher(SpanProcessor):
    """Sends the stages of the turns of the watched conversations to their callbacks."""

    def __init__(self, report_limit_s: float) -> None:
        self.report_limit_s = report_limit_s
        self._lock = threading.Lock()
        self._conversations: dict[str, StageCallback] = {}
        self._turns: dict[int, _Turn] = {}

    @contextmanager
    def watch(self, conversation_id: str, callback: StageCallback) -> Iterator[None]:
        with self._lock:
            self._conversations[conversation_id] = callback
        try:
            yield
        finally:
            with self._lock:
                self._conversations.pop(conversation_id, None)
                for key in [
                    k for k, t in self._turns.items() if t.callback is callback
                ]:
                    del self._turns[key]

    # --- SpanProcessor -----------------------------------------------------------------------------
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        attrs = dict(span.attributes or {})
        trace_id = span.context.trace_id
        with self._lock:
            if span.name == "turn":
                callback = self._conversations.get(str(attrs.get("conversation_id")))
                if callback is not None:
                    self._turns[trace_id] = _Turn(callback)
                return
            turn = self._turns.get(trace_id)
        if turn is not None and attrs.get("kind") == "node":
            text = stage_text(span.name, attrs, turn.planned_queries)
            if text:
                turn.callback(Stage(text))

    def on_end(self, span: ReadableSpan) -> None:
        attrs = dict(span.attributes or {})
        trace_id = span.context.trace_id if span.context else 0
        with self._lock:
            if span.name == "turn":
                self._turns.pop(trace_id, None)
                return
            turn = self._turns.get(trace_id)
        if turn is None:
            return
        if span.name == "node.plan_report":
            turn.planned_queries = int(attrs.get("planned_queries") or 0)
        elif span.name == "node.route" and attrs.get("intent") == "report":
            turn.callback(
                Stage("Planning the report", limit_text("report", self.report_limit_s))
            )

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def stage_text(
    name: str, attrs: dict[str, Any], planned_queries: int = 0
) -> str | None:
    """The text for a node that starts. None keeps the text of the earlier stage."""
    node = name.removeprefix("node.")
    if node == "run_query":
        number = int(attrs.get("step_index") or 0) + 1
        if planned_queries:
            return f"Running query {number} of {planned_queries}"
        return f"Running query {number}"
    return STAGE_TEXT.get(node)


def limit_text(kind: str, seconds: float) -> str:
    return f"Limit for a {kind}: {seconds:g} s"
