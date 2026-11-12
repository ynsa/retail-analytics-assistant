"""Command-line interface (spec 01). Spec 37 adds the full set of commands.

Specs 32 and 33 add the minimum: the report commands (`/reports`, `/open`, `/export`,
`/undo`), the delete confirmation panel with the `confirm>` prompt, and `purge`.
"""

from __future__ import annotations

import shlex
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
    "Development build: the long-conversation memory is not active yet. "
    "Report commands: /reports, /open <id>, /export <id> --format html|md, /undo."
)
COMMANDS = ("reports", "open", "export", "undo")


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
    elif result.error_type == "confirmation_rejected":
        console.print(f"[yellow]{result.answer}[/yellow]")
    else:
        console.print(Markdown(result.answer))
    t = result.totals
    console.print(
        f"[dim]{t['seconds']} s · {t['llm_calls']} model calls · {t['bq_queries']} queries · "
        f"{int(t['bytes_billed']) / 1e6:.1f} MB billed · trace {(result.trace_id or '')[:8]}[/dim]\n"
    )


def send(session, text: str) -> None:
    """Send one input. Text that is not the confirmation cancels the delete and is sent again
    as a new question (spec 33, A2.8)."""
    with console.status("Working…"):
        result = session.send(text)
    show_result(result)
    if result.replay_input:
        with console.status("Working…"):
            result = session.send(text)
        show_result(result)


@app.callback()
def _root() -> None:
    """Retail analytics chat assistant (prototype)."""


@app.command()
def chat(
    user: str = typer.Option(..., "--user", help="User ID from config/users.yaml"),
    refresh_cache: bool = typer.Option(
        False,
        "--refresh-cache",
        help="Build the value cache again (one small query, about 40 MB billed).",
    ),
) -> None:
    """Start a chat as one user. Type /exit or press Ctrl-D to end."""
    try:
        config = load_config()
        user_ctx = resolve_user(config.users, user)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    from opsfleet_agent.session import (
        AssistantSession,
        build_services,
        check_user_brands,
    )

    with console.status("Starting: reading the table schemas and the value cache…"):
        try:
            services = build_services(config, refresh_cache=refresh_cache)
        except Exception as exc:
            console.print(
                f"[red]The assistant could not start: {type(exc).__name__}: {exc}[/red]"
            )
            raise typer.Exit(code=1) from exc
    try:
        warnings = check_user_brands(config, services.values, user_ctx.id)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    for line in warnings:
        console.print(f"[yellow]Warning: {line}[/yellow]")
    session = AssistantSession(config, user_ctx, services)

    console.print(f"[bold]Hello, {user_ctx.name}.[/bold] {user_ctx.describe()}")
    console.print(f"[yellow]{DEV_NOTE}[/yellow]")
    console.print("Ask a question about the sales data. Type /exit to end.\n")
    while True:
        prompt = "confirm> " if session.pending is not None else f"{user_ctx.id}> "
        try:
            question = console.input(f"[bold cyan]{prompt}[/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not question:
            continue
        if question in ("/exit", "/quit"):
            break
        if question.startswith("/"):
            run_command(session, question)
            continue
        send(session, question)
    if session.close() is not None:
        console.print("[yellow]Delete cancelled. Nothing was deleted.[/yellow]")
    console.print("Goodbye.")


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


def main() -> None:
    app()
