"""Command-line interface (spec 01). Spec 37 adds the full set of commands.

Specs 32 and 33 add the minimum: the report commands (`/reports`, `/open`, `/export`,
`/undo`), the delete confirmation panel with the `confirm>` prompt, and `purge`. Spec 34 adds
`chat --resume <id>` and `conversations --user <id>`. Spec 36 adds the protection against crashes
and the Ctrl-C rules.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opsfleet_agent.config import load_config
from opsfleet_agent.errors import ConfigError
from opsfleet_agent.identity import resolve_user

app = typer.Typer(
    add_completion=False, help="Retail analytics chat assistant (prototype)."
)
console = Console()

DEV_NOTE = (
    "Development build. Report commands: /reports, /open <id>, "
    "/export <id> --format html|md, /undo."
)
COMMANDS = ("reports", "open", "export", "undo")
CTRL_C_WINDOW_S = 2.0  # spec 36, B2.6 #4: a second Ctrl-C within 2 s ends the CLI
STOPPED = "Stopped."


def run_command(session, line: str) -> None:
    """A slash command: no model call and no query."""
    try:
        parts = shlex.split(line[1:])
    except ValueError:
        parts = line[1:].split()
    name, args = (parts[0].lower(), parts[1:]) if parts else ("", [])
    if name not in COMMANDS:
        console.print(f"[yellow]Unknown command '/{name}'. Type /exit to end.[/yellow]")
        return
    outcome = session.command(name, args)
    if outcome.kind == "error":
        console.print(f"[yellow]{outcome.text}[/yellow]")
    elif name == "open":
        console.print(Markdown(outcome.text))
    elif outcome.path or name == "undo":
        console.print(f"[green]{outcome.text}[/green]")
    else:
        console.print(Markdown(outcome.text))


def show_confirmation(view) -> None:
    """The red delete panel (spec 37, B2.4). The rows and the text come from the session."""
    table = Table(box=None, show_edge=False, pad_edge=False)
    for column in ("ID", "Title", "Created", "Why"):
        table.add_column(column, overflow="fold")
    for row in view.rows:
        table.add_row(
            row["report_id"], row["title"][:40], row["created"], row["reason"]
        )
    footer = Text()
    if view.not_found:
        footer.append(f"Not found: {', '.join(view.not_found)}\n")
    footer.append(
        f"To delete {'this report' if view.count == 1 else f'these {view.count} reports'}, type: "
    )
    footer.append(view.expected_text, style="bold")
    footer.append("\nAny other answer cancels. You can undo for 30 days with /undo.")
    body = Table.grid()
    body.add_row(table)
    body.add_row("")
    body.add_row(footer)
    console.print(Panel(body, title="Delete these reports?", border_style="red"))


def show_result(result) -> None:
    if result.confirmation is not None:
        show_confirmation(result.confirmation)
        return
    if result.delete_outcome == "deleted":
        console.print(f"[green]{result.answer}[/green]")
    elif (
        result.error_type == "confirmation_rejected"
        or getattr(result, "status", "ok") == "cancelled"
    ):
        console.print(f"[yellow]{result.answer}[/yellow]")
    else:
        console.print(Markdown(result.answer))
    t = result.totals
    console.print(
        f"[dim]{t['seconds']} s · {t['llm_calls']} model calls · {t['bq_queries']} queries · "
        f"{int(t['bytes_billed']) / 1e6:.1f} MB billed · trace {(result.trace_id or '')[:8]}[/dim]\n"
    )


def send(session, text: str) -> None:
    """Send one input. The answer shows as soon as it is ready; the summary of older turns runs
    after it (spec 34, B2.4). Text that is not the confirmation cancels the delete and is sent
    again as a new question (spec 33, A2.8)."""
    result = _send_once(session, text)
    if result.replay_input:
        _send_once(session, text)


def _send_once(session, text: str):
    shown: list[bool] = []
    with console.status("Working…") as status:

        def on_answer(result) -> None:
            shown.append(True)
            show_result(result)
            status.update("Updating the conversation memory…")

        result = session.send(text, on_answer=on_answer)
    if not shown:
        show_result(result)
    return result


def show_resume(view, user_id: str) -> None:
    """The last turns and the first lines of the summary (spec 34, A2.6)."""
    console.print(f"[bold]Conversation {view.conversation_id}[/bold] (resumed)")
    if view.summary:
        console.print("[dim]Earlier in this conversation:[/dim]")
        for line in view.summary:
            console.print(f"[dim]{line}[/dim]")
    for turn in view.turns:
        console.print(f"[bold cyan]{user_id}>[/bold cyan] {turn.user_text}")
        console.print(Markdown(turn.answer_text))
    for note in view.notes:
        console.print(f"[yellow]{note}[/yellow]")
    console.print()


def saved_line(conversation_id: str, user_id: str) -> str:
    return (
        f"Conversation {conversation_id} saved. "
        f"Continue it with: chat --user {user_id} --resume {conversation_id}"
    )


@app.callback()
def _root() -> None:
    """Retail analytics chat assistant (prototype)."""


def check_secrets(config) -> str | None:
    """Spec 36, B2.6 #6: a missing `.env` value stops the start and names the variable."""
    for name, value in (
        ("GOOGLE_API_KEY", config.secrets.google_api_key),
        ("GOOGLE_CLOUD_PROJECT", config.secrets.google_cloud_project),
    ):
        if not value:
            return f"{name} is not set. Add it to the .env file (see .env.example)."
    return None


