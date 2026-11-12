"""The CLI (spec 37, B4 cases 1-17). A fake session or the real session with fakes, Typer's
`CliRunner`, and Rich consoles of 80 columns for the snapshots. No model call, no query."""

from __future__ import annotations

import io
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from rich.console import Console
from typer.testing import CliRunner

import opsfleet_agent.cli.main as cli
from opsfleet_agent import capabilities
from opsfleet_agent.cli import chat_loop as loop
from opsfleet_agent.cli.main import app
from opsfleet_agent.cli.render import Ui
from opsfleet_agent.commands import CommandOutcome
from opsfleet_agent.observability.progress import ProgressEvent
from opsfleet_agent.session import ConfirmationView, Greeting, TurnResult
from opsfleet_agent.startup import Started, StartError
from tests.conftest import FakeBQ, FakeLLM, frame_result, route_reply, sql_reply
from tests.test_observability import fixture_40

runner = CliRunner()
SNAPSHOTS = Path(__file__).parent / "snapshots"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TRACE = "4f2a9c1e00112233445566778899aabb"
T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
# Glossary filter and the stated period of `sql_reply` (the checks of spec 31, B7 pass).
ORDERS_SQL = (
    f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {T_ITEMS} oi "
    "WHERE oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01') "
    "AND oi.status NOT IN ('Cancelled', 'Returned')"
)


# --- helpers ---------------------------------------------------------------------------------------
def record_ui(**kwargs: Any) -> Ui:
    console = Console(width=80, record=True, color_system=None, file=_Null())
    return Ui(console, plain=kwargs.pop("plain", False), status_line=False, **kwargs)


class _Null:
    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None


def text_of(ui: Ui) -> str:
    return "\n".join(line.rstrip() for line in ui.console.export_text().splitlines())


def assert_snapshot(name: str, text: str) -> None:
    """Compare with `tests/snapshots/<name>.txt`. A missing file is written (first run)."""
    path = SNAPSHOTS / f"{name}.txt"
    text = text.rstrip("\n") + "\n"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def result(answer: str = "There were 1,200 orders.", **fields: Any) -> TurnResult:
    base: dict[str, Any] = {
        "answer": answer,
        "error_type": None,
        "trace_id": TRACE,
        "totals": {
            "seconds": 12.4,
            "llm_calls": 3,
            "bq_queries": 2,
            "bytes_billed": 34_000_000,
        },
        "intent": "analysis",
    }
    base.update(fields)
    return TurnResult(**base)


VIEW = ConfirmationView(
    kind="delete_reports",
    rows=(
        {
            "report_id": "R-7K2PQ",
            "title": "Q1 2026 sales review",
            "created": "2026-11-12",
            "reason": "this conv.",
        },
        {
            "report_id": "R-3M9XA",
            "title": "Levi's returns, October",
            "created": "2026-11-12",
            "reason": "this conv.",
        },
        {
            "report_id": "R-9QF2D",
            "title": "Calvin Klein vs Carhartt, Q3",
            "created": "2026-11-12",
            "reason": "this conv.",
        },
    ),
    count=3,
    expected_text="delete 3",
    text="list",
)


