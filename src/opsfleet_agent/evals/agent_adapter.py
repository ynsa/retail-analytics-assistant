"""The only place where the runner touches the agent (spec 23, B1: "through AssistantSession").

The runner needs a small interface:

    adapter.start(case_id, user_id, case_dir, fixture, fixture_args) -> Conversation
    conversation.send(text) -> TurnRecord
    conversation.close()

`SessionAdapter` implements it on `opsfleet_agent.session.AssistantSession.send()`, the same call as
the CLI and the UI. It reads the structured fields of `TurnResult` (intent, steps, footer, the delete
confirmation, the report ID and files). The spans add the facts that `TurnResult` does not have
(tool spans, guard spans, error types). When the session API changes, only this file changes.
Tests of the runner use a fake adapter.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

from opsfleet_agent import tracing
from opsfleet_agent.config import PROJECT_ROOT, Paths
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


def _sum_totals(results: list[Any]) -> dict[str, float | int]:
    totals: dict[str, float | int] = {}
    for result in results:
        for key, value in (result.totals or {}).items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _absolute(path: str) -> str:
    """`TurnResult.report_files` holds display paths (relative to the project)."""
    full = Path(path)
    return str(full if full.is_absolute() else PROJECT_ROOT / full)


@dataclass
class SessionConversation:
    session: Any  # opsfleet_agent.session.AssistantSession
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

    def _report_text(self, results: list[Any]) -> str | None:
        """The body of the report that the turn saved (spec 32 `TurnResult.report_id`)."""
        report_id = next((r.report_id for r in reversed(results) if r.report_id), None)
        if self.library is None or report_id is None:
            return None
        try:
            return self.library.body(report_id)
        except CapabilityMissing:
            return None

    def send(self, text: str) -> TurnRecord:
        before_reports, before_events = self._reports(), self._delete_events()
        before_files = self._files()
        identity_before = self.session.user.as_state()
        self.sink.begin()
        start = time.monotonic()
        results = [self.session.send(text)]
        if results[0].replay_input:
            # Spec 33, A2.8: the text was not the confirmation. The delete is cancelled, and the
            # CLI sends the same text again as a new question. The record holds both parts.
            results.append(self.session.send(text))
        latency_ms = (time.monotonic() - start) * 1000
        queries, llm_calls = self.sink.take()
        spans = [s for r in results for s in self.collector.take(r.trace_id)]
        last = results[-1]
        files = {_absolute(f) for r in results for f in r.report_files}
        files |= self._files() - before_files
        return TurnRecord(
            text=text,
            answer="\n\n".join(r.answer for r in results),
            kind="confirmation" if last.confirmation is not None else "answer",
            error_type=last.error_type,
            trace_id=last.trace_id,
            route=last.intent,
            latency_ms=latency_ms,
            totals=_sum_totals(results),
            queries=queries,
            llm_calls=llm_calls,
            spans=spans,
            identity_before=identity_before,
            identity_after=self.session.user.as_state(),
            confirmation=_plain_confirmation(last.confirmation),
            reports_before=before_reports,
            reports_after=self._reports(),
            delete_events=self._delete_events() - before_events,
            report_text=self._report_text(results),
            files=sorted(files),
            footer=last.footer,
            steps=[dict(s) for r in results for s in r.steps],
            report_id=last.report_id,
            delete_outcome=next(
                (r.delete_outcome for r in results if r.delete_outcome), None
            ),
            batch_id=last.batch_id,
            replayed=len(results) > 1,
        )

    def close(self) -> None:
        """End the chat. A waiting delete is cancelled (spec 33, B2.6 #15): no model call."""
        self.session.close()
        self.sink.take()
        self.collector.take(
            None
        )  # the spans of the cancel turn are not part of a record


def _plain_confirmation(confirmation: Any) -> dict[str, Any] | None:
    """The `ConfirmationView` of spec 33 as a plain dict (count, rows, expected_text, text)."""
    if confirmation is None:
        return None
    if isinstance(confirmation, dict):
        return confirmation
    return dataclasses.asdict(confirmation)


class SessionAdapter:
    """Runs each case in a new `AssistantSession` with the recorders, in its own data folder.

    The case folder holds a copy of `app.db` and the report files of the case, so a case never
    changes the real report library or `data/reports/`. The traces and the real daily counter stay
    in the real data folder.
    """

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
        # max_retries 0: retries are invisible to the cap and the throttle. A 429 ends the turn
        # with llm_rate_limited, and the runner waits and runs the case again. No fallback model
        # (spec 36): the run measures one model, and each request goes through the cap.
        models = {
            "max_retries": 0,
            "fallback": None,
            **({"default": model} if model else {}),
        }
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

        real = self.config.settings
        settings = real.model_copy(update={"paths": Paths(data_dir=case_dir.resolve())})
        config = self.config.model_copy(update={"settings": settings})
        # A new tool registry with the case settings: the delete tool moves the files of the case.
        services = dataclasses.replace(self.services, settings=settings, tools=None)
        db_path = copy_app_db(real.data_path("app.db"), settings.data_path("app.db"))
        conn = connect(db_path)
        user = resolve_user(config.users, user_id)
        session = AssistantSession(
            config, user, services, conn=conn, checkpointer=InMemorySaver()
        )
        library = SqliteReportLibrary(conn)
        fixture_ids: dict[str, list[str]] = {}
        self.bq.inject_text = None
        if fixture == "inject_row_text":
            self.bq.inject_text = str((fixture_args or {}).get("text", ""))
        elif fixture in SEED_FIXTURES:
            fixture_ids = library.seed(
                user_id, session.conversation_id, load_seed(fixture)
            )
        return SessionConversation(
            session=session,
            sink=self.sink,
            collector=self.collector,
            library=library,
            user_id=user_id,
            conversation_id=session.conversation_id,
            fixture_ids=fixture_ids,
            reports_dir=settings.reports_dir() / user_id,
        )
