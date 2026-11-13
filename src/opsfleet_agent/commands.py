"""The slash commands of a chat (spec 37, A2.4). They make no model call and no query.

`run_command(session, name, args)` returns a `CommandOutcome`. The CLI and the local UI only
show it: `text` is ready to show as Markdown, and `data` has the structured view of `/stats`
(`Stats`) and `/trace` (`TraceTree`) for a richer display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from opsfleet_agent.errors import AgentError
from opsfleet_agent.observability.stats import compute_stats, stats_text
from opsfleet_agent.observability.trace_view import load_trace, trace_text
from opsfleet_agent.reports.files import display_path
from opsfleet_agent.reports.flow import list_text

if TYPE_CHECKING:
    from opsfleet_agent.session import AssistantSession


@dataclass(frozen=True)
class CommandOutcome:
    """The result of a slash command (spec 37, B3). Commands make no model call and no query."""

    kind: Literal["message", "error"]
    text: str
    path: str | None = None  # a written file, for /export
    title: str | None = None  # the report title, for /open
    data: Any = None  # Stats for /stats, TraceTree for /trace


# The commands, their usage lines and their help texts (A2.4).
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("/help", "The list of commands and example questions"),
    "reports": ("/reports", "Your saved reports"),
    "open": ("/open <id>", "Show a report"),
    "export": ("/export <id> --format html|md", "Write a report file"),
    "undo": ("/undo [batch]", "Restore the last deleted reports"),
    "trace": ("/trace [trace]", "The steps of the last turn, or of a trace ID"),
    "stats": ("/stats [--days N]", "Your statistics (default 7 days)"),
    "exit": ("/exit", "End the chat"),
}
# Commands that keep a waiting delete. `/undo` and `/exit` cancel it (spec 37, B2.4).
KEEP_PENDING = frozenset({"help", "reports", "open", "export", "trace", "stats"})
EXAMPLES = (
    "What was the revenue of my brands last month?",
    "Which 5 product categories had the most returns in Q3 2026?",
    "Create a report for Q1 2026 with action items for Q2, as an HTML file.",
)
NOT_FOUND = "Trace not found."


def usage(name: str) -> str:
    return f"Usage: {COMMANDS[name][0]}"


def unknown(name: str) -> CommandOutcome:
    return CommandOutcome("error", f"Unknown command '/{name}'. Type /help.")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")  # a pipe inside a Markdown table cell


def help_text() -> str:
    rows = [f"| `{_cell(line)}` | {text} |" for line, text in COMMANDS.values()]
    examples = "\n".join(f"- {q}" for q in EXAMPLES)
    return (
        "| Command | What it does |\n|---|---|\n"
        + "\n".join(rows)
        + f"\n\n**Example questions:**\n\n{examples}"
    )


def run_command(
    session: AssistantSession, name: str, args: list[str]
) -> CommandOutcome:
    name = name.lstrip("/").lower()
    handler = _HANDLERS.get(name)
    if handler is None:
        return unknown(name)
    try:
        return handler(session, args)
    except AgentError as exc:  # also ReportNotFound: a user error, not a failure
        return CommandOutcome("error", exc.user_message)


def _help(session: AssistantSession, args: list[str]) -> CommandOutcome:
    return CommandOutcome("message", help_text())


def _reports(session: AssistantSession, args: list[str]) -> CommandOutcome:
    if args:
        return CommandOutcome("error", usage("reports"))
    listing = session.library.list(session.user)
    return CommandOutcome(
        "message", list_text(session.services.prompts, listing), data=listing
    )


def _open(session: AssistantSession, args: list[str]) -> CommandOutcome:
    if len(args) != 1:
        return CommandOutcome("error", usage("open"))
    report = session.library.get(session.user, args[0])
    return CommandOutcome(
        "message", report.body_markdown, title=f"{report.report_id} · {report.title}"
    )


def _export(session: AssistantSession, args: list[str]) -> CommandOutcome:
    fmt = export_format(args)
    if fmt is None:
        return CommandOutcome("error", usage("export"))
    path = display_path(session.library.export(session.user, args[0], fmt))
    return CommandOutcome("message", f"Saved: {path}", path=path)


def _undo(session: AssistantSession, args: list[str]) -> CommandOutcome:
    if len(args) > 1:
        return CommandOutcome("error", usage("undo"))
    return session.undo(args[0] if args else None)


def _trace(session: AssistantSession, args: list[str]) -> CommandOutcome:
    if len(args) > 1:
        return CommandOutcome("error", usage("trace"))
    trace_id = args[0] if args else session.last_trace()
    if not trace_id:
        return CommandOutcome(
            "error", "No turn in this conversation yet. Use /trace <trace ID>."
        )
    trace_dir = session.config.settings.data_path("traces")
    tree = load_trace(trace_dir, trace_id, session.user.id)
    if tree is None:
        return CommandOutcome("error", NOT_FOUND)
    return CommandOutcome("message", trace_text(tree), data=tree)


def _stats(session: AssistantSession, args: list[str]) -> CommandOutcome:
    settings = session.config.settings.stats
    days = stats_days(args, settings.default_days, settings.max_days)
    if days is None:
        return CommandOutcome(
            "error", f"{usage('stats')} (N from 1 to {settings.max_days})"
        )
    stats = compute_stats(session.conn, session.services.clock(), days, session.user.id)
    return CommandOutcome("message", stats_text(stats), data=stats)


_HANDLERS = {
    "help": _help,
    "reports": _reports,
    "open": _open,
    "export": _export,
    "undo": _undo,
    "trace": _trace,
    "stats": _stats,
}


def export_format(args: list[str]) -> str | None:
    """`<id> --format html|md` (the format is html when it is not given)."""
    if not args:
        return None
    rest = args[1:]
    if not rest:
        return "html"
    if len(rest) == 2 and rest[0] == "--format" and rest[1] in ("html", "md"):
        return rest[1]
    return None


def stats_days(args: list[str], default: int, maximum: int) -> int | None:
    """`[--days N]` with 1 <= N <= maximum. None for wrong arguments."""
    if not args:
        return default
    if len(args) == 2 and args[0] == "--days" and args[1].isdigit():
        days = int(args[1])
        return days if 1 <= days <= maximum else None
    return None