@dataclass
class FakeSession:
    """The interface of `AssistantSession` that the CLI uses. It records each call."""

    results: list[Any] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    commands: list[tuple[str, list[str]]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    pending: Any = None
    fail_with: BaseException | None = None
    missing: list[str] = field(default_factory=list)
    turns: int = 1
    conversation_id: str = "C-8M2QX"
    progress: list[ProgressEvent] = field(default_factory=list)
    llm_calls: int = 0  # a command never adds to these two
    bq_queries: int = 0

    def send(self, text, on_answer=None, on_progress=None):
        self.sent.append(text)
        if self.fail_with is not None:
            raise self.fail_with
        for event in self.progress:
            if on_progress is not None:
                on_progress(event)
        item = self.results.pop(0) if self.results else result()
        self.llm_calls += item.totals.get("llm_calls", 0)
        self.pending = item.confirmation
        return item

    def command(self, name, args):
        self.commands.append((name, args))
        from opsfleet_agent.commands import unknown

        if name == "export" and args[-1:] == ["pdf"]:
            return CommandOutcome("error", "Usage: /export <id> --format html|md")
        if name == "nothing":
            return unknown(name)
        return CommandOutcome("message", f"{name} output")

    def cancel_pending(self, reason="abandoned"):
        if self.pending is None:
            return None
        self.cancelled.append(reason)
        self.pending = None
        return result("Delete cancelled. Nothing was deleted.", delete_outcome=reason)

    def close(self):
        return self.cancel_pending("abandoned")

    def turn_count(self):
        return self.turns

    def greeting(self, resumed=False):
        return Greeting(
            name="Maya (brand manager)",
            role="manager",
            brands=("Calvin Klein", "Carhartt"),
            data_from=date(2019, 1, 7),
            data_to=date(2026, 11, 12),
            conversation_id=self.conversation_id,
            resumed=resumed,
        )

    def missing_capabilities(self):
        return list(self.missing)


def reader(items):
    items = list(items)
    prompts: list[str] = []

    def read(prompt):
        prompts.append(prompt)
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    read.prompts = prompts  # type: ignore[attr-defined]
    return read


def run_chat(monkeypatch, config, session, user_input: str, args=()):
    """`opsfleet-agent chat --user maya` with the fake session and piped input."""
    from opsfleet_agent.identity import resolve_user

    monkeypatch.setattr(cli, "load_config", lambda: config)

    def start(cfg, user, resume=None, refresh_cache=False):
        return Started(session, resolve_user(cfg.users, user), bool(resume))

    monkeypatch.setattr(cli, "start_session", start)
    return runner.invoke(app, ["chat", "--user", "maya", *args], input=user_input)


# --- B4 #1: --help ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "options"),
    [
        ([], ["chat", "conversations", "stats", "purge"]),
        (["chat"], ["--user", "--resume", "--refresh-cache", "--debug"]),
        (["conversations"], ["--user"]),
        (["stats"], ["--user", "--all", "--days", "--export"]),
        (["purge"], ["--dry-run"]),
    ],
)
def test_1_help_lists_all_commands_and_options(command, options):
    out = runner.invoke(app, [*command, "--help"])
    assert out.exit_code == 0
    for option in options:
        assert option in out.output


# --- B4 #2, #3: the development-build line ----------------------------------------------------------
def test_2_all_capabilities_registered_no_development_line(monkeypatch, config):
    import opsfleet_agent.session  # noqa: F401 - registers every part

    assert capabilities.missing() == []
    out = run_chat(monkeypatch, config, FakeSession(), "/exit\n")
    assert out.exit_code == 0
    assert "Hello, Maya (brand manager)." in out.output
    assert "Development build" not in out.output
    assert "Type a question, or /help." in out.output
    assert "Conversation: C-8M2QX" in out.output


def test_3_missing_delete_and_memory_named_in_the_line(monkeypatch, config):
    session = FakeSession(
        missing=capabilities.missing(
            frozenset(capabilities.TEXTS) - {"delete", "memory"}
        )
    )
    out = run_chat(monkeypatch, config, session, "/exit\n")
    assert (
        "Development build: delete with confirmation and long-conversation memory "
        "not active yet." in out.output
    )


def test_development_line_text():
    assert capabilities.development_line([]) is None
    assert capabilities.development_line(["guard_full"]) == (
        "Development build: brand scope and PII protection not active yet."
    )


