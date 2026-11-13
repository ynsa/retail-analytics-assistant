"""The conversation log in `data/app.db` (spec 34, B2.5).

- `conversations`: one row for each conversation, with `last_active_at` and `turn_count`.
- `conversation_turns`: one row for each turn. The full answer text stays here, outside the
  prompt and the checkpoint.

The user text and the title are masked with the output scanner (spec 30) before they are stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.guard.scanner import OutputScanner
from opsfleet_agent.reports.ids import new_id, normalize_id
from opsfleet_agent.store.db import retry_locked

TITLE_CHARS = 60
PREFIX = "C"


@dataclass(frozen=True)
class ConversationRow:
    id: str
    user_id: str
    started_at: str
    last_active_at: str
    turn_count: int
    title: str


@dataclass(frozen=True)
class TurnRow:
    turn_no: int
    started_at: str
    user_text: str
    answer_text: str
    intent: str | None
    status: str
    error_type: str | None


class ConversationNotFound(AgentError):
    """No conversation with this ID for this user. A conversation of another user gives the same
    message, so a user cannot learn about the conversations of others (A2.7)."""

    def __init__(self, conversation_id: str, user_id: str) -> None:
        shown = conversation_id.strip()[:40] or "?"
        super().__init__(
            ErrorType.TOOL_ERROR,
            f"Conversation {shown} not found for user {user_id}.",
            "conversation not found",
        )


class ConversationTooOld(AgentError):
    def __init__(self, conversation_id: str, days: int) -> None:
        super().__init__(
            ErrorType.TOOL_ERROR,
            f"Conversation {conversation_id} is older than {days} days and cannot be resumed.",
            "conversation too old",
        )


def new_conversation_id() -> str:
    """`C-8M2QX`: five characters of Crockford base32, like the report IDs."""
    return new_id(PREFIX)


def canonical_id(text: str) -> str:
    """`c-8m2qx` and `C8M2QX` give `C-8M2QX`. Other IDs (older hex IDs) stay as they are."""
    return normalize_id(text, PREFIX) or text.strip()


def masked(scanner: OutputScanner, text: str) -> str:
    return scanner.scan(text, target="conversation").text


def create(
    conn: sqlite3.Connection, conversation_id: str, user_id: str, now: datetime
) -> None:
    def write() -> None:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations "
                "(id, user_id, started_at, last_active_at, turn_count) VALUES (?, ?, ?, ?, 0)",
                (conversation_id, user_id, now.isoformat(), now.isoformat()),
            )

    retry_locked(write)


def record_turn(
    conn: sqlite3.Connection,
    scanner: OutputScanner,
    *,
    conversation_id: str,
    turn_no: int,
    trace_id: str | None,
    started_at: datetime,
    user_text: str,
    answer_text: str,
    intent: str | None,
    status: str,
    error_type: str | None,
    user_id: str | None = None,
) -> None:
    """Write the turn row and update the conversation. A second write of the same turn (the
    typed answer of a delete confirmation) keeps the question and replaces the answer. Spec 35
    adds the figures of the turn (`observability.turn_metrics`)."""
    question = masked(scanner, user_text)

    def write() -> None:
        with conn:
            conn.execute(
                """
                INSERT INTO conversation_turns (conversation_id, turn_no, trace_id, started_at,
                    user_text, answer_text, intent, status, error_type, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id, turn_no) DO UPDATE SET
                    trace_id = excluded.trace_id,
                    answer_text = excluded.answer_text,
                    intent = COALESCE(conversation_turns.intent, excluded.intent),
                    status = excluded.status,
                    error_type = excluded.error_type
                """,
                (
                    conversation_id,
                    turn_no,
                    trace_id,
                    started_at.isoformat(),
                    question,
                    answer_text,
                    intent,
                    status,
                    error_type,
                    user_id,
                ),
            )
            conn.execute(
                """
                UPDATE conversations SET
                    last_active_at = ?,
                    turn_count = (SELECT COUNT(*) FROM conversation_turns
                                  WHERE conversation_id = ?),
                    title = COALESCE(title, ?)
                WHERE id = ?
                """,
                (
                    started_at.isoformat(),
                    conversation_id,
                    question[:TITLE_CHARS] or None,
                    conversation_id,
                ),
            )

    retry_locked(write)


def _row(row: sqlite3.Row) -> ConversationRow:
    return ConversationRow(
        id=row["id"],
        user_id=row["user_id"],
        started_at=row["started_at"],
        last_active_at=row["last_active_at"] or row["started_at"],
        turn_count=int(row["turn_count"] or 0),
        title=row["title"] or "",
    )


def list_for_user(
    conn: sqlite3.Connection, user_id: str, now: datetime, days: int = 90
) -> list[ConversationRow]:
    """The conversations of the user with at least one turn in the last `days` days, newest
    first."""
    since = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM conversations WHERE user_id = ? AND turn_count > 0 "
        "AND COALESCE(last_active_at, started_at) >= ? "
        "ORDER BY COALESCE(last_active_at, started_at) DESC",
        (user_id, since),
    ).fetchall()
    return [_row(r) for r in rows]


def load_for_resume(
    conn: sqlite3.Connection,
    conversation_id: str,
    user_id: str,
    now: datetime,
    days: int = 90,
) -> ConversationRow:
    """The conversation for `--resume`. Raises `ConversationNotFound` (unknown ID or another
    user) or `ConversationTooOld`."""
    wanted = canonical_id(conversation_id)
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (wanted, user_id)
    ).fetchone()
    if row is None:
        raise ConversationNotFound(conversation_id, user_id)
    found = _row(row)
    last = datetime.fromisoformat(found.last_active_at)
    if now - last > timedelta(days=days):
        raise ConversationTooOld(found.id, days)
    return found


def last_turns(
    conn: sqlite3.Connection, conversation_id: str, count: int = 3
) -> list[TurnRow]:
    rows = conn.execute(
        "SELECT * FROM conversation_turns WHERE conversation_id = ? "
        "ORDER BY turn_no DESC LIMIT ?",
        (conversation_id, count),
    ).fetchall()
    return [
        TurnRow(
            turn_no=r["turn_no"],
            started_at=r["started_at"],
            user_text=r["user_text"] or "",  # NULL after 90 days (spec 35)
            answer_text=r["answer_text"] or "",
            intent=r["intent"],
            status=r["status"],
            error_type=r["error_type"],
        )
        for r in reversed(rows)
    ]
