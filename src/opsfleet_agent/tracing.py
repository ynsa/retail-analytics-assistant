"""Tracing with OpenTelemetry. Spans go to `data/traces/<UTC date>.jsonl` (spec 00, B3).

A trace never contains result rows or PII. The exporter calls the `Redactor` of spec 35 before
each write: it keeps only the allowed attributes, masks SQL text and free text, and cuts long
values. Two in-memory processors get the raw spans: the turn figures (spec 35) and the progress
of a running turn (spec 37). No span leaves the machine.
"""

from __future__ import annotations

import json
import logging
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
    """Writes one masked JSON line for each finished span."""

    def __init__(self, directory: Path, max_chars: int = 2000) -> None:
        from opsfleet_agent.observability.redact import (
            Redactor,  # import cycle via the guard
        )

        self._dir = directory
        self._lock = threading.Lock()
        self._redactor = Redactor(max_chars)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            for span in spans:
                start = span.start_time or 0
                end = span.end_time or start
                day = datetime.fromtimestamp(start / 1e9, UTC).strftime("%Y-%m-%d")
                attrs = dict(span.attributes or {})
                kind = str(attrs.pop("kind", "node"))
                record = {
                    "trace_id": format(span.context.trace_id, "032x"),
                    "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x")
                    if span.parent
                    else None,
                    "name": span.name,
                    "kind": kind,
                    "start": _iso(start),
                    "duration_ms": round((end - start) / 1e6, 1),
                    "status": "error"
                    if span.status.status_code is StatusCode.ERROR
                    else "ok",
                    "attributes": self._redactor.attributes(kind, attrs),
                }
                if span.events:
                    record["events"] = [
                        self._redactor.event(e.name, dict(e.attributes or {}))
                        for e in span.events
                    ]
                with (self._dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - nothing to release
        return None


def init_tracing(
    trace_dir: Path,
    max_chars: int = 2000,
    otlp_endpoint: str | None = None,
) -> TracerProvider:
    """Set up the tracer once for the process. Later calls return the same provider.

    Spec 35, B2.6: the only exporter is the local JSONL file. The setting
    `tracing.otlp_endpoint` stays `null`. This prototype has no OTLP exporter: a value only
    writes a warning to the log, and no span leaves the machine."""
    global _provider
    if _provider is None:
        from opsfleet_agent.observability.progress import PROGRESS
        from opsfleet_agent.observability.turn_metrics import TURN_SPANS

        provider = TracerProvider(
            resource=Resource.create({"service.name": "opsfleet-agent"})
        )
        provider.add_span_processor(
            SimpleSpanProcessor(JsonlSpanExporter(trace_dir, max_chars))
        )
        provider.add_span_processor(TURN_SPANS)
        provider.add_span_processor(PROGRESS)
        if otlp_endpoint:
            logging.getLogger("opsfleet_agent").warning(
                "tracing.otlp_endpoint is set, but this prototype has no OTLP exporter. "
                "The spans stay in the local JSONL files."
            )
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
            if getattr(
                exc, "temporary", False
            ):  # a retry can help (spec 36 ServiceFailure)
                sp.set_attribute("error_temporary", True)
            if exc.detail:  # for debugging; the exporter masks it (spec 35)
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
