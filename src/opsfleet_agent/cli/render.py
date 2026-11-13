"""Rich output of the chat (spec 37, B2.3 and B2.4). Display only: no rule of the assistant.

Model and user text is never read as Rich markup: the code prints `Text` objects or `Markdown`.
In plain mode (stdin or stdout is not a terminal), there are no colors and no status line.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from opsfleet_agent.cli.status import OTHER, STATUS_TEXTS, status_text
from opsfleet_agent.display import describe, footer_line
from opsfleet_agent.observability.progress import ProgressEvent
from opsfleet_agent.observability.stats import Stats, stats_title
from opsfleet_agent.observability.trace_view import TraceNode, TraceTree

STYLES = {"default": "", "yellow": "yellow", "green": "green", "red": "red"}
HINT = "Type a question, or /help."


def is_plain() -> bool:
    return not (sys.stdin.isatty() and sys.stdout.isatty())


class _Elapsed:
    """A status text with the seconds since the start. Rich renders it at each refresh."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.started = time.monotonic()

    def __rich__(self) -> Text:
        return Text(f"{self.text} … {int(time.monotonic() - self.started)} s")


class Status:
    """The status line of a running turn. In plain mode it shows nothing."""

    def __init__(self, live: Any | None, max_attempts: int, text: str = OTHER) -> None:
        self._live = live
        self._label = _Elapsed(text)
        self._max_attempts = max_attempts
        if live is not None:
            live.update(self._label)

    def progress(self, event: ProgressEvent) -> None:
        if event.node not in STATUS_TEXTS:
            return  # an internal step keeps the current text
        self.set(status_text(event, self._max_attempts))

    def set(self, text: str) -> None:
        self._label.text = text
        if self._live is not None:
            self._live.update(self._label)


