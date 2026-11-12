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

    def send(self, text):
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
