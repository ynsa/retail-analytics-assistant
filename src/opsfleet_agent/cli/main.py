"""Command-line interface (spec 37): a thin Typer app over `AssistantSession`.

    opsfleet-agent chat --user maya [--resume C-8M2QX] [--refresh-cache] [--debug]
    opsfleet-agent conversations --user maya
    opsfleet-agent stats [--user maya | --all] [--days N] [--export csv|json]
    opsfleet-agent purge [--dry-run]
    opsfleet-agent ui [--port 8000]

The rules of the assistant are in the session and the core modules. This package only reads the
input and shows the results.
"""

from __future__ import annotations

from datetime import UTC, datetime

import typer

from opsfleet_agent.cli.chat_loop import chat_loop, end_chat
from opsfleet_agent.cli.render import Ui
from opsfleet_agent.config import AppConfig, load_config
from opsfleet_agent.errors import ConfigError
from opsfleet_agent.startup import (
    StartError,
    check_secrets,
    check_user,
    start_services,
    start_session,
)

app = typer.Typer(
    add_completion=False,
    help="Retail analytics chat assistant (prototype).",
    no_args_is_help=True,
)


def _ui(config: AppConfig | None = None) -> Ui:
    if config is None:
        return Ui()
    cli = config.settings.cli
    return Ui(
        show_footer=cli.show_footer,
        status_line=cli.status_line,
        max_attempts=config.settings.agent.max_attempts_per_step,
    )


def _config(ui: Ui) -> AppConfig:
    try:
        return load_config()
    except ConfigError as exc:
        ui.line(str(exc), "red")
        raise typer.Exit(code=2) from exc


@app.callback()
def _root() -> None:
    """Retail analytics chat assistant (prototype)."""


@app.command()
def chat(
    user: str = typer.Option(..., "--user", help="User ID from config/users.yaml."),
    resume: str | None = typer.Option(
        None, "--resume", help="Continue a saved conversation, for example C-8M2QX."
    ),
    refresh_cache: bool = typer.Option(
        False,
        "--refresh-cache",
        help="Build the value cache again (one small query, about 40 MB billed).",
    ),
    debug: bool = typer.Option(
        False, "--debug", help="Show the stack trace of an error on the screen."
    ),
) -> None:
    """Start a chat as one user. Type /help for the commands, /exit or Ctrl-D to end."""
    config = _config(Ui())
    ui = _ui(config)
    if debug:
        from opsfleet_agent.resilience.safe_node import init_logging

        init_logging(config.settings.data_path("logs"), debug=True)
    try:
        with ui.status("Starting: reading the table schemas and the value cache"):
            started = start_session(config, user, resume, refresh_cache)
    except StartError as exc:
        ui.line(exc.message, "red")
        raise typer.Exit(code=exc.exit_code) from exc
    session = started.session
    for warning in started.warnings:
        ui.line(f"Warning: {warning}", "yellow")
    from opsfleet_agent.capabilities import development_line

    ui.greeting(
        session.greeting(resumed=started.resumed),
        development_line(session.missing_capabilities()),
    )
    if started.resumed:
        ui.resume(session.resume_view(), started.user.id)
    chat_loop(ui, session, started.user.id, debug=debug)
    end_chat(ui, session, started.user.id)


@app.command("conversations")
def conversations_command(
    user: str = typer.Option(..., "--user", help="User ID from config/users.yaml."),
) -> None:
    """List the conversations of a user from the last 90 days."""
    from opsfleet_agent.memory.conversations import list_for_user
    from opsfleet_agent.store.db import connect

    ui = Ui()
    config = _config(ui)
    try:
        user_ctx = check_user(config, user)
    except StartError as exc:
        ui.line(exc.message, "red")
        raise typer.Exit(code=exc.exit_code) from exc
    settings = config.settings
    conn = connect(settings.data_path("app.db"))
    try:
        rows = list_for_user(
            conn, user_ctx.id, datetime.now(UTC), settings.context.resume_max_days
        )
    finally:
        conn.close()
    if not rows:
        ui.line(f"No conversations of {user_ctx.id} in the last 90 days.")
        return
    ui.table(
        ("ID", "Started", "Last active", "Turns", "First question"),
        [
            (
                r.id,
                r.started_at[:16].replace("T", " "),
                r.last_active_at[:16].replace("T", " "),
                str(r.turn_count),
                r.title,
            )
            for r in rows
        ],
    )
    ui.line(
        f"Continue one with: opsfleet-agent chat --user {user_ctx.id} --resume <ID>"
    )