# --- B4 #4: each slash command, no model call and no query -----------------------------------------
@pytest.fixture
def real_session(make_session):
    llm = FakeLLM(
        [route_reply(), sql_reply(ORDERS_SQL), "There were 1,200 orders last month."]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    session, llm, bq = make_session("maya", llm=llm, bq=bq)
    return session, llm, bq


def test_4_each_slash_command_with_the_real_session(real_session):
    session, llm, bq = real_session
    ui = record_ui()
    loop.chat_loop(
        ui,
        session,
        "maya",
        read=reader(
            ["How many orders last month?"]
            + [
                "/help",
                "/reports",
                "/open R-ZZZZZ",
                "/export R-ZZZZZ --format md",
                "/undo",
                "/trace",
                "/stats",
                "/stats --days 30",
                "/exit",
            ]
        ),
    )
    calls, queries = len(llm.calls), len(bq.queries)
    assert (calls, queries) == (3, 1)  # the question only
    out = text_of(ui)
    assert (
        "/export <id> --format html|md" in out and "Example questions" in out
    )  # /help
    assert "You have no saved reports." in out  # /reports
    assert "Report R-ZZZZZ not found." in out  # /open and /export
    assert f"Trace {session.last_trace_id[:8]}" in out and "node.route" in out  # /trace
    assert "Statistics of user maya, last 7 days" in out  # /stats
    assert "Statistics of user maya, last 30 days" in out
    assert "Questions" in out and "p95: not enough data (n = 1)" in out


def test_4_commands_go_to_the_session_and_make_no_calls():
    session = FakeSession()
    ui = record_ui()
    loop.chat_loop(
        ui,
        session,
        "maya",
        read=reader(
            ["/reports", "/export R-7K2PQ --format md", "/trace 4f2a9c1e", EOFError()]
        ),
    )
    assert session.commands == [
        ("reports", []),
        ("export", ["R-7K2PQ", "--format", "md"]),
        ("trace", ["4f2a9c1e"]),
    ]
    assert session.sent == [] and session.llm_calls == 0 and session.bq_queries == 0


# --- B4 #5, #6: wrong arguments, unknown command ---------------------------------------------------
def test_5_export_with_a_wrong_format_shows_the_usage(real_session):
    session, llm, _ = real_session
    outcome = session.command("export", ["R-1", "--format", "pdf"])
    assert outcome.kind == "error"
    assert outcome.text == "Usage: /export <id> --format html|md"
    assert session.command("stats", ["--days", "200"]).text == (
        "Usage: /stats [--days N] (N from 1 to 90)"
    )
    assert session.command("open", []).text == "Usage: /open <id>"
    assert llm.calls == []


def test_6_unknown_command(real_session):
    session, _, _ = real_session
    ui = record_ui()
    loop.chat_loop(ui, session, "maya", read=reader(["/nothing", "/exit"]))
    assert "Unknown command '/nothing'. Type /help." in text_of(ui)


# --- B4 #7: an answer with a Markdown table ---------------------------------------------------------
def test_7_answer_with_a_markdown_table_snapshot():
    ui = record_ui()
    ui.result(
        result(
            "Revenue of your brands in October 2026:\n\n"
            "| Brand | Revenue | Orders |\n|---|---:|---:|\n"
            "| Calvin Klein | $8,120.40 | 131 |\n| Carhartt | $5,716.47 | 88 |\n\n"
            "Period: 1–31 Oct 2026 · Brands: Calvin Klein, Carhartt"
        )
    )
    out = text_of(ui)
    assert "12.4 s · 3 model calls · 2 queries · 34.0 MB · trace 4f2a9c1e" in out
    assert max(len(line) for line in out.splitlines()) <= 80
    assert_snapshot("answer_table", out)


def test_footer_can_be_turned_off():
    ui = record_ui(show_footer=False)
    ui.result(result())
    assert "model calls" not in text_of(ui)


# --- B4 #8, #9, #10: the confirmation --------------------------------------------------------------
def test_8_confirmation_panel_and_prompt_snapshot():
    session = FakeSession(results=[result("list", confirmation=VIEW)])
    ui = record_ui()
    read = reader(["Delete all the reports of this conversation", "/exit"])
    loop.chat_loop(ui, session, "maya", read=read)
    out = text_of(ui)
    assert read.prompts == ["maya> ", "confirm> "]
    assert "To delete these 3 reports, type: delete 3" in out
    assert_snapshot("confirmation_panel", out)


def test_8_both_outcomes_have_their_colors():
    ui = Ui(
        Console(width=80, record=True, force_terminal=True, file=_Null()),
        plain=False,
        status_line=False,
    )
    ui.result(
        result(
            "Deleted 3 reports. Undo with /undo (batch D-4F8KQ).",
            delete_outcome="deleted",
        )
    )
    ui.result(
        result(
            "Delete cancelled. Nothing was deleted.",
            delete_outcome="not_exact",
            error_type="confirmation_rejected",
        )
    )
    styled = ui.console.export_text(styles=True)
    assert "\x1b[32mDeleted 3 reports" in styled  # green
    assert "\x1b[33mDelete cancelled" in styled  # yellow
    assert "model calls" not in styled  # no footer after a delete line


def test_9_reports_at_confirm_keeps_the_pending_delete():
    session = FakeSession(results=[result("list", confirmation=VIEW)])
    ui = record_ui()
    read = reader(["delete my reports", "/reports", "/stats", "", "delete 3"])
    session.results.append(result("Deleted 3 reports.", delete_outcome="deleted"))
    loop.chat_loop(ui, session, "maya", read=_then_eof(read))
    assert read.prompts[:5] == [
        "maya> ",
        "confirm> ",
        "confirm> ",
        "confirm> ",
        "confirm> ",
    ]
    assert session.sent == ["delete my reports", "delete 3"]
    assert session.cancelled == []


def _then_eof(read):
    def wrapped(prompt):
        try:
            return read(prompt)
        except IndexError:
            raise EOFError from None

    return wrapped


def test_10_other_text_at_confirm_is_cancelled_then_sent_again():
    session = FakeSession(
        results=[
            result("list", confirmation=VIEW),
            result(
                "Delete cancelled. Nothing was deleted.",
                replay_input=True,
                delete_outcome="other_text",
                error_type="confirmation_rejected",
            ),
            result("There were 12 orders in May."),
        ]
    )
    ui = record_ui()
    loop.chat_loop(
        ui,
        session,
        "maya",
        read=reader(["delete them", "How many orders in May?", "/exit"]),
    )
    out = text_of(ui)
    assert session.sent == [
        "delete them",
        "How many orders in May?",
        "How many orders in May?",
    ]
    assert out.index("Delete cancelled. Nothing was deleted.") < out.index(
        "There were 12 orders"
    )


def test_exit_at_confirm_cancels_and_shows_the_resume_hint():
    session = FakeSession(results=[result("list", confirmation=VIEW)])
    ui = record_ui()
    loop.chat_loop(ui, session, "maya", read=reader(["delete them", "/exit"]))
    loop.end_chat(ui, session, "maya")
    out = text_of(ui)
    assert session.cancelled == ["abandoned"]
    assert "Delete cancelled. Nothing was deleted." in out
    assert "opsfleet-agent chat --user maya --resume C-8M2QX" in out


def test_ctrl_c_at_confirm_cancels_the_delete():
    session = FakeSession(results=[result("list", confirmation=VIEW)])
    ui = record_ui()
    loop.chat_loop(
        ui, session, "maya", read=reader(["delete them", KeyboardInterrupt(), "/exit"])
    )
    assert session.cancelled == ["abandoned"]
    assert "Delete cancelled. Nothing was deleted." in text_of(ui)


# --- B4 #11: each error type ------------------------------------------------------------------------
FAILURES = [
    "llm_timeout",
    "llm_rate_limited",
    "llm_error",
    "bq_syntax",
    "bq_not_found",
    "bq_permission",
    "bq_timeout",
    "bq_bytes_cap",
    "bq_error",
    "guard_parse_error",
    "tool_error",
    "internal_error",
]


@pytest.mark.parametrize("error_type", FAILURES)
def test_11_failures_are_one_red_error_line_with_the_trace(error_type):
    ui = Ui(
        Console(width=80, record=True, force_terminal=True, file=_Null()),
        plain=False,
        status_line=False,
    )
    ui.result(
        result(
            "The service failed. Trace: 4f2a9c1e.",
            error_type=error_type,
            status="error",
        )
    )
    styled = ui.console.export_text(styles=True)
    plain = ANSI.sub("", styled).strip()
    assert plain == "Error: The service failed. Trace: 4f2a9c1e."
    assert "\x1b[31m" in styled  # red


def test_11_limit_and_block_lines():
    ui = record_ui()
    ui.result(
        result(
            "This question needs more queries than allowed (6).",
            error_type="limit_exceeded",
        )
    )
    ui.result(result("I cannot show customer emails.", error_type="guard_policy_block"))
    ui.result(result("No orders match these filters.", error_type="empty_result"))
    ui.result(result("Report R-1 not found.", error_type="tool_error"))
    ui.result(result("I can only help with the sales data.", intent="off_topic"))
    lines = [line for line in text_of(ui).splitlines() if line.strip()]
    assert lines == [
        "Limit: This question needs more queries than allowed (6). Trace: 4f2a9c1e.",
        "Not possible: I cannot show customer emails.",
        "Not possible: No orders match these filters.",
        "Report R-1 not found.",
        "I can only help with the sales data.",
    ]


def test_11_error_line_without_a_trace_in_the_text_gets_it():
    ui = record_ui()
    ui.result(
        result(
            "I could not get a valid result after 3 attempts.", error_type="bq_syntax"
        )
    )
    assert (
        "Error: I could not get a valid result after 3 attempts. Trace: 4f2a9c1e."
        in text_of(ui)
    )


# --- B4 #12, #13: exceptions ------------------------------------------------------------------------
def test_12_exception_in_send_shows_the_internal_line_and_continues():
    session = FakeSession(fail_with=RuntimeError("a bug"))
    ui = record_ui()
    loop.chat_loop(
        ui, session, "maya", read=reader(["orders?", "orders again?", EOFError()])
    )
    out = text_of(ui)
    assert out.count("Error: Something went wrong on my side. The chat continues.") == 2
    assert "Traceback" not in out and "RuntimeError" not in out


def test_13_debug_shows_the_traceback():
    session = FakeSession(fail_with=RuntimeError("a bug"))
    ui = record_ui()
    loop.chat_loop(
        ui, session, "maya", read=reader(["orders?", EOFError()]), debug=True
    )
    out = text_of(ui)
    assert "Traceback" in out and "RuntimeError" in out


def test_ctrl_c_during_an_answer_shows_stopped():
    session = FakeSession(fail_with=KeyboardInterrupt())
    ui = record_ui()
    loop.chat_loop(ui, session, "maya", read=reader(["orders?", "/exit"]))
    assert "Stopped." in text_of(ui)


def test_two_ctrl_c_within_2_seconds_end_the_chat():
    session = FakeSession()
    ui = record_ui()
    times = iter([10.0, 11.0])
    loop.chat_loop(
        ui,
        session,
        "maya",
        read=reader([KeyboardInterrupt(), KeyboardInterrupt(), "never"]),
        clock=lambda: next(times),
    )
    assert "Press Ctrl-C again" in text_of(ui) and session.sent == []


def test_one_ctrl_c_keeps_the_chat():
    session = FakeSession()
    ui = record_ui()
    times = iter([10.0, 20.0])
    loop.chat_loop(
        ui,
        session,
        "maya",
        read=reader([KeyboardInterrupt(), "orders?", KeyboardInterrupt(), "/exit"]),
        clock=lambda: next(times),
    )
    assert session.sent == ["orders?"]


# --- B4 #14, #15: piped input, Ctrl-D ---------------------------------------------------------------
def test_14_piped_input_gives_plain_text(monkeypatch, config):
    session = FakeSession(results=[result("There were **41,000** orders in 2024.")])
    out = run_chat(monkeypatch, config, session, "How many orders in 2024?\n/exit\n")
    assert out.exit_code == 0
    assert "\x1b[" not in out.output  # no ANSI codes
    assert "maya> How many orders in 2024?" in out.output  # the input is shown
    assert "There were 41,000 orders in 2024." in out.output
    assert "Reading the question" not in out.output and "Working" not in out.output
    assert session.sent == ["How many orders in 2024?"]


def test_15_ctrl_d_shows_the_resume_hint_and_exit_code_0(monkeypatch, config):
    session = FakeSession(results=[result()])
    out = run_chat(monkeypatch, config, session, "orders?\n")  # the input ends: Ctrl-D
    assert out.exit_code == 0
    assert (
        "Conversation C-8M2QX saved. Continue it with: "
        "opsfleet-agent chat --user maya --resume C-8M2QX" in out.output
    )


def test_no_resume_hint_without_a_turn(monkeypatch, config):
    out = run_chat(monkeypatch, config, FakeSession(turns=0), "/exit\n")
    assert "saved" not in out.output


# --- B4 #16: start errors ---------------------------------------------------------------------------
def test_16_unknown_user(monkeypatch, config):
    monkeypatch.setattr(cli, "load_config", lambda: config)
    out = runner.invoke(app, ["chat", "--user", "nobody"])
    assert out.exit_code == 2
    assert "User 'nobody' is not in config/users.yaml." in out.output


def test_closed_stdin_asks_for_docker_it(monkeypatch, config):
    """Spec 53, A4: `docker run` without `-i` gives /dev/null as stdin."""
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "stdin_is_closed", lambda: True)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 2
    assert cli.NO_TERMINAL in out.output


