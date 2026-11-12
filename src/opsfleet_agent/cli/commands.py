"""Slash command parsing (shlex) and dispatch to the session (spec 37, B3)."""

from __future__ import annotations

import shlex
from typing import Any

from opsfleet_agent.cli.render import Ui


def parse(line: str) -> tuple[str, list[str]]:
    """`/export R-7K2PQ --format md` gives `("export", ["R-7K2PQ", "--format", "md"])`."""
    body = line.strip()[1:]
    try:
        parts = shlex.split(body)
    except ValueError:  # an open quote
        parts = body.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def run_command(ui: Ui, session: Any, line: str) -> None:
    """Run one slash command. It makes no model call and no query."""
    name, args = parse(line)
    outcome = session.command(name, args)
    ui.outcome(name, outcome)
