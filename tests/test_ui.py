"""The local test UI API (spec 40, B4): users, conversations, the SSE stream, the delete
confirmation, the steps of a turn and the report owner. FastAPI `TestClient`, a fake session
and a fake trace file: no model call, no query."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsfleet_agent.config import AppConfig, ReportSettings
from opsfleet_agent.identity import UserContext
from opsfleet_agent.memory import conversations
from opsfleet_agent.observability.progress import ProgressEvent
from opsfleet_agent.reports.library import ReportLibrary
from opsfleet_agent.session import CommandOutcome, ConfirmationView, TurnResult
from opsfleet_agent.store.db import connect
from opsfleet_agent.ui.backend import UiBackend
from opsfleet_agent.ui.server import STATIC, create_app
from tests.report_helpers import make_report

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
TRACE_A = "a" * 32
TRACE_B = "b" * 32
TOTALS = {
    "llm_calls": 3,
    "bq_queries": 1,
    "tokens_in": 3120,
    "tokens_out": 240,
    "bytes_billed": 20_971_520,
    "seconds": 7.4,
}
ANSWER = (
    "Revenue fell by 12%.\n\n| Brand | Oct |\n|---|---|\n| Carhartt | $22,800 |\n\n"
    "<script>alert(1)</script>"
)
CONFIRM = ConfirmationView(
    kind="delete_reports",
    rows=(
        {"report_id": "R-AAAAA", "title": "Q1 report", "created": "12 Nov 10:20"},
        {"report_id": "R-BBBBB", "title": "October drop", "created": "12 Nov 10:31"},
    ),
    count=2,
    expected_text="delete 2",
    text="These 2 reports will be deleted …",
)


def result(**fields: Any) -> TurnResult:
    base = TurnResult(
        answer=ANSWER,
        error_type=None,
        trace_id=TRACE_A,
        totals=dict(TOTALS),
        intent="analysis",
        turn_no=1,
    )
    return replace(base, **fields)


@dataclass
class FakeSession:
    """The part of `AssistantSession` that the UI uses. Each `send()` sends the progress events
    of `progress` (spec 37, B2.2) before the answer."""

    conversation_id: str
    user: UserContext
    results: list[TurnResult] = field(default_factory=list)
    waiting: ConfirmationView | None = None
    sent: list[str] = field(default_factory=list)
    commands: list[tuple[str, list[str]]] = field(default_factory=list)
    progress: list[ProgressEvent] = field(
        default_factory=lambda: [
            ProgressEvent("load_context"),  # not in the table: no new text
            ProgressEvent("route"),
            ProgressEvent("run_query"),
        ]
    )

    @property
    def pending(self) -> ConfirmationView | None:
        return self.waiting

    def send(
        self,
        text: str,
        on_answer: Callable[[TurnResult], None] | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> TurnResult:
        self.sent.append(text)
        for event in self.progress:
            if on_progress is not None:
                on_progress(event)
        outcome = self.results.pop(0)
        if on_answer is not None:
            on_answer(outcome)
        if outcome.confirmation is None:
            self.waiting = None
        return outcome

    def cancel_pending(self, reason: str = "abandoned") -> TurnResult | None:
        if self.waiting is None:
            return None
        self.waiting = None
        return result(answer="Delete cancelled. Nothing was deleted.", trace_id=None)

    def command(self, name: str, args: list[str]) -> CommandOutcome:
        self.commands.append((name, args))
        return CommandOutcome("message", "2 reports restored.")


@dataclass
class FakeFactory:
    """Creates fake sessions. Like the real session, it writes the conversation row."""

    config: AppConfig
    sessions: list[FakeSession] = field(default_factory=list)
    queued: list[TurnResult] = field(default_factory=list)
    next_id: int = 0

    def create(self, user: UserContext) -> FakeSession:
        self.next_id += 1
        session = FakeSession(f"C-TEST{self.next_id}", user, list(self.queued))
        conn = connect(self.config.settings.data_path("app.db"))
        try:
            conversations.create(conn, session.conversation_id, user.id, NOW)
        finally:
            conn.close()
        self.sessions.append(session)
        return session

    def resume(self, user: UserContext, conversation_id: str) -> FakeSession:
        return self.create(user)


@pytest.fixture
def factory(config: AppConfig) -> FakeFactory:
    return FakeFactory(config)


@pytest.fixture
def backend(config: AppConfig, factory: FakeFactory) -> UiBackend:
    return UiBackend(config, factory, clock=lambda: NOW)


@pytest.fixture
def client(backend: UiBackend) -> TestClient:
    return TestClient(create_app(backend), base_url="http://127.0.0.1")


def events_of(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        name = lines[0].removeprefix("event: ")
        out.append((name, json.loads(lines[1].removeprefix("data: "))))
    return out


def start(client: TestClient, user: str = "maya") -> str:
    response = client.post("/api/conversations", json={"user": user})
    assert response.status_code == 200, response.text
    return response.json()["conversation_id"]


def write_trace(config: AppConfig, spans: list[dict[str, Any]]) -> None:
    folder = config.settings.data_path("traces")
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "2026-11-12.jsonl").open("a", encoding="utf-8") as fh:
        for record in spans:
            fh.write(json.dumps(record) + "\n")


def record(
    trace_id: str,
    span_id: str,
    name: str,
    kind: str,
    parent: str | None,
    ms: float,
    start: str = "2026-11-12T09:00:00.000Z",
    status: str = "ok",
    **attrs: Any,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "kind": kind,
        "start": start,
        "duration_ms": ms,
        "status": status,
        "attributes": attrs,
    }


def data_question_trace(trace_id: str = TRACE_A, user: str = "maya") -> list[dict]:
    """A data question in the trace format of spec 00, as the session writes it today."""
    t = trace_id
    return [
        record(t, "n1", "node.route", "node", "t0", 300, "2026-11-12T09:00:00.100Z", intent="analysis"),
        record(t, "l1", "llm.route", "llm", "n1", 290, "2026-11-12T09:00:00.101Z", tokens_in=2000, tokens_out=100),
        record(t, "n2", "node.write_sql", "node", "t0", 1900, "2026-11-12T09:00:00.500Z", attempt=1),
        record(t, "l2", "llm.write_sql", "llm", "n2", 1890, "2026-11-12T09:00:00.501Z", tokens_in=1000, tokens_out=100),
        record(t, "n3", "node.guard", "node", "t0", 17, "2026-11-12T09:00:02.500Z"),
        record(t, "g1", "guard.check", "guard", "n3", 16, "2026-11-12T09:00:02.501Z", outcome="pass"),
        record(t, "n4", "node.run_query", "node", "t0", 1400, "2026-11-12T09:00:02.600Z", step_index=0),
        record(t, "q1", "bq.query", "sql", "n4", 1390, "2026-11-12T09:00:02.601Z", bytes_billed=20_971_520),
        record(t, "n5", "node.mask_result", "node", "t0", 2, "2026-11-12T09:00:04.000Z"),
        record(t, "n6", "node.write_answer", "node", "t0", 2100, "2026-11-12T09:00:04.100Z"),
        record(t, "l3", "llm.write_answer", "llm", "n6", 2090, "2026-11-12T09:00:04.101Z", tokens_in=120, tokens_out=40),
        record(t, "n7", "node.finish", "node", "t0", 0.1, "2026-11-12T09:00:06.300Z"),
        record(
            t, "t0", "turn", "turn", None, 6300, "2026-11-12T09:00:00.000Z",
            user_id=user, conversation_id="C-8M2QX", llm_calls=3, bq_queries=1,
            tokens_in=3120, tokens_out=240, bytes_billed=20_971_520, seconds=6.3,
            intent="analysis",
        ),
    ]  # fmt: skip


# --- users and start (A2.1) -----------------------------------------------------------------------
def test_users_come_from_users_yaml(client):
    users = client.get("/api/users").json()
    assert [u["id"] for u in users] == ["ceo", "maya", "alex"]
    maya = users[1]
    assert maya["role"] == "manager" and maya["scope"] == "Calvin Klein, Carhartt"
    assert users[0]["scope"] == "all brands"


def test_start_creates_a_conversation_and_refuses_an_unknown_user(client, factory):
    conversation_id = start(client)
    assert conversation_id == "C-TEST1"
    assert factory.sessions[0].user.id == "maya"
    response = client.post("/api/conversations", json={"user": "mallory"})
    assert response.status_code == 404
    assert "Unknown user 'mallory'" in response.json()["detail"]


def test_page_and_static_files_are_local_only(client):
    page = client.get("/")
    assert page.status_code == 200 and "Who is testing?" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    for name in ("index.html", "app.js", "styles.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        # No CDN and no external font: the page works offline (spec 40, A5).
        assert not re.search(r"(src|href)=\"https?://", text), name
        assert "@import" not in text and "fonts.googleapis" not in text
    assert client.get("/static/app.js").status_code == 200


def test_other_host_names_are_refused(backend):
    client = TestClient(create_app(backend), base_url="http://evil.example")
    assert client.get("/api/users").status_code == 400


# --- the SSE stream (A2.3) ------------------------------------------------------------------------
def test_message_streams_stages_then_the_answer(client, factory):
    factory.queued = [result()]
    conversation_id = start(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "Why did the sales drop?"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = events_of(response.text)
    names = [name for name, _ in events]
    assert names[0] == "stage" and names[-1] == "done"
    stages = [data["text"] for name, data in events if name == "stage"]
    assert stages == ["Working", "Reading the question", "Running the query (BigQuery)"]
    assert events[0][1]["limit"] == "Limit for a question: 60 s"
    turns = [data for name, data in events if name == "turn"]
    assert [t["final"] for t in turns] == [
        False,
        True,
    ]  # on_answer, then the final result
    answer = turns[-1]
    assert "<table>" in answer["answer_html"]
    assert "<script>" not in answer["answer_html"]  # raw HTML stays text
    assert "&lt;script&gt;" in answer["answer_html"]
    assert answer["cost_line"] == (
        "Answered in 7.4 s · 3 model calls · 1 query · 21.0 MB billed"
    )
    assert answer["label"] is None
    assert factory.sessions[0].sent == ["Why did the sales drop?"]


def test_a_refusal_gets_an_amber_label(client, factory):
    blocked = result(
        error_type="guard_policy_block",
        steps=({"reason_code": "pii_column"},),
        answer="I cannot show personal data.",
    )
    factory.queued = [blocked]
    conversation_id = start(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "Show the emails of the top customers"},
    )
    turn = [d for n, d in events_of(response.text) if n == "turn"][-1]
    assert turn["label"] == {"text": "Refused · personal data", "tone": "amber"}


def test_an_empty_message_and_a_second_message_at_the_same_time_are_refused(
    client, backend, factory
):
    conversation_id = start(client)
    empty = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "   "},
    )
    assert empty.status_code == 400
    live = backend.session("maya", conversation_id)
    live.lock.acquire()
    try:
        busy = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"user": "maya", "text": "Again"},
        )
    finally:
        live.lock.release()
    assert busy.status_code == 409
    assert factory.sessions[0].sent == []


def test_an_error_in_the_session_gives_an_error_event_and_frees_the_lock(
    client, backend, factory
):
    conversation_id = start(client)  # no queued result: the fake raises IndexError
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "Hello"},
    )
    names = [n for n, _ in events_of(response.text)]
    assert names[-2:] == ["error", "done"]
    assert not backend.session("maya", conversation_id).lock.locked()


def test_another_user_cannot_use_the_conversation(client):
    conversation_id = start(client, "maya")
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "alex", "text": "Show Maya's data"},
    )
    assert response.status_code == 404
    assert "not found for user alex" in response.json()["detail"]


# --- the delete confirmation (A2.4) ----------------------------------------------------------------
def test_confirmation_card_then_the_exact_text_deletes(client, factory):
    factory.queued = [
        result(confirmation=CONFIRM, answer=CONFIRM.text, intent="delete"),
        result(
            answer="2 reports deleted.",
            delete_outcome="deleted",
            batch_id="D-AAAAA",
            intent="delete",
        ),
    ]
    conversation_id = start(client)
    session = factory.sessions[0]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "Delete the reports of this conversation"},
    )
    turn = [d for n, d in events_of(response.text) if n == "turn"][-1]
    assert turn["confirmation"]["expected_text"] == "delete 2"
    assert [r["report_id"] for r in turn["confirmation"]["rows"]] == [
        "R-AAAAA",
        "R-BBBBB",
    ]
    assert turn["answer_html"] == "" and turn["label"] is None
    session.waiting = CONFIRM  # the real session pauses here
    view = client.get(f"/api/conversations/{conversation_id}?user=maya")
    assert view.json()["pending"]["expected_text"] == "delete 2"

    confirmed = client.post(
        f"/api/conversations/{conversation_id}/confirm",
        json={"user": "maya", "text": "delete 2"},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["delete_outcome"] == "deleted" and body["batch_id"] == "D-AAAAA"
    assert session.sent[-1] == "delete 2"  # the session checks the text, not the UI

    undo = client.post(
        f"/api/conversations/{conversation_id}/undo",
        json={"user": "maya", "batch_id": "D-AAAAA"},
    )
    assert undo.json() == {"kind": "message", "text": "2 reports restored."}
    assert session.commands == [("undo", ["D-AAAAA"])]


def test_confirm_without_a_waiting_delete_is_refused(client, factory):
    conversation_id = start(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/confirm",
        json={"user": "maya", "text": "delete 2"},
    )
    assert response.status_code == 400
    assert factory.sessions[0].sent == []


def test_cancel_button_cancels_the_waiting_delete(client, factory):
    conversation_id = start(client)
    factory.sessions[0].waiting = CONFIRM
    response = client.post(
        f"/api/conversations/{conversation_id}/cancel", json={"user": "maya"}
    )
    assert "Nothing was deleted" in response.json()["answer_html"]
    again = client.post(
        f"/api/conversations/{conversation_id}/cancel", json={"user": "maya"}
    )
    assert again.status_code == 400


def test_a_new_message_cancels_the_delete_and_is_sent_again(client, factory):
    factory.queued = [
        result(
            answer="Delete cancelled. Nothing was deleted.",
            replay_input=True,
            delete_outcome="other_text",
            trace_id=TRACE_B,
        ),
        result(),
    ]
    conversation_id = start(client)
    factory.sessions[0].waiting = CONFIRM
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "What about Levi's?"},
    )
    turns = [d for n, d in events_of(response.text) if n == "turn" and d["final"]]
    assert [t["trace_id"] for t in turns] == [TRACE_B, TRACE_A]
    assert factory.sessions[0].sent == ["What about Levi's?", "What about Levi's?"]


# --- history and steps (A2.2) ---------------------------------------------------------------------
def add_history(config: AppConfig, user: str = "maya") -> None:
    conn = connect(config.settings.data_path("app.db"))
    try:
        conversations.create(conn, "C-8M2QX", user, NOW)
        from opsfleet_agent.guard.scanner import OutputScanner

        conversations.record_turn(
            conn,
            OutputScanner(),
            conversation_id="C-8M2QX",
            turn_no=1,
            trace_id=TRACE_A,
            started_at=NOW,
            user_text="Why did the sales drop?",
            answer_text="Revenue fell by **12%**.",
            intent="analysis",
            status="ok",
            error_type=None,
        )
        # The figures of spec 35, as the session writes them at the end of the turn.
        conn.execute(
            "UPDATE conversation_turns SET duration_ms = 6300, llm_calls = 3, "
            "tokens_in = 3120, tokens_out = 240, bq_queries = 1, "
            "bq_bytes_billed = 20971520 WHERE conversation_id = 'C-8M2QX'"
        )
        conn.commit()
    finally:
        conn.close()
    write_trace(config, data_question_trace(TRACE_A, user))


def test_history_lists_only_the_users_conversations(client, config):
    add_history(config)
    rows = client.get("/api/conversations?user=maya").json()
    assert [r["id"] for r in rows] == ["C-8M2QX"]
    assert rows[0]["title"] == "Why did the sales drop?" and rows[0]["turn_count"] == 1
    assert client.get("/api/conversations?user=alex").json() == []


def test_an_earlier_conversation_shows_its_turns_with_the_cost(client, config):
    add_history(config)
    data = client.get("/api/conversations/C-8M2QX?user=maya").json()
    turn = data["turns"][0]
    assert turn["question"] == "Why did the sales drop?"
    assert "<strong>12%</strong>" in turn["answer_html"]
    assert turn["cost_line"] == (
        "Answered in 6.3 s · 3 model calls · 1 query · 21.0 MB billed"
    )
    other = client.get("/api/conversations/C-8M2QX?user=alex")
    assert other.status_code == 404


def test_steps_pane_shows_the_steps_and_the_limits(client, config):
    write_trace(config, data_question_trace())
    data = client.get(f"/api/turns/{TRACE_A}?user=maya").json()
    titles = [s["title"] for s in data["steps"]]
    assert titles == [
        "Route: data question",
        "Write SQL",
        "SQL guard: pass",
        "BigQuery: 1 query",
        "Write the answer",
    ]
    assert data["steps"][0]["meta"] == "Gemini · 0.3 s"
    assert data["steps"][2]["note"] == "scope: Calvin Klein, Carhartt"
    assert data["steps"][3]["meta"] == "21.0 MB · 1.4 s"
    assert data["totals"] == [
        "Tokens: 3,120 in · 240 out",
        "Model calls: 3 of 8",
        "Queries: 1 of 6",
        "Bytes billed: 21.0 MB",
        "Time: 6.3 s of 60 s",
    ]
    trace = client.get(f"/api/turns/{TRACE_A}/trace?user=maya")
    assert trace.headers["content-type"].startswith("text/plain")
    assert trace.text.startswith("Trace aaaaaaaa (13 steps)")  # the text of /trace
    assert "bq.query · 1.4 s · bytes_billed=21.0 MB" in trace.text
    assert client.get(f"/api/turns/{TRACE_A}/trace?user=alex").status_code == 404


def test_a_trace_of_another_user_is_not_found(client, config):
    write_trace(config, data_question_trace())
    for path in (f"/api/turns/{TRACE_A}?user=alex", "/api/turns/../x?user=maya"):
        response = client.get(path)
        assert response.status_code == 404
    assert client.get(f"/api/turns/{TRACE_B}?user=maya").json() == {
        "detail": "Trace not found."
    }


def test_steps_show_the_masked_sql_and_the_totals_of_the_spans(client, config):
    """A blocked query shows the SQL of the model in the guard step, a query that ran shows its
    SQL in the BigQuery step. A turn that stopped with an internal error has no figures on its
    `turn` span: the totals then come from the child spans."""
    t = TRACE_B
    write_trace(
        config,
        [
            record(t, "n1", "node.guard", "node", "t0", 20),
            record(
                t,
                "g1",
                "guard.check",
                "guard",
                "n1",
                10,
                outcome="policy_block",
                reason_code="pii_column",
                sql_masked="SELECT email FROM users",
            ),
            record(t, "n2", "node.run_query", "node", "t0", 900, step_index=0),
            record(
                t,
                "q1",
                "bq.query",
                "sql",
                "n2",
                800,
                bytes_billed=10_000_000,
                sql_masked="SELECT … WHERE brand IN ('<s:12>')",
            ),
            record(
                t,
                "n3",
                "node.write_answer",
                "node",
                "t0",
                500,
                status="error",
                error_type="llm_timeout",
            ),
            record(
                t, "l1", "llm.generate", "llm", "n3", 500, tokens_in=10, tokens_out=0
            ),
            record(
                t, "t0", "turn", "turn", None, 1500, user_id="maya", intent="report"
            ),
        ],
    )
    data = client.get(f"/api/turns/{TRACE_B}?user=maya").json()
    guard, query, answer = data["steps"]
    assert guard["title"] == "SQL guard: block"
    assert guard["note"] == "reason: pii column"
    assert guard["sql"] == "SELECT email FROM users"
    assert query["title"] == "BigQuery: 1 query"
    assert query["sql"] == "SELECT … WHERE brand IN ('<s:12>')"
    assert answer["status"] == "error" and answer["note"] == "llm_timeout"
    assert "Model calls: 1 of 8" in data["totals"]
    assert "Bytes billed: 10.0 MB" in data["totals"]
    assert "Time: 1.5 s of 150 s" in data["totals"]  # the report limit


def test_an_old_turn_without_figures_has_no_cost_line(client, config):
    add_history(config)
    conn = connect(config.settings.data_path("app.db"))
    try:
        conn.execute("UPDATE conversation_turns SET duration_ms = 0")
        conn.commit()
    finally:
        conn.close()
    turn = client.get("/api/conversations/C-8M2QX?user=maya").json()["turns"][0]
    assert turn["cost_line"] == "" and turn["totals"] is None


# --- reports (A2.5) -------------------------------------------------------------------------------
def save_report(config: AppConfig, report_id: str, owner: str, **fields: Any) -> None:
    conn = connect(config.settings.data_path("app.db"))
    try:
        ReportLibrary(conn, config.settings).create(
            make_report(report_id, owner, **fields)
        )
    finally:
        conn.close()


def chart_data() -> dict[str, Any]:
    from opsfleet_agent.agent.formatting import store_result
    from opsfleet_agent.guard.masker import MaskedResult
    from opsfleet_agent.reports.charts import check_chart
    from opsfleet_agent.reports.models import ChartSpec

    masked = MaskedResult(
        columns=["month", "revenue"],
        rows=[["2026-01", 1200.5], ["2026-02", 1300.25], ["2026-03", 900.0]],
        row_count=3,
        truncated=False,
    )
    results = {"r1": store_result(masked, "r1", "test")}
    spec = ChartSpec(ref="r1", type="bar", x="month", y=["revenue"], title="Revenue")
    data, _ = check_chart(spec, results, ReportSettings())
    return data


def test_reports_list_only_the_owners_reports(client, config):
    save_report(config, "R-AAAAA", "maya", title="Q1 report: my brands")
    save_report(config, "R-BBBBB", "alex", title="Levi's report")
    assert [r["report_id"] for r in client.get("/api/reports?user=maya").json()] == [
        "R-AAAAA"
    ]
    assert [r["report_id"] for r in client.get("/api/reports?user=alex").json()] == [
        "R-BBBBB"
    ]


def test_report_card_has_the_sections_the_chart_and_the_actions(client, config):
    save_report(
        config,
        "R-AAAAA",
        "maya",
        charts=[chart_data()],
        sections={
            "summary": ["Sales fell."],
            "period_text": "1 Jan – 31 Mar 2026",
            "action_items": [{"action": "Restock Carhartt outerwear."}],
        },
    )
    card = client.get("/api/reports/R-AAAAA?user=maya").json()
    assert card["sections"][0] == "Summary" and len(card["sections"]) == 6
    assert card["action_items"] == ["Restock Carhartt outerwear."]
    assert card["charts"][0]["svg"].startswith("<svg")
    assert card["period_text"] == "1 Jan – 31 Mar 2026"
    assert client.get("/api/reports/R-AAAAA?user=alex").status_code == 404


def test_report_files_only_for_the_owner(client, config):
    save_report(config, "R-AAAAA", "maya")
    html = client.get("/api/reports/R-AAAAA/file?user=maya&format=html")
    assert html.status_code == 200 and html.text.lstrip().startswith("<!")
    assert html.headers["content-security-policy"].startswith("default-src 'none'")
    md = client.get("/api/reports/r-aaaaa/file?user=maya&format=md")
    assert md.status_code == 200
    assert md.headers["content-disposition"] == 'attachment; filename="R-AAAAA.md"'
    assert (config.settings.reports_dir() / "maya" / "R-AAAAA.md").is_file()

    other = client.get("/api/reports/R-AAAAA/file?user=alex&format=html")
    assert other.status_code == 404
    assert not (config.settings.reports_dir() / "alex").exists()
    bad = client.get("/api/reports/R-AAAAA/file?user=maya&format=pdf")
    assert bad.status_code == 400


# --- the status texts and the CLI ----------------------------------------------------------------
def test_report_stages_use_the_status_texts_and_the_report_limit(client, factory):
    """The same texts as the CLI status line (spec 37, B2.2). A retry shows its attempt, and
    the report flow changes the limit line."""
    factory.queued = [result(intent="report")]
    conversation_id = start(client)
    factory.sessions[0].progress = [
        ProgressEvent("route"),
        ProgressEvent("plan_report"),
        ProgressEvent("write_sql"),
        ProgressEvent("write_sql", attempt=2),
        ProgressEvent("run_query"),
        ProgressEvent("render"),
    ]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "Create a report for Q3."},
    )
    stages = [d for n, d in events_of(response.text) if n == "stage"]
    assert [s["text"] for s in stages] == [
        "Working",
        "Reading the question",
        "Planning the report",
        "Writing the query",
        "Writing the query (attempt 2 of 3)",
        "Running the query (BigQuery)",
        "Drawing the charts",
    ]
    assert [s.get("limit") for s in stages[:3]] == [
        "Limit for a question: 60 s",
        None,
        "Limit for a report: 150 s",
    ]


def test_ui_command_is_in_the_cli():
    from typer.testing import CliRunner

    from opsfleet_agent.cli.main import app

    output = CliRunner().invoke(app, ["ui", "--help"]).output
    assert "local test UI" in output and "--port" in output


def real_backend(config: AppConfig, persona_file: Path, monkeypatch, llm, bq):
    """The real `AssistantFactory` (`startup.start_session()`) with a fake Gemini and a fake
    BigQuery. The services come from the factory, so the start builds none."""
    import opsfleet_agent.session as session_module
    from opsfleet_agent.agent.context import PromptLibrary
    from opsfleet_agent.agent.state import Services
    from opsfleet_agent.config import Secrets
    from opsfleet_agent.ui.backend import AssistantFactory
    from tests.conftest import load_test_schema, load_test_values

    def no_build(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the UI builds the services once, at the server start")

    monkeypatch.setattr(session_module, "build_services", no_build)
    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key="k", google_cloud_project="p")}
    )
    services = Services(
        settings=cfg.settings,
        llm=llm,
        bq=bq,
        schema=load_test_schema(),
        values=load_test_values(),
        prompts=PromptLibrary(persona_path=persona_file),
    )
    return UiBackend(cfg, AssistantFactory(cfg, services))


def test_real_session_through_the_ui_api(config, persona_file, monkeypatch):
    """One data question with the real `AssistantSession`, a fake Gemini and a fake BigQuery:
    the stages come from the real node spans, and the steps from the real trace file."""
    import pandas as pd

    from tests.conftest import FakeBQ, FakeLLM, frame_result, route_reply, sql_reply
    from tests.test_agent_flow import ORDERS_SQL

    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "There were 1,200 orders."])
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    backend = real_backend(config, persona_file, monkeypatch, llm, bq)
    client = TestClient(create_app(backend), base_url="http://127.0.0.1")
    conversation_id = start(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"user": "maya", "text": "How many orders last month?"},
    )
    events = events_of(response.text)
    stages = [d["text"] for n, d in events if n == "stage"]
    assert stages[0] == "Working"
    assert {
        "Reading the question",
        "Writing the query",
        "Checking the query",
        "Running the query (BigQuery)",
        "Writing the answer",
    } <= set(stages)
    turn = [d for n, d in events if n == "turn"][-1]
    assert turn["answer_html"].startswith("<p>There were 1,200 orders.")
    steps = client.get(f"/api/turns/{turn['trace_id']}?user=maya").json()
    titles = [s["title"] for s in steps["steps"]]
    assert titles[:3] == ["Route: data question", "Write SQL", "SQL guard: pass"]
    assert "Model calls: 3 of 8" in steps["totals"]
    # The history reads the figures that the session wrote to `conversation_turns`.
    earlier = client.get(f"/api/conversations/{conversation_id}?user=maya").json()
    assert " · 3 model calls · 1 query · " in earlier["turns"][0]["cost_line"]
    history = client.get("/api/conversations?user=maya").json()
    assert history[0]["id"] == conversation_id
    assert Path(config.settings.data_path("traces")).is_dir()


def test_real_start_errors(config, persona_file, monkeypatch):
    """`start_session()` checks the start: an unknown conversation is "not found" (spec 34), a
    wrong brand of the user stops the start (spec 30, B2.8)."""
    import opsfleet_agent.session as session_module
    from opsfleet_agent.errors import ConfigError
    from tests.conftest import FakeBQ, FakeLLM

    backend = real_backend(config, persona_file, monkeypatch, FakeLLM(), FakeBQ())
    client = TestClient(create_app(backend), base_url="http://127.0.0.1")
    response = client.post(
        "/api/conversations/C-ZZZZZ/messages", json={"user": "maya", "text": "Hi"}
    )
    assert response.status_code == 404
    assert "C-ZZZZZ not found" in response.json()["detail"]

    def wrong_brand(*_a: Any, **_k: Any) -> list[str]:
        raise ConfigError("Brand 'Calvn Klein' of user maya is not in the data.")

    monkeypatch.setattr(session_module, "check_user_brands", wrong_brand)
    response = client.post("/api/conversations", json={"user": "maya"})
    assert response.status_code == 400
    assert "Calvn Klein" in response.json()["detail"]


def test_ui_command_start_error(monkeypatch, config):
    """A start problem stops the `ui` command with the message and the exit code of the CLI."""
    from typer.testing import CliRunner

    from opsfleet_agent.cli import main as cli
    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key=None, google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    out = CliRunner().invoke(cli.app, ["ui"])
    assert out.exit_code == 2 and "GOOGLE_API_KEY is not set" in out.output