def test_stdin_is_closed_only_for_a_device_that_is_not_a_terminal(monkeypatch):
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        assert cli.stdin_is_closed()
    read_end, write_end = os.pipe()
    with os.fdopen(read_end) as pipe, os.fdopen(write_end, "w"):
        monkeypatch.setattr(sys, "stdin", pipe)
        # Piped input: printf ... | opsfleet-agent chat
        assert not cli.stdin_is_closed()
    monkeypatch.setattr(sys, "stdin", io.StringIO("text"))
    assert not cli.stdin_is_closed()  # no file descriptor (CliRunner)
    monkeypatch.setattr(sys, "stdin", None)
    assert not cli.stdin_is_closed()


def test_16_data_folder_not_writable(monkeypatch, config, tmp_path):
    """Spec 53, B6: Docker on Linux without --user cannot write to the host data folder."""
    from opsfleet_agent.config import Paths

    blocked = tmp_path / "blocked"
    blocked.write_text(
        "a file, not a folder"
    )  # mkdir fails as for a folder without rights
    settings = config.settings.model_copy(
        update={"paths": Paths(data_dir=blocked / "data")}
    )
    cfg = config.model_copy(update={"settings": settings})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    for args in (["chat", "--user", "maya"], ["stats", "--all"], ["purge"]):
        out = runner.invoke(app, args)
        assert out.exit_code == 2, args
        assert "Cannot write to the data folder" in out.output
        assert '--user "$(id -u):$(id -g)"' in out.output
        assert "Traceback" not in out.output


