"""The result cache: the full masked rows of each query result (spec 34, B2.3).

The history of the conversation keeps only a reference (`res_` and 8 characters) and a preview of
5 rows. The rows stay here for 30 days, so "save the last answer as a report" needs no new query.
Each read filters by the owner: a user never reads the results of another user.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from opsfleet_agent.agent.formatting import ResultData

REF_PREFIX = "res_"
REF_CHARS = "0123456789abcdefghjkmnpqrstvwxyz"
MAX_ROWS = 200


def new_ref() -> str:
    return REF_PREFIX + "".join(secrets.choice(REF_CHARS) for _ in range(8))


def put(
    conn: sqlite3.Connection,
    result: ResultData,
    *,
    owner_id: str,
    conversation_id: str,
    turn_no: int,
    sql: str,
    now: datetime,
    days: int,
) -> str:
    """Store one masked result. Returns its reference."""
    ref = new_ref()
    payload = {
        "kinds": result["kinds"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
        "local_ref": result["ref"],
    }
    with conn:
        conn.execute(
            """
            INSERT INTO result_cache (ref, conversation_id, turn_no, owner_id, purpose, sql,
                                      columns, rows, meta, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref,
                conversation_id,
                turn_no,
                owner_id,
                result["purpose"],
                sql,
                json.dumps(result["columns"]),
                json.dumps(result["rows"][:MAX_ROWS], default=str),
                json.dumps(payload),
                now.isoformat(),
                (now + timedelta(days=days)).isoformat(),
            ),
        )
    return ref


def get(
    conn: sqlite3.Connection, ref: str, *, owner_id: str, now: datetime
) -> ResultData | None:
    """The stored result, or None: unknown, of another user, or expired."""
    row = conn.execute(
        "SELECT * FROM result_cache WHERE ref = ? AND owner_id = ? AND expires_at > ?",
        (ref, owner_id, now.isoformat()),
    ).fetchone()
    if row is None:
        return None
    meta: dict[str, Any] = json.loads(row["meta"] or "{}")
    return ResultData(
        ref=str(meta.get("local_ref") or ref),
        purpose=row["purpose"],
        columns=json.loads(row["columns"]),
        kinds=list(meta.get("kinds") or []),
        rows=json.loads(row["rows"]),
        row_count=int(meta.get("row_count") or 0),
        truncated=bool(meta.get("truncated")),
    )


def expire(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete the expired results. Returns the count."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM result_cache WHERE expires_at <= ?", (now.isoformat(),)
        )
    return cursor.rowcount
