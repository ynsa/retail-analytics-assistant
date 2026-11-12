"""Audit events in `data/app.db` (spec 00, B3, and spec 33, B2.5).

Each event of the delete flow also writes one span of kind `confirm` with the outcome and the
reason. The span gets no titles and no free text, only IDs, counts and codes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from opsfleet_agent.tracing import span


def write_audit(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str | None,
    action: str,
    details: dict[str, Any],
    at: datetime | None = None,
    commit: bool = True,
) -> None:
    """Insert one audit event. With `commit=False`, the caller's transaction includes it."""
    when = (at or datetime.now(UTC)).isoformat()
    conn.execute(
        "INSERT INTO audit_events (at, user_id, conversation_id, action, details) "
        "VALUES (?, ?, ?, ?, ?)",
        (when, user_id, conversation_id, action, json.dumps(details, default=str)),
    )
    if commit:
        conn.commit()


def confirm_span(
    action: str, outcome: str, reason: str | None = None, **attrs: Any
) -> None:
    """One `confirm` span for a step of the delete flow (spec 33, B2.5)."""
    with span(f"confirm.{action}", "confirm", outcome=outcome, reason=reason, **attrs):
        pass


def audit_events(
    conn: sqlite3.Connection, user_id: str | None = None, action: str | None = None
) -> list[dict[str, Any]]:
    """The audit events, oldest first (for the tests and the statistics of spec 35)."""
    sql = "SELECT at, user_id, conversation_id, action, details FROM audit_events"
    where, params = [], []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if action is not None:
        where.append("action = ?")
        params.append(action)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [
        {
            "at": r[0],
            "user_id": r[1],
            "conversation_id": r[2],
            "action": r[3],
            "details": json.loads(r[4]),
        }
        for r in rows
    ]