def test_16_missing_env_value(monkeypatch, config):
    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key=None, google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 2
    assert "GOOGLE_API_KEY is not set. See the setup guide." in out.output


def test_16_resume_not_found(monkeypatch, config):
    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key="k", google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    out = runner.invoke(app, ["chat", "--user", "maya", "--resume", "C-AAAAA"])
    assert out.exit_code == 2
    assert "Conversation C-AAAAA not found for user maya." in out.output


def test_16_wrong_brand_and_bigquery_down(monkeypatch, config):
    from opsfleet_agent import startup
    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key="k", google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    from google.api_core import exceptions as gexc

    def down(*_a, **_k):
        raise gexc.ServiceUnavailable("down")

    import opsfleet_agent.session as session_module

    monkeypatch.setattr(session_module, "build_services", down)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 3
    assert out.output.strip().startswith(
        "The assistant could not start. BigQuery is not available now."
    )

    def brand_problem(*_a, **_k):
        raise StartError("Brand 'Calvn Klein' of user maya is not in the data.", 2)

    monkeypatch.setattr(cli, "start_session", brand_problem)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 2 and "Calvn Klein" in out.output
    assert startup.start_error(gexc.ServiceUnavailable("x")).startswith(
        "The assistant could not start."
    )


def test_16_start_error_shows_one_message_and_logs_the_details(
    monkeypatch, config, capfd
):
    """Spec 53, A5: without credentials, the client's BigQuery file logs an error on the root
    logger. The log goes to data/logs. The screen shows only the message."""
    import logging

    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key="k", google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    def no_credentials(*_a, **_k):
        # The same call as in bq_client.py (the root logger).
        logging.error("Failed to initialize BigQuery client: no creds")  # noqa: LOG015
        exc = type("DefaultCredentialsError", (Exception,), {})
        raise exc("Your default credentials were not found.")

    import opsfleet_agent.session as session_module

    monkeypatch.setattr(session_module, "build_services", no_credentials)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 3
    assert "gcloud auth application-default login" in out.output
    assert "Failed to initialize" not in out.output + capfd.readouterr().err
    logs = list(cfg.settings.data_path("logs").glob("*.jsonl"))
    assert logs and "Failed to initialize" in logs[0].read_text()


