"""The only place where the runner touches the agent (spec 23, B1: "through AssistantSession").

The runner needs a small interface:

    adapter.start(case_id, user_id, case_dir, fixture, fixture_args) -> Conversation
    conversation.send(text) -> TurnRecord
    conversation.close()

`SessionAdapter` implements it on `opsfleet_agent.session.AssistantSession`. When the session API
changes (spec 37: `AssistantSession.start()`, `send()`, `TurnOutcome`), only this file changes.
Tests use a fake adapter.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

from opsfleet_agent import tracing
from opsfleet_agent.evals.fixtures import (
    SEED_FIXTURES,
    CapabilityMissing,
    SqliteReportLibrary,
    copy_app_db,
    load_seed,
)
from opsfleet_agent.evals.recorder import (
    CallCap,
    RecordingBigQueryService,
    RecordingGeminiClient,
    Throttle,
    TurnSink,
)
from opsfleet_agent.evals.records import TurnRecord


class Conversation(Protocol):
    conversation_id: str
    fixture_ids: dict[str, list[str]]

    def send(self, text: str) -> TurnRecord: ...

    def close(self) -> None: ...


class AgentAdapter(Protocol):
    model: str

    def start(
        self,
        case_id: str,
        user_id: str,
        case_dir: Path,
        fixture: str | None = None,
        fixture_args: dict[str, Any] | None = None,
    ) -> Conversation: ...


# ------------------------------------------------------------------------------------------------
# Span collection: the spans of each turn, in memory, in the format of the JSONL trace (spec 00).
# ------------------------------------------------------------------------------------------------


class SpanCollector(SpanProcessor):
    def __init__(self) -> None:
        self._spans: list[dict[str, Any]] = []

    def on_end(self, span: ReadableSpan) -> None:
        attrs = dict(span.attributes or {})
        start = span.start_time or 0
        end = span.end_time or start
        self._spans.append(
            {
                "trace_id": format(span.context.trace_id, "032x"),
                "span_id": format(span.context.span_id, "016x"),
                "name": span.name,
                "kind": attrs.pop("kind", "node"),
                "duration_ms": round((end - start) / 1e6, 1),
                "attributes": attrs,
            }
        )

    def take(self, trace_id: str | None) -> list[dict[str, Any]]:
        mine = [s for s in self._spans if trace_id is None or s["trace_id"] == trace_id]
        self._spans = [s for s in self._spans if s not in mine]
        return mine


_COLLECTOR: SpanCollector | None = None


def span_collector(trace_dir: Path) -> SpanCollector:
    """Attach one collector to the process tracer (spec 00 `init_tracing`)."""
    global _COLLECTOR
    provider = tracing.init_tracing(trace_dir)
    if (
        _COLLECTOR is None
        or getattr(provider, "_opsfleet_eval_collector", None) is not _COLLECTOR
    ):
        _COLLECTOR = SpanCollector()
        provider.add_span_processor(_COLLECTOR)
        provider._opsfleet_eval_collector = _COLLECTOR  # type: ignore[attr-defined]
    return _COLLECTOR


# ------------------------------------------------------------------------------------------------
# The adapter on AssistantSession
# ------------------------------------------------------------------------------------------------


@dataclass
class SessionConversation:
    session: Any
    sink: TurnSink
    collector: SpanCollector
    library: SqliteReportLibrary | None
    user_id: str
    conversation_id: str
    fixture_ids: dict[str, list[str]] = field(default_factory=dict)
    reports_dir: Path | None = None

    def _reports(self) -> dict[str, str | None] | None:
        if self.library is None:
            return None
        try:
            return self.library.state()
        except CapabilityMissing:
            return None

    def _delete_events(self) -> int:
        if self.library is None:
            return 0
        try:
            return self.library.delete_events()
        except CapabilityMissing:
            return 0

    def _files(self) -> set[str]:
        if self.reports_dir is None or not self.reports_dir.exists():
            return set()
        return {str(p) for p in self.reports_dir.rglob("*") if p.is_file()}

    def send(self, text: str) -> TurnRecord:
        before_reports, before_events = self._reports(), self._delete_events()
        before_files = self._files()
        identity_before = self.session.user.as_state()
        self.sink.begin()
        start = time.monotonic()
        if hasattr(self.session, "send"):  # spec 37 API
            outcome = self.session.send(text)
            answer, kind = outcome.text, outcome.kind
            error_type = outcome.error_type.value if outcome.error_type else None
            footer = getattr(outcome, "footer", None)
            trace_id = getattr(footer, "trace_id", None)
            totals = dict(getattr(footer, "__dict__", {}) or {})
            confirmation = getattr(outcome, "confirmation", None)
        else:  # spec 01 API
            result = self.session.ask(text)
            answer, kind, error_type = result.answer, "answer", result.error_type
            trace_id, totals, confirmation = result.trace_id, dict(result.totals), None
        latency_ms = (time.monotonic() - start) * 1000
        queries, llm_calls = self.sink.take()
        spans = self.collector.take(trace_id)
        route = next(
            (
                s["attributes"].get("intent")
                for s in spans
                if s["attributes"].get("intent")
            ),
            None,
        )
        after_reports = self._reports()
        new_files = sorted(self._files() - before_files)
        return TurnRecord(
            text=text,
            answer=answer,
            kind=kind,
            error_type=error_type,
            trace_id=trace_id,
            route=route,
            latency_ms=latency_ms,
            totals=totals,
            queries=queries,
            llm_calls=llm_calls,
            spans=spans,
            identity_before=identity_before,
            identity_after=self.session.user.as_state(),
            confirmation=_plain_confirmation(confirmation),
            reports_before=before_reports,
            reports_after=after_reports,
            delete_events=self._delete_events() - before_events,
            report_text=_new_report_text(self.library, before_reports, after_reports),
            files=new_files,
        )

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


def _plain_confirmation(confirmation: Any) -> dict[str, Any] | None:
    if confirmation is None:
        return None
    if isinstance(confirmation, dict):
        return confirmation
    return dict(getattr(confirmation, "__dict__", {}))


def _new_report_text(
    library: SqliteReportLibrary | None,
    before: dict[str, str | None] | None,
    after: dict[str, str | None] | None,
) -> str | None:
    """The body of a report that the turn created (spec 32 table `reports`)."""
    if library is None or before is None or after is None:
        return None
    new_ids = [i for i in after if i not in before]
    if not new_ids:
        return None
    try:
        return library.body(new_ids[-1])
    except (AttributeError, CapabilityMissing):
        return None


class SessionAdapter:
    """Runs each case in a new `AssistantSession` with the recorders and a copy of `app.db`."""

    def __init__(
        self,
        config: Any,
        cap: CallCap,
        model: str | None = None,
        services: Any | None = None,
        real_ledger: Any | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        from opsfleet_agent.session import build_services

        settings = config.settings
        # max_retries 0: the SDK retries are invisible to the cap and the throttle. A 429 ends
        # the turn with llm_rate_limited, and the runner waits and runs the case again.
        models = {"max_retries": 0, **({"default": model} if model else {})}
        settings = settings.model_copy(
            update={"models": settings.models.model_copy(update=models)}
        )
        config = config.model_copy(update={"settings": settings})
        self.config = config
        self.model = settings.models.default
        self.sink = TurnSink()
        base = services or build_services(config)
        throttle = throttle or Throttle(settings.eval.call_interval_s)
        self.bq = RecordingBigQueryService(base.bq, self.sink, mirror=real_ledger)
        base.llm = RecordingGeminiClient(
            base.llm, self.sink, cap, throttle, mirror=real_ledger, model=self.model
        )
        base.bq = self.bq
        self.services = base
        self.collector = span_collector(settings.data_path("traces"))

    def start(
        self,
        case_id: str,
        user_id: str,
        case_dir: Path,
        fixture: str | None = None,
        fixture_args: dict[str, Any] | None = None,
    ) -> SessionConversation:
        from langgraph.checkpoint.memory import InMemorySaver

        from opsfleet_agent.identity import resolve_user
        from opsfleet_agent.session import AssistantSession
        from opsfleet_agent.store.db import connect

        settings = self.config.settings
        db_path = copy_app_db(settings.data_path("app.db"), case_dir / "app.db")
        conn = connect(db_path)
        user = resolve_user(self.config.users, user_id)
        session = AssistantSession(
            self.config, user, self.services, conn=conn, checkpointer=InMemorySaver()
        )
        conversation_id = getattr(session, "conversation_id", uuid.uuid4().hex)
        library = SqliteReportLibrary(conn)
        fixture_ids: dict[str, list[str]] = {}
        self.bq.inject_text = None
        if fixture == "inject_row_text":
            self.bq.inject_text = str((fixture_args or {}).get("text", ""))
        elif fixture in SEED_FIXTURES:
            fixture_ids = library.seed(user_id, conversation_id, load_seed(fixture))
        return SessionConversation(
            session=session,
            sink=self.sink,
            collector=self.collector,
            library=library,
            user_id=user_id,
            conversation_id=conversation_id,
            fixture_ids=fixture_ids,
            reports_dir=settings.data_path("reports", user_id),
        )
