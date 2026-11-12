"""The UI backend: live sessions, the history, the reports and the traces (spec 40, B2).

Each method takes the user ID that the start screen sent. The user comes from `users.yaml`
only; the model never sets it. Each read goes through the same owner checks as the CLI:
`load_for_resume()` for a conversation, `ReportLibrary` for a report, and the `user_id` of the
`turn` span for a trace. A conversation, report or trace of another user gives the same
"not found" message as an unknown ID.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from opsfleet_agent.cli.status import OTHER, STATUS_TEXTS, status_text
from opsfleet_agent.config import AppConfig
from opsfleet_agent.errors import AgentError, ConfigError, ReportNotFound
from opsfleet_agent.identity import UserContext, resolve_user
from opsfleet_agent.memory import conversations
from opsfleet_agent.memory.conversations import canonical_id
from opsfleet_agent.observability.progress import ProgressEvent
from opsfleet_agent.reports.library import ReportLibrary
from opsfleet_agent.resilience.safe_node import internal_message, log_exception
from opsfleet_agent.session import CommandOutcome, ConfirmationView, TurnResult
from opsfleet_agent.startup import StartError
from opsfleet_agent.store.db import connect
from opsfleet_agent.tracing import init_tracing
from opsfleet_agent.ui import views
from opsfleet_agent.ui.history import conversation_turns, report_of_trace
from opsfleet_agent.ui.trace_reader import load_turn, load_turns

Emit = Callable[[str, dict[str, Any]], None]
TRACE_NOT_FOUND = "Trace not found."
REPORT_START = "plan_report"  # from this node on, the time limit of a report applies


def limit_text(kind: str, seconds: float) -> str:
    """The limit line under the status text, for example "Limit for a question: 60 s"."""
    return f"Limit for a {kind}: {seconds:g} s"


class UiError(Exception):
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(UiError):
    status = 404


class Busy(UiError):
    status = 409


class SessionLike(Protocol):
    """The part of `AssistantSession` that the UI uses (spec 33, B7, and spec 34, B7)."""

    conversation_id: str

    @property
    def pending(self) -> ConfirmationView | None: ...

    def send(
        self,
        text: str,
        on_answer: Callable[[TurnResult], None] | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> TurnResult: ...

    def cancel_pending(self, reason: str = "abandoned") -> TurnResult | None: ...

    def command(self, name: str, args: list[str]) -> CommandOutcome: ...


class SessionFactory(Protocol):
    def create(self, user: UserContext) -> SessionLike: ...

    def resume(self, user: UserContext, conversation_id: str) -> SessionLike: ...


class AssistantFactory:
    """The real sessions: `startup.start_session()`, the same start as `opsfleet-agent chat`.
    The services are built once when the server starts. A start problem (for example a wrong
    brand of the user, spec 30 B2.8) raises `StartError`."""

    def __init__(self, config: AppConfig, services: Any) -> None:
        self.config = config
        self.services = services

    def create(self, user: UserContext) -> SessionLike:
        return self._start(user.id)

    def resume(self, user: UserContext, conversation_id: str) -> SessionLike:
        return self._start(user.id, conversation_id)

    def _start(self, user_id: str, resume: str | None = None) -> SessionLike:
        from opsfleet_agent.startup import start_session

        started = start_session(self.config, user_id, resume, services=self.services)
        return started.session  # type: ignore[return-value]


def _start_error(exc: StartError) -> UiError:
    """An unknown conversation, or one of another user (spec 34), is "not found". Another start
    problem, for example a wrong brand in `users.yaml`, is a normal error."""
    if isinstance(exc.__cause__, AgentError):
        return NotFound(exc.message)
    return UiError(exc.message)


@dataclass
class LiveSession:
    session: SessionLike
    user_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)


class UiBackend:
    def __init__(
        self,
        config: AppConfig,
        factory: SessionFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.settings = config.settings
        self.factory = factory
        self.clock = clock
        self.trace_dir: Path = self.settings.data_path("traces")
        init_tracing(self.trace_dir)
        self._live: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(
            self.settings.data_path("app.db"),
            busy_timeout_s=self.settings.resilience.sqlite_busy_timeout_s,
        )
        try:
            yield conn
        finally:
            conn.close()

    # --- users -------------------------------------------------------------------------------------
    def users(self) -> list[dict[str, Any]]:
        return [
            views.user_view(resolve_user(self.config.users, u.id))
            for u in self.config.users.users
        ]

    def user(self, user_id: str) -> UserContext:
        try:
            return resolve_user(self.config.users, user_id)
        except ConfigError as exc:
            raise NotFound(str(exc)) from exc

    # --- conversations -----------------------------------------------------------------------------
    def start(self, user_id: str) -> dict[str, Any]:
        """A new conversation of one user (spec 40, A2.1)."""
        user = self.user(user_id)
        try:
            session = self.factory.create(user)
        except StartError as exc:
            raise UiError(exc.message) from exc
        with self._lock:
            self._live[session.conversation_id] = LiveSession(session, user.id)
        return {
            "conversation_id": session.conversation_id,
            "user": views.user_view(user),
        }

    def conversations(self, user_id: str) -> list[dict[str, Any]]:
        user = self.user(user_id)
        days = self.settings.context.resume_max_days
        with self._db() as conn:
            rows = conversations.list_for_user(conn, user.id, self.clock(), days)
        return [
            {
                "id": r.id,
                "title": r.title,
                "started_at": r.started_at,
                "last_active_at": r.last_active_at,
                "turn_count": r.turn_count,
            }
            for r in rows
        ]

    def conversation(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        """The turns of one conversation, with the cost of each answer from its trace."""
        user = self.user(user_id)
        days = self.settings.context.resume_max_days
        with self._db() as conn:
            try:
                row = conversations.load_for_resume(
                    conn, conversation_id, user.id, self.clock(), days
                )
            except AgentError as exc:
                raise NotFound(exc.user_message) from exc
            turns = conversation_turns(conn, row.id)
            reports = {
                t["trace_id"]: report_of_trace(conn, user.id, t["trace_id"])
                for t in turns
                if t["intent"] == "report"
            }
        traces = load_turns(
            self.trace_dir,
            [t["trace_id"] for t in turns if t["trace_id"]],
            user.id,
            views.scope_text(user),
        )
        live = self._live_of(user, row.id)
        pending = live.session.pending if live and not live.lock.locked() else None
        return {
            "id": row.id,
            "title": row.title,
            "user": views.user_view(user),
            "turns": [
                views.history_turn_view(
                    t, traces.get(t["trace_id"] or ""), reports.get(t["trace_id"])
                )
                for t in turns
            ],
            "pending": views.confirmation_view(pending) if pending else None,
        }

    def _live_of(self, user: UserContext, conversation_id: str) -> LiveSession | None:
        with self._lock:
            live = self._live.get(canonical_id(conversation_id))
        if live is not None and live.user_id != user.id:
            return None
        return live

    def session(self, user_id: str, conversation_id: str) -> LiveSession:
        """The live session of the conversation. An earlier conversation is resumed (spec 34):
        a delete that waited in it expires."""
        user = self.user(user_id)
        with self._lock:
            live = self._live.get(canonical_id(conversation_id))
            if live is not None:
                if live.user_id != user.id:
                    raise NotFound(
                        conversations.ConversationNotFound(
                            conversation_id, user.id
                        ).user_message
                    )
                return live
            try:
                session = self.factory.resume(user, conversation_id)
            except StartError as exc:
                raise _start_error(exc) from exc
            live = LiveSession(session, user.id)
            self._live[session.conversation_id] = live
            return live

    @contextmanager
    def turn_lock(self, live: LiveSession) -> Iterator[None]:
        """One answer at a time in a conversation. The page locks the input too."""
        if not live.lock.acquire(blocking=False):
            raise Busy("An answer is in progress. Wait for it.")
        try:
            yield
        finally:
            live.lock.release()

    def begin_turn(self, user_id: str, conversation_id: str, text: str) -> LiveSession:
        """Check the request before the stream starts, and take the lock of the session."""
        if not text.strip():
            raise UiError("Type a question.")
        live = self.session(user_id, conversation_id)
        if not live.lock.acquire(blocking=False):
            raise Busy("An answer is in progress. Wait for it.")
        return live

    def run_turn(self, live: LiveSession, text: str, emit: Emit) -> None:
        """Answer one message and send the events (in a worker thread). `begin_turn()` took the
        lock; this method releases it. Text that is not the confirmation cancels a waiting delete
        and goes again as a new question (spec 33, A2.8)."""
        session = live.session
        limits = self.settings.limits.per_turn
        max_attempts = self.settings.agent.max_attempts_per_step

        def on_progress(event: ProgressEvent) -> None:
            """The status texts of the CLI (spec 37, B2.2). A node that is not in the table keeps
            the current text. A report has its own time limit."""
            if event.node not in STATUS_TEXTS:
                return
            stage: dict[str, Any] = {"text": status_text(event, max_attempts)}
            if event.node == REPORT_START:
                stage["limit"] = limit_text("report", limits.max_seconds_report)
            emit("stage", stage)

        emit(
            "stage",
            {
                "text": OTHER,
                "limit": limit_text("question", limits.max_seconds_question),
            },
        )
        try:
            result = self._send(session, text, emit, on_progress)
            if result.replay_input:
                self._send(session, text, emit, on_progress)
        except Exception:  # noqa: BLE001 - the page continues after any bug (spec 36)
            log_exception("ui", None)
            emit("error", {"message": internal_message(None)})
        finally:
            live.lock.release()
            emit("done", {})

    @staticmethod
    def _send(
        session: SessionLike,
        text: str,
        emit: Emit,
        on_progress: Callable[[ProgressEvent], None],
    ) -> TurnResult:
        def on_answer(result: TurnResult) -> None:
            emit("turn", views.turn_view(result, final=False))

        result = session.send(text, on_answer=on_answer, on_progress=on_progress)
        emit("turn", views.turn_view(result))
        return result

    def confirm(self, user_id: str, conversation_id: str, text: str) -> dict[str, Any]:
        """The typed confirmation of a waiting delete. The session checks the text (spec 33)."""
        live = self.session(user_id, conversation_id)
        with self.turn_lock(live):
            if live.session.pending is None:
                raise UiError("No delete waits for a confirmation.")
            return views.turn_view(live.session.send(text))

    def cancel(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        live = self.session(user_id, conversation_id)
        with self.turn_lock(live):
            result = live.session.cancel_pending("abandoned")
        if result is None:
            raise UiError("No delete waits for a confirmation.")
        return views.turn_view(result)

    def undo(
        self, user_id: str, conversation_id: str, batch_id: str | None
    ) -> dict[str, Any]:
        """The Undo link after a delete: the same as `/undo <batch>` in the CLI."""
        live = self.session(user_id, conversation_id)
        with self.turn_lock(live):
            outcome = live.session.command("undo", [batch_id] if batch_id else [])
        return {"kind": outcome.kind, "text": outcome.text}

    # --- traces ------------------------------------------------------------------------------------
    def steps(self, user_id: str, trace_id: str) -> dict[str, Any]:
        user = self.user(user_id)
        trace = load_turn(self.trace_dir, trace_id, user.id, views.scope_text(user))
        if trace is None:
            raise NotFound(TRACE_NOT_FOUND)
        return views.steps_view(trace, self.settings.limits.per_turn)

    def spans(self, user_id: str, trace_id: str) -> list[dict[str, Any]]:
        user = self.user(user_id)
        trace = load_turn(self.trace_dir, trace_id, user.id)
        if trace is None:
            raise NotFound(TRACE_NOT_FOUND)
        return list(trace.spans)

    # --- reports -----------------------------------------------------------------------------------
    @contextmanager
    def _library(self) -> Iterator[ReportLibrary]:
        with self._db() as conn:
            yield ReportLibrary(conn, self.settings, clock=self.clock)

    def reports(self, user_id: str) -> list[dict[str, Any]]:
        user = self.user(user_id)
        with self._library() as library:
            listing = library.list(user)
        return [views.report_summary_view(r) for r in listing.reports]

    def report(self, user_id: str, report_id: str) -> dict[str, Any]:
        user = self.user(user_id)
        with self._library() as library:
            try:
                report = library.get(user, report_id)
            except ReportNotFound as exc:
                raise NotFound(exc.user_message) from exc
        return views.report_view(report)

    def report_file(self, user_id: str, report_id: str, fmt: str) -> tuple[str, str]:
        """Write the file of an own report, as `/export` does, and return its text and name."""
        if fmt not in ("html", "md"):
            raise UiError("The format must be html or md.")
        user = self.user(user_id)
        with self._library() as library:
            try:
                path = library.export(user, report_id, fmt)  # type: ignore[arg-type]
            except ReportNotFound as exc:
                raise NotFound(exc.user_message) from exc
            except AgentError as exc:  # for example a file that is too large
                raise UiError(exc.user_message) from exc
        return path.read_text(encoding="utf-8"), path.name