# --- B4 #17: progress events ------------------------------------------------------------------------
def test_17_status_texts_in_order():
    from opsfleet_agent.cli.render import Status
    from opsfleet_agent.cli.status import status_text

    seen: list[str] = []

    class Live:
        def update(self, label):
            seen.append(label.text)

    status = Status(Live(), max_attempts=3)
    for node, attempt in [
        ("load_context", 1),
        ("route", 1),
        ("write_sql", 1),
        ("guard", 1),
        ("write_sql", 2),
        ("guard", 1),
        ("run_query", 1),
        ("check_empty", 1),
        ("mask_result", 1),
        ("write_answer", 1),
        ("scan_output", 1),
        ("plan_report", 1),
        ("write_report", 1),
        ("render", 1),
        ("summarize", 1),
    ]:
        status.progress(ProgressEvent(node, attempt))
    assert seen == [
        "Working",
        "Reading the question",
        "Writing the query",
        "Checking the query",
        "Writing the query (attempt 2 of 3)",
        "Checking the query",
        "Running the query (BigQuery)",
        "Checking why the result is empty",
        "Writing the answer",
        "Planning the report",
        "Writing the report",
        "Drawing the charts",
        "Updating the conversation memory",
    ]
    assert status_text(ProgressEvent("unknown_node")) == "Working"