def start_error(exc: BaseException) -> str:
    """A clear message for a failed start, for example BigQuery down without a schema cache."""
    from opsfleet_agent.resilience.errors_map import map_bq_error

    mapped = map_bq_error(exc)
    return f"The assistant could not start. {mapped.user_message}"


def guarded_send(session, text: str) -> None:
    """Spec 36, B2.6 #3: no exception of one turn ends the CLI."""
    from opsfleet_agent.resilience.safe_node import internal_message, log_exception

    try:
        send(session, text)
    except KeyboardInterrupt:
        cancel = getattr(getattr(session, "services", None), "bq", None)
        if cancel is not None and hasattr(cancel, "cancel_running"):
            cancel.cancel_running()
        console.print(f"[yellow]{STOPPED}[/yellow]")
    except Exception:  # noqa: BLE001 - the chat continues after any bug (spec 36)
        log_exception("cli", None)
        console.print(f"[yellow]{internal_message(None)}[/yellow]")


def chat_loop(
    session,
    user_id: str,
    read: Callable[[str], str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Read and answer until /exit, Ctrl-D, or two Ctrl-C within 2 s at the prompt.

    A Ctrl-C at the `confirm>` prompt cancels the waiting delete (spec 33). A Ctrl-C during an
    answer stops the answer and the running query (spec 36, B2.6 #4). The chat continues.
    """
    read = read or (lambda prompt: console.input(f"[bold cyan]{prompt}[/bold cyan]"))
    last_ctrl_c: float | None = None
    while True:
        prompt = "confirm> " if session.pending is not None else f"{user_id}> "
        try:
            question = read(prompt).strip()
        except EOFError:
            console.print()
            break
        except KeyboardInterrupt:
            console.print()
            now = clock()
            if session.pending is not None:
                session.cancel_pending("abandoned")
                console.print("[yellow]Delete cancelled. Nothing was deleted.[/yellow]")
                last_ctrl_c = now
                continue
            if last_ctrl_c is not None and now - last_ctrl_c <= CTRL_C_WINDOW_S:
                break
            last_ctrl_c = now
            console.print("[dim]Press Ctrl-C again to end, or type /exit.[/dim]")
            continue
        last_ctrl_c = None
        if not question:
            continue
        if question in ("/exit", "/quit"):
            break
        if question.startswith("/"):
            run_command(session, question)
            continue
        guarded_send(session, question)


@app.command()
def chat(
    user: str = typer.Option(..., "--user", help="User ID from config/users.yaml"),
    refresh_cache: bool = typer.Option(
        False,
        "--refresh-cache",
        help="Build the value cache again (one small query, about 40 MB billed).",
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="Continue a saved conversation, for example C-8M2QX (spec 34).",
    ),
) -> None:
    """Start a chat as one user. Type /exit or press Ctrl-D to end."""
    try:
        config = load_config()
        user_ctx = resolve_user(config.users, user)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    missing = check_secrets(config)
    if missing:
        console.print(f"[red]{missing}[/red]")
        raise typer.Exit(code=2)
    if resume:
        problem = check_resume(config, user_ctx.id, resume)
        if problem:
            console.print(f"[red]{problem}[/red]")
            raise typer.Exit(code=2)

    from opsfleet_agent.session import (
        AssistantSession,
        build_services,
        check_user_brands,
    )

    with console.status("Starting: reading the table schemas and the value cache…"):
        try:
            services = build_services(config, refresh_cache=refresh_cache)
        except Exception as exc:
            console.print(f"[red]{start_error(exc)}[/red]")
            raise typer.Exit(code=1) from exc
    try:
        warnings = check_user_brands(config, services.values, user_ctx.id)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    for line in warnings:
        console.print(f"[yellow]Warning: {line}[/yellow]")
    if resume:
        session = AssistantSession.resume(config, user_ctx, services, resume)
    else:
        session = AssistantSession(config, user_ctx, services)

    console.print(f"[bold]Hello, {user_ctx.name}.[/bold] {user_ctx.describe()}")
    console.print(f"[yellow]{DEV_NOTE}[/yellow]")
    if resume:
        show_resume(session.resume_view(), user_ctx.id)
    console.print("Ask a question about the sales data. Type /exit to end.\n")
    chat_loop(session, user_ctx.id)
    if session.close() is not None:
        console.print("[yellow]Delete cancelled. Nothing was deleted.[/yellow]")
    if session.turn_count() > 0:
        console.print(saved_line(session.conversation_id, user_ctx.id))
    console.print("Goodbye.")


def check_resume(config, user_id: str, conversation_id: str) -> str | None:
    """Check `--resume` before the start: the conversation of this user, at most 90 days old."""
    from opsfleet_agent.errors import AgentError
    from opsfleet_agent.memory.conversations import load_for_resume
    from opsfleet_agent.store.db import connect

    settings = config.settings
    conn = connect(settings.data_path("app.db"))
    try:
        load_for_resume(
            conn,
            conversation_id,
            user_id,
            datetime.now(UTC),
            settings.context.resume_max_days,
        )
    except AgentError as exc:
        return exc.user_message
    finally:
        conn.close()
    return None


@app.command("conversations")
def conversations_command(
    user: str = typer.Option(..., "--user", help="User ID from config/users.yaml"),
) -> None:
    """List the conversations of a user from the last 90 days (spec 34, A2.8)."""
    from opsfleet_agent.memory.conversations import list_for_user
    from opsfleet_agent.store.db import connect

    try:
        config = load_config()
        user_ctx = resolve_user(config.users, user)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    settings = config.settings
    conn = connect(settings.data_path("app.db"))
    try:
        rows = list_for_user(
            conn, user_ctx.id, datetime.now(UTC), settings.context.resume_max_days
        )
    finally:
        conn.close()
    if not rows:
        console.print(f"No conversations of {user_ctx.id} in the last 90 days.")
        return
    table = Table(box=None, show_edge=False, pad_edge=False)
    for column in ("ID", "Started", "Last active", "Turns", "First question"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(
            row.id,
            row.started_at[:16].replace("T", " "),
            row.last_active_at[:16].replace("T", " "),
            str(row.turn_count),
            row.title,
        )
    console.print(table)
    console.print(f"Continue one with: chat --user {user_ctx.id} --resume <ID>")


@app.command("purge")
def purge_command(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the batches and the counts. Change nothing."
    ),
) -> None:
    """Delete for good the reports that users deleted more than 30 days ago (spec 33)."""
    from opsfleet_agent.reports.deletion import purge
    from opsfleet_agent.store.db import connect
    from opsfleet_agent.tracing import init_tracing

    config = load_config()
    init_tracing(config.settings.data_path("traces"))
    conn = connect(config.settings.data_path("app.db"))
    try:
        result = purge(conn, config.settings, datetime.now(UTC), dry_run=dry_run)
    finally:
        conn.close()
    if not result.batches:
        console.print("Nothing to purge.")
        return
    verb = "Would purge" if dry_run else "Purged"
    for batch in result.batches:
        console.print(
            f"{verb} batch {batch['batch_id']} of {batch['owner_id']}: {batch['count']} reports."
        )
    if not dry_run:
        console.print(f"Files deleted: {result.files_deleted}.")


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
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    missing = check_secrets(config)
    if missing:
        console.print(f"[red]{missing}[/red]")
        raise typer.Exit(code=2)

    from opsfleet_agent.session import build_services
    from opsfleet_agent.ui.backend import AssistantFactory, UiBackend
    from opsfleet_agent.ui.server import serve

    with console.status("Starting: reading the table schemas and the value cache…"):
        try:
            services = build_services(config)
        except Exception as exc:
            console.print(f"[red]{start_error(exc)}[/red]")
            raise typer.Exit(code=1) from exc
    backend = UiBackend(config, AssistantFactory(config, services))
    console.print(
        f"[bold]Local test UI:[/bold] http://127.0.0.1:{port}  (Ctrl-C to stop)"
    )
    serve(backend, host=host, port=port)


def main() -> None:
    app()
