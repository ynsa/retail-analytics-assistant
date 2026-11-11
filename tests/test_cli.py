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