def test_17_session_sends_progress_events_from_the_node_spans(make_session):
    llm = FakeLLM(
        [route_reply(), sql_reply(ORDERS_SQL), "There were 1,200 orders last month."]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    session, _, _ = make_session("maya", llm=llm, bq=bq)
    events: list[ProgressEvent] = []
    session.send("How many orders last month?", on_progress=events.append)
    nodes = [e.node for e in events]
    for expected in (
        "load_context",
        "route",
        "write_sql",
        "guard",
        "run_query",
        "write_answer",
        "finish",
    ):
        assert expected in nodes
    assert nodes.index("route") < nodes.index("write_sql") < nodes.index("run_query")


def test_17_retry_shows_the_attempt_number(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply("SELECT FROM WHERE"),
            sql_reply(ORDERS_SQL),
            "1,200 orders.",
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    events: list[ProgressEvent] = []
    session.send("How many orders?", on_progress=events.append)
    attempts = [e.attempt for e in events if e.node == "write_sql"]
    assert attempts == [1, 2]


# --- other output ------------------------------------------------------------------------------------
def test_greeting_snapshot():
    ui = record_ui()
    ui.greeting(FakeSession().greeting(), None)
    assert_snapshot("greeting", text_of(ui))


def test_message_markup_is_not_interpreted():
    ui = record_ui()
    ui.result(
        result("Contact [removed] for [bold]x[/bold].", error_type="guard_policy_block")
    )
    assert "Not possible: Contact [removed] for [bold]x[/bold]." in text_of(ui)


# --- spec 35: the operator commands ------------------------------------------------------------
def test_15_stats_command_exports(monkeypatch, config, conn):
    from typer.testing import CliRunner

    import opsfleet_agent.cli.main as cli

    fixture_40(conn)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    out = CliRunner().invoke(
        cli.app, ["stats", "--all", "--days", "30", "--export", "csv"]
    )
    assert out.exit_code == 0 and "Saved:" in out.output
    files = list(config.settings.data_path("exports").glob("stats-*.csv"))
    assert len(files) == 1
    shown = CliRunner().invoke(cli.app, ["stats", "--user", "maya", "--days", "90"])
    assert (
        shown.exit_code == 0 and "Statistics of user maya, last 90 days" in shown.output
    )
    assert CliRunner().invoke(cli.app, ["stats"]).exit_code == 2
    assert (
        CliRunner().invoke(cli.app, ["stats", "--all", "--export", "pdf"]).exit_code
        == 2
    )


def test_purge_command_runs_the_retention(monkeypatch, config):
    from typer.testing import CliRunner

    import opsfleet_agent.cli.main as cli

    monkeypatch.setattr(cli, "load_config", lambda: config)
    out = CliRunner().invoke(cli.app, ["purge"])
    assert out.exit_code == 0 and "Nothing to purge." in out.output
    assert "Retention: 0 trace files" in out.output


def test_session_errors_outside_a_turn_do_not_end_the_cli():
    class Broken(FakeSession):
        def close(self):
            raise RuntimeError("checkpoint database is gone")

    session = Broken(results=[result("list", confirmation=VIEW)])
    session.cancel_pending = lambda reason="abandoned": (_ for _ in ()).throw(
        RuntimeError("x")
    )
    ui = record_ui()
    loop.chat_loop(
        ui, session, "maya", read=reader(["delete them", KeyboardInterrupt(), "/exit"])
    )
    loop.end_chat(ui, session, "maya")
    out = text_of(ui)
    assert "Delete cancelled. Nothing was deleted." in out
    assert "opsfleet-agent chat --user maya --resume C-8M2QX" in out


def test_start_config_error_gives_exit_code_2(monkeypatch, config):
    import opsfleet_agent.session as session_module
    from opsfleet_agent.config import Secrets
    from opsfleet_agent.errors import ConfigError

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key="k", google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    def broken(*_a, **_k):
        raise ConfigError("Configuration is not valid: persona file missing.")

    monkeypatch.setattr(session_module, "build_services", broken)
    out = runner.invoke(app, ["chat", "--user", "maya"])
    assert out.exit_code == 2 and "persona file missing" in out.output
