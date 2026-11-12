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
