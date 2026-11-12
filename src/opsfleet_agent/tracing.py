"""Tracing with OpenTelemetry. Spans go to `data/traces/<UTC date>.jsonl` (spec 00, B3).

A trace never contains result rows or PII. Spec 35 adds the masking of SQL text.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode

from opsfleet_agent.errors import AgentError, ErrorType

_TRACER_NAME = "opsfleet_agent"
_provider: TracerProvider | None = None


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return (
        datetime.fromtimestamp(ns / 1e9, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class JsonlSpanExporter(SpanExporter):
    """Writes one JSON line for each finished span."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            for span in spans:
                start = span.start_time or 0
                end = span.end_time or start
                day = datetime.fromtimestamp(start / 1e9, UTC).strftime("%Y-%m-%d")
                attrs = dict(span.attributes or {})
                record = {
                    "trace_id": format(span.context.trace_id, "032x"),
                    "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x")
                    if span.parent
                    else None,
                    "name": span.name,
                    "kind": attrs.pop("kind", "node"),
                    "start": _iso(start),
                    "duration_ms": round((end - start) / 1e6, 1),
                    "status": "error"
                    if span.status.status_code is StatusCode.ERROR
                    else "ok",
                    "attributes": attrs,
                }
                with (self._dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - nothing to release
        return None


def init_tracing(trace_dir: Path) -> TracerProvider:
    """Set up the tracer once for the process. Later calls return the same provider."""
    global _provider
    if _provider is None:
        provider = TracerProvider(
            resource=Resource.create({"service.name": "opsfleet-agent"})
        )
        provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_dir)))
        _provider = provider
    return _provider


def tracer() -> trace.Tracer:
    if _provider is None:
        return trace.get_tracer(_TRACER_NAME)  # no-op tracer before init_tracing()
    return _provider.get_tracer(_TRACER_NAME)


@contextmanager
def span(name: str, kind: str, **attributes: Any) -> Generator[Span]:
    """A span of one step. An exception marks the span as an error with its error type."""
    clean = {k: v for k, v in attributes.items() if v is not None}
    with tracer().start_as_current_span(
        name,
        attributes={"kind": kind, **clean},
        record_exception=False,
        set_status_on_exception=False,
    ) as sp:
        try:
            yield sp
        except GraphBubbleUp:
            sp.set_attribute(
                "interrupted", True
            )  # a paused graph (spec 33), not an error
            raise
        except AgentError as exc:
            sp.set_attribute("error_type", exc.error_type.value)
            if exc.detail:  # for debugging; spec 35 adds the masking of this text
                sp.set_attribute("error_detail", exc.detail[:300])
            sp.set_status(Status(StatusCode.ERROR, exc.error_type.value))
            raise
        except Exception as exc:
            sp.set_attribute("error_type", ErrorType.INTERNAL_ERROR.value)
            sp.set_attribute(
                "error_detail", type(exc).__name__
            )  # the type only: no values
            sp.set_status(Status(StatusCode.ERROR, ErrorType.INTERNAL_ERROR.value))
            raise


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
