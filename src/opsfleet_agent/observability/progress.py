"""Progress of a running turn (spec 37, B2.2), from the start of each node span.

The node wrapper of spec 31 opens a `node.<name>` span when a node starts. This processor sends a
`ProgressEvent` to the listener of the trace. The subgraph nodes (`write_sql`, `guard`,
`run_query`, ...) also have spans, so the caller sees each step, also inside `data_loop`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

NODE_PREFIX = "node."
# The nodes that start a new query step: the next `write_sql` is attempt 1 again.
_STEP_END = frozenset({"mask_result", "write_answer", "collect_query", "next_query"})


@dataclass(frozen=True)
class ProgressEvent:
    node: str  # the node name, for example `run_query`
    attempt: int = 1  # the SQL attempt of the current step (write_sql only)


Listener = Callable[[ProgressEvent], None]


class _Tracker:
    def __init__(self, listener: Listener) -> None:
        self.listener = listener
        self.attempt = 0


class ProgressProcessor(SpanProcessor):
    def __init__(self) -> None:
        self._listeners: dict[str, _Tracker] = {}
        self._lock = threading.Lock()

    def listen(self, trace_id: str, listener: Listener) -> None:
        with self._lock:
            self._listeners[trace_id] = _Tracker(listener)

    def stop(self, trace_id: str | None) -> None:
        with self._lock:
            self._listeners.pop(trace_id or "", None)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if not span.name.startswith(NODE_PREFIX):
            return
        trace_id = format(span.context.trace_id, "032x")
        with self._lock:
            tracker = self._listeners.get(trace_id)
        if tracker is None:
            return
        node = span.name[len(NODE_PREFIX) :]
        if node == "write_sql":
            tracker.attempt += 1
        elif node in _STEP_END:
            tracker.attempt = 0
        try:
            tracker.listener(ProgressEvent(node, max(tracker.attempt, 1)))
        except Exception:  # noqa: BLE001, S110 - a display problem never stops the turn
            pass

    def on_end(self, span: ReadableSpan) -> None:
        return None


PROGRESS = ProgressProcessor()
