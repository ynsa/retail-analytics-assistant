from typer.testing import CliRunner

from opsfleet_agent.cli.main import app

runner = CliRunner()


def test_help_works():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0 and "chat" in result.output


def test_unknown_user_stops_with_exit_code_2():
    result = runner.invoke(app, ["chat", "--user", "nobody"])
    assert result.exit_code == 2
    assert "Unknown user" in result.output


class _Outcome:
    def __init__(self, kind, text, path=None):
        self.kind, self.text, self.path = kind, text, path


class _FakeSession:
    def __init__(self):
        self.calls = []

    def command(self, name, args):
        self.calls.append((name, args))
        if name == "export":
            return _Outcome(
                "message",
                "Saved: data/reports/maya/R-7K2PQ.md",
                "data/reports/maya/R-7K2PQ.md",
            )
        return _Outcome("message", "You have no saved reports.")


def test_report_commands_go_to_the_session(capsys):
    from opsfleet_agent.cli.main import run_command

    session = _FakeSession()
    run_command(session, "/reports")
    run_command(session, "/export R-7K2PQ --format md")
    run_command(session, "/nothing")
    out = capsys.readouterr().out
    assert session.calls == [("reports", []), ("export", ["R-7K2PQ", "--format", "md"])]
    assert (
        "Saved: data/reports/maya/R-7K2PQ.md" in out
        and "Unknown command '/nothing'" in out
    )


class _View:
    rows = (
        {
            "report_id": "R-00001",
            "title": "Q1 2026 sales review",
            "created": "2026-11-12",
            "reason": "this conversation",
        },
    )
    count = 1
    expected_text = "delete 1"
    not_found = ()


class _Result:
    def __init__(
        self, answer, confirmation=None, replay=False, outcome=None, error_type=None
    ):
        self.answer, self.confirmation, self.replay_input = answer, confirmation, replay
        self.delete_outcome, self.error_type, self.trace_id = outcome, error_type, "abc"
        self.totals = {
            "seconds": 0.1,
            "llm_calls": 0,
            "bq_queries": 0,
            "bytes_billed": 0,
        }


class _ReplaySession:
    def __init__(self):
        self.sent = []

    def send(self, text, on_answer=None):
        self.sent.append(text)
        if len(self.sent) == 1:
            return _Result(
                "Delete cancelled. Nothing was deleted.",
                replay=True,
                outcome="other_text",
                error_type="confirmation_rejected",
            )
        return _Result("There were 12 orders.")


def test_confirmation_panel_shows_the_list_and_the_text(capsys):
    from opsfleet_agent.cli.main import show_result

    show_result(_Result("text", confirmation=_View()))
    out = capsys.readouterr().out
    assert "Delete these reports?" in out and "R-00001" in out
    assert "To delete this report, type: delete 1" in out


def test_other_text_at_confirm_is_sent_again(capsys):
    from opsfleet_agent.cli.main import send

    session = _ReplaySession()
    send(session, "How many orders in May?")
    out = capsys.readouterr().out
    assert session.sent == ["How many orders in May?", "How many orders in May?"]
    assert (
        "Delete cancelled. Nothing was deleted." in out
        and "There were 12 orders." in out
    )


def test_purge_dry_run_command(monkeypatch, config):
    import opsfleet_agent.cli.main as cli

    monkeypatch.setattr(cli, "load_config", lambda: config)
    result = runner.invoke(app, ["purge", "--dry-run"])
    assert result.exit_code == 0 and "Nothing to purge." in result.output


# --- spec 36: no crash, Ctrl-C ------------------------------------------------------------------


class _LoopSession:
    def __init__(self, fail_with=None):
        self.pending = None
        self.sent = []
        self.fail_with = fail_with
        self.cancelled = []

    def send(self, text, on_answer=None):
        self.sent.append(text)
        if self.fail_with is not None:
            raise self.fail_with
        return _Result("There were 12 orders.")

    def cancel_pending(self, reason):
        self.cancelled.append(reason)
        self.pending = None


def _reader(items):
    items = list(items)

    def read(prompt):
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return read


def test_two_ctrl_c_within_2_seconds_end_the_chat(capsys):
    from opsfleet_agent.cli.main import chat_loop

    session = _LoopSession()
    times = iter([10.0, 11.0])
    chat_loop(
        session,
        "maya",
        read=_reader([KeyboardInterrupt(), KeyboardInterrupt(), "never"]),
        clock=lambda: next(times),
    )
    assert "Press Ctrl-C again" in capsys.readouterr().out and session.sent == []


def test_one_ctrl_c_keeps_the_chat(capsys):
    from opsfleet_agent.cli.main import chat_loop

    session = _LoopSession()
    times = iter([10.0, 20.0])
    chat_loop(
        session,
        "maya",
        read=_reader([KeyboardInterrupt(), "orders?", KeyboardInterrupt(), "/exit"]),
        clock=lambda: next(times),
    )
    assert session.sent == ["orders?"]
    assert "There were 12 orders." in capsys.readouterr().out


def test_ctrl_c_at_confirm_cancels_the_delete(capsys):
    from opsfleet_agent.cli.main import chat_loop

    session = _LoopSession()
    session.pending = object()
    chat_loop(session, "maya", read=_reader([KeyboardInterrupt(), "/exit"]))
    assert session.cancelled == ["abandoned"]
    assert "Delete cancelled. Nothing was deleted." in capsys.readouterr().out


def test_an_exception_in_a_turn_does_not_end_the_cli(capsys, tmp_path):
    from opsfleet_agent.cli.main import chat_loop

    session = _LoopSession(fail_with=RuntimeError("bug"))
    chat_loop(session, "maya", read=_reader(["orders?", EOFError()]))
    out = capsys.readouterr().out
    assert "Something went wrong on my side. The chat continues." in out
    assert "RuntimeError" not in out  # no stack trace on the screen


def test_ctrl_c_outside_the_session_shows_stopped(capsys):
    from opsfleet_agent.cli.main import chat_loop

    session = _LoopSession(fail_with=KeyboardInterrupt())
    chat_loop(session, "maya", read=_reader(["orders?", "/exit"]))
    assert "Stopped." in capsys.readouterr().out


def test_missing_env_value_stops_the_start_and_names_it(monkeypatch, config):
    import opsfleet_agent.cli.main as cli
    from opsfleet_agent.config import Secrets

    cfg = config.model_copy(
        update={"secrets": Secrets(google_api_key=None, google_cloud_project="p")}
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    result = runner.invoke(app, ["chat", "--user", "maya"])
    assert result.exit_code == 2 and "GOOGLE_API_KEY is not set" in result.output


def test_start_error_with_bigquery_down_is_clear():
    from google.api_core import exceptions as gexc

    from opsfleet_agent.cli.main import start_error

    text = start_error(gexc.ServiceUnavailable("down"))
    assert text.startswith(
        "The assistant could not start. BigQuery is not available now."
    )
