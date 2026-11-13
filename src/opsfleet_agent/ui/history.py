"""Reads of `data/app.db` for the history pane (spec 40, A2.2). Read only.

`memory.conversations.last_turns()` gives the turns without the trace ID and without the figures.
The page needs the trace ID of each answer for the steps pane, and the figures of spec 35 for the
cost line, so this module reads the same table.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def conversation_turns(
    conn: sqlite3.Connection, conversation_id: str
) -> list[dict[str, Any]]:
    """The turns in order. `totals` has the same keys as `TurnResult.totals`. A row from before
    spec 35 has no figures: its `totals` is empty."""
    rows = conn.execute(
        "SELECT turn_no, trace_id, started_at, user_text, answer_text, intent, status, "
        "error_type, guard_block_reason, duration_ms, llm_calls, tokens_in, tokens_out, "
        "bq_queries, bq_bytes_billed FROM conversation_turns WHERE conversation_id = ? "
        "ORDER BY turn_no",
        (conversation_id,),
    ).fetchall()
    out = []
    for r in rows:
        turn = dict(r)
        seconds = round(float(turn.pop("duration_ms") or 0) / 1000, 2)
        figures = {
            "llm_calls": turn.pop("llm_calls"),
            "tokens_in": turn.pop("tokens_in"),
            "tokens_out": turn.pop("tokens_out"),
            "bq_queries": turn.pop("bq_queries"),
            "bytes_billed": turn.pop("bq_bytes_billed"),
            "seconds": seconds,
        }
        turn["totals"] = figures if seconds else {}
        out.append(turn)
    return out


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
