"""Command-line interface (spec 01). Spec 37 adds the full set of commands.

Specs 32 and 33 add the minimum: the report commands (`/reports`, `/open`, `/export`).
"""

from __future__ import annotations

import shlex

import typer
from rich.console import Console
from rich.markdown import Markdown

from opsfleet_agent.config import load_config
from opsfleet_agent.errors import ConfigError
from opsfleet_agent.identity import resolve_user

app = typer.Typer(
    add_completion=False, help="Retail analytics chat assistant (prototype)."
)
console = Console()

DEV_NOTE = (
    "Development build: the long-conversation memory is not active yet. "
    "Type /reports, /open <id> or /export <id> --format html|md for your saved reports."
)
COMMANDS = ("reports", "open", "export")


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
    elif outcome.path:
        console.print(f"[green]{outcome.text}[/green]")
    else:
        console.print(Markdown(outcome.text))


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
        try:
            question = console.input("[bold cyan]> [/bold cyan]").strip()
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
        with console.status("Working…"):
            result = session.ask(question)
        console.print(Markdown(result.answer))
        t = result.totals
        console.print(
            f"[dim]{t['seconds']} s · {t['llm_calls']} model calls · {t['bq_queries']} queries · "
            f"{int(t['bytes_billed']) / 1e6:.1f} MB billed · trace {(result.trace_id or '')[:8]}[/dim]\n"
        )
    console.print("Goodbye.")


def main() -> None:
    app()
