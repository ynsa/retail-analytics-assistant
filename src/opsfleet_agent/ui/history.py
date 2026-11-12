"""Reads of `data/app.db` for the history pane (spec 40, A2.2). Read only.

`memory.conversations.last_turns()` gives the turns without the trace ID. The page needs the
trace ID of each answer for the steps pane, so this module reads the same table.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def conversation_turns(
    conn: sqlite3.Connection, conversation_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT turn_no, trace_id, started_at, user_text, answer_text, intent, status, "
        "error_type FROM conversation_turns WHERE conversation_id = ? ORDER BY turn_no",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def report_of_trace(
    conn: sqlite3.Connection, owner_id: str, trace_id: str | None
) -> str | None:
    """The active report that a turn saved, for the report card of an earlier answer."""
    if not trace_id:
        return None
    row = conn.execute(
        "SELECT report_id FROM reports WHERE trace_id = ? AND owner_id = ? "
        "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (trace_id, owner_id),
    ).fetchone()
    return row["report_id"] if row else None