class Ui:
    def __init__(
        self,
        console: Console | None = None,
        *,
        plain: bool | None = None,
        show_footer: bool = True,
        status_line: bool = True,
        max_attempts: int = 3,
    ) -> None:
        self.plain = is_plain() if plain is None else plain
        self.console = console or (
            Console(color_system=None, highlight=False)
            if self.plain
            else Console(highlight=False)
        )
        self.show_footer = show_footer
        self.status_line = status_line and not self.plain
        self.max_attempts = max_attempts

    # --- basic lines -------------------------------------------------------------------------
    def line(self, text: str, style: str = "default") -> None:
        """One message line. The terminal wraps it: a command in it stays one line to copy."""
        self.console.print(Text(text, style=STYLES.get(style, style)), soft_wrap=True)

    def markdown(self, text: str) -> None:
        self.console.print(Markdown(text))

    def blank(self) -> None:
        self.console.print()

    @contextmanager
    def status(self, text: str = OTHER) -> Iterator[Status]:
        if not self.status_line:
            yield Status(None, self.max_attempts, text)
            return
        with self.console.status(text) as live:
            yield Status(live, self.max_attempts, text)

    # --- start and end -----------------------------------------------------------------------
    def greeting(self, greeting: Any, dev_line: str | None) -> None:
        brands = ", ".join(greeting.brands) if greeting.brands else "all brands"
        data = (
            f"{greeting.data_from:%-d %b %Y} – {greeting.data_to:%-d %b %Y}"
            if greeting.data_from
            else f"until {greeting.data_to:%-d %b %Y}"
        )
        body = Text()
        body.append(f"Hello, {greeting.name}.\n", style="bold")
        body.append(f"Role: {greeting.role} · Brands: {brands}\n")
        body.append(f"Data: {data}\n")
        resumed = " (resumed)" if greeting.resumed else ""
        body.append(f"Conversation: {greeting.conversation_id}{resumed}\n")
        body.append(HINT)
        self.console.print(
            Panel(body, title="Retail analytics assistant", border_style="cyan")
        )
        if dev_line:
            self.line(dev_line, "yellow")

    def resume(self, view: Any, user_id: str) -> None:
        """The last turns and the first lines of the summary (spec 34, A2.6)."""
        if view.summary:
            self.line("Earlier in this conversation:", "dim")
            for text in view.summary:
                self.line(text, "dim")
            self.blank()
        for turn in view.turns:
            self.console.print(
                Text(f"{user_id}> ", style="bold cyan") + Text(turn.user_text)
            )
            self.markdown(turn.answer_text)
            self.blank()
        for note in view.notes:
            self.line(note, "yellow")

    # --- turns --------------------------------------------------------------------------------
    def result(self, result: Any) -> None:
        shown = describe(result)
        if shown.kind == "confirmation":
            self.confirmation(result.confirmation)
            return
        if shown.markdown:
            self.markdown(shown.text)
        else:
            self.line(shown.text, shown.style)
        if shown.footer and self.show_footer:
            self.line(footer_line(result), "dim")
        self.blank()

    def confirmation(self, view: Any) -> None:
        """The red delete panel (B2.4). The rows and the text come from the session."""
        table = Table(box=None, show_edge=False, pad_edge=False)
        for column in ("ID", "Title", "Created", "Why"):
            table.add_column(column, overflow="fold")
        for row in view.rows:
            table.add_row(
                str(row["report_id"]),
                str(row["title"])[:40],
                str(row["created"]),
                str(row["reason"]),
            )
        footer = Text()
        if view.not_found:
            footer.append(f"Not found: {', '.join(view.not_found)}\n")
        what = "this report" if view.count == 1 else f"these {view.count} reports"
        footer.append(f"To delete {what}, type: ")
        footer.append(view.expected_text, style="bold")
        footer.append(
            "\nAny other answer cancels. You can undo for 30 days with /undo."
        )
        body = Table.grid()
        body.add_row(table)
        body.add_row("")
        body.add_row(footer)
        self.console.print(
            Panel(body, title="Delete these reports?", border_style="red")
        )

    # --- commands ----------------------------------------------------------------------------
    def outcome(self, name: str, outcome: Any) -> None:
        if outcome.kind == "error":
            self.line(outcome.text, "yellow")
        elif name == "trace" and isinstance(outcome.data, TraceTree):
            self.trace(outcome.data)
        elif name == "stats" and isinstance(outcome.data, Stats):
            self.stats(outcome.data)
        elif name == "open":
            self.console.print(
                Panel(Markdown(outcome.text), title=outcome.title, border_style="cyan")
            )
        elif outcome.path:
            self.saved(outcome.text, outcome.path)
        elif name == "undo":
            self.line(outcome.text, "green")
        else:
            self.markdown(outcome.text)
        self.blank()

    def saved(self, text: str, path: str) -> None:
        line = Text(text, style="green")
        if not self.plain:
            line.stylize(f"link {Path(path).resolve().as_uri()}", len("Saved: "))
        self.console.print(line)

    def trace(self, tree: TraceTree) -> None:
        root = Tree(
            Text(f"Trace {tree.trace_id[:8]} ({tree.span_count} steps)", "bold")
        )

        def add(parent: Tree, node: TraceNode) -> None:
            style = "red" if node.status == "error" else ""
            branch = parent.add(Text(node.label(), style=style))
            for detail in node.details():
                branch.add(Text(detail, style="dim"))
            for child in node.children:
                add(branch, child)

        add(root, tree.root)
        self.console.print(root)

    def stats(self, stats: Stats) -> None:
        self.line(stats_title(stats), "bold")
        table = Table(box=box.SIMPLE, show_header=False, expand=False, pad_edge=False)
        table.add_column("Figure", style="bold", no_wrap=True)
        table.add_column("Value", overflow="fold")
        for group, rows in stats.groups():
            table.add_row(Text(group, style="cyan"), "", end_section=False)
            for label, value in rows:
                table.add_row(f"  {label}", value)
        self.console.print(table)

    def table(self, columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        table = Table(box=None, show_edge=False, pad_edge=False)
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*row)
        self.console.print(table)