@app.command("stats")
def stats_command(
    user: str | None = typer.Option(None, "--user", help="The statistics of one user."),
    all_users: bool = typer.Option(False, "--all", help="The statistics of all users."),
    days: int | None = typer.Option(
        None, "--days", min=1, max=90, help="The period in days (default 7)."
    ),
    export: str | None = typer.Option(
        None,
        "--export",
        help="Write one row for each turn to data/exports/: csv or json. Figures only.",
    ),
) -> None:
    """Statistics of the turns, for the operator. Reads local data only.

    The same figures as /stats in the chat (spec 35), for one user or for all users."""
    from opsfleet_agent.observability.stats import compute_stats
    from opsfleet_agent.observability.stats import export as export_rows
    from opsfleet_agent.store.db import connect

    ui = Ui()
    config = _config(ui)
    if all_users == (user is not None):
        ui.line("Give --user <id> or --all.", "red")
        raise typer.Exit(code=2)
    if export not in (None, "csv", "json"):
        ui.line("Usage: --export csv|json", "red")
        raise typer.Exit(code=2)
    user_id = None
    if user is not None:
        try:
            user_id = check_user(config, user).id
        except StartError as exc:
            ui.line(exc.message, "red")
            raise typer.Exit(code=exc.exit_code) from exc
    settings = config.settings
    period = days or settings.stats.default_days
    now = datetime.now(UTC)
    conn = connect(settings.data_path("app.db"))
    try:
        if export:
            path, count = export_rows(
                conn,
                now,
                period,
                user_id,
                export,
                settings.data_path(settings.stats.export_dir),
            )
            ui.saved(f"Saved: {path} ({count} turns)", str(path))
            return
        ui.stats(compute_stats(conn, now, period, user_id))
    finally:
        conn.close()


@app.command("purge")
def purge_command(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the batches and the counts. Change nothing."
    ),
) -> None:
    """Delete old deleted reports for good, and remove old traces, logs and texts.

    Reports that users deleted more than 30 days ago (spec 33). Traces and logs older than
    30 days, and question and answer texts older than 90 days (spec 35)."""
    from opsfleet_agent.observability.retention import prune
    from opsfleet_agent.reports.deletion import purge
    from opsfleet_agent.store.db import connect
    from opsfleet_agent.tracing import init_tracing

    ui = Ui()
    config = _config(ui)
    init_tracing(config.settings.data_path("traces"))
    now = datetime.now(UTC)
    conn = connect(config.settings.data_path("app.db"))
    try:
        result = purge(conn, config.settings, now, dry_run=dry_run)
        pruned = None if dry_run else prune(conn, config.settings, now)
    finally:
        conn.close()
    if not result.batches:
        ui.line("Nothing to purge.")
    verb = "Would purge" if dry_run else "Purged"
    for batch in result.batches:
        ui.line(
            f"{verb} batch {batch['batch_id']} of {batch['owner_id']}: "
            f"{batch['count']} reports."
        )
    if result.batches and not dry_run:
        ui.line(f"Files deleted: {result.files_deleted}.")
    if pruned is not None:
        ui.line(pruned.summary())


@app.command("ui")
def ui_command(
    port: int = typer.Option(8000, "--port", help="The port on 127.0.0.1."),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Keep 127.0.0.1. Use 0.0.0.0 only inside a container (spec 52).",
    ),
) -> None:
    """Start the local test UI (spec 52) on http://127.0.0.1:8000."""
    from opsfleet_agent.ui.backend import AssistantFactory, UiBackend
    from opsfleet_agent.ui.server import serve

    ui = Ui()
    config = _config(ui)
    try:
        check_secrets(config)
        with ui.status("Starting: reading the table schemas and the value cache"):
            services = start_services(config)
    except StartError as exc:
        ui.line(exc.message, "red")
        raise typer.Exit(code=exc.exit_code) from exc
    backend = UiBackend(config, AssistantFactory(config, services))
    ui.line(f"Local test UI: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    serve(backend, host=host, port=port)


def main() -> None:
    app()
