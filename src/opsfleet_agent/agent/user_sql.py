"""SQL text in the user's message goes through the guard before the route (spec 50 round 1).

A message such as "Run this SQL: SELECT 1; DELETE FROM `…orders` WHERE TRUE" must be stopped by
the guard of spec 30, not only by the opinion of the route model. When the message contains a
statement that changes data, code checks the SQL text with the guard. A policy block ends the turn
with the guard message, before any model call. Nothing runs: the guard only reads the text.

Any other result (a parse error of normal words, or a pass) changes nothing: the turn goes to the
route as before.
"""

from __future__ import annotations

import re

from opsfleet_agent.guard.sql_guard import GuardOutcome, GuardResult, SqlGuard

# A statement that changes data or the schema. The words must have the SQL form, so a normal
# request ("Delete all the reports of this conversation") does not match.
_WRITE = re.compile(
    r"\b(?:delete\s+from|drop\s+(?:table|view|schema|database)|insert\s+into"
    r"|update\s+\S+\s+set|truncate\s+table|merge\s+into"
    r"|create\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?(?:table|view)"
    r"|alter\s+table|export\s+data|grant\s+\w+)\b",
    re.IGNORECASE,
)
_START = re.compile(
    r"\b(?:select|with|delete|drop|insert|update|truncate|merge|create|alter|export|grant)\b",
    re.IGNORECASE,
)
MAX_STARTS = 3  # the guard checks at most 3 start points of the SQL text


def sql_candidates(text: str) -> list[str]:
    """The SQL texts to check: from each SQL keyword before the write statement to the end."""
    write = _WRITE.search(text)
    if write is None:
        return []
    starts = [m.start() for m in _START.finditer(text) if m.start() <= write.start()]
    out: list[str] = []
    for start in starts[:MAX_STARTS]:
        sql = text[start:].strip().rstrip(".").strip()
        if sql and sql not in out:
            out.append(sql)
    return out


def blocked_user_sql(guard: SqlGuard, text: str) -> GuardResult | None:
    """The guard's policy block for SQL text in the message, or None."""
    for sql in sql_candidates(text):
        result = guard.check(sql, purpose="user_sql")  # writes its own `guard` span
        if result.outcome is GuardOutcome.POLICY_BLOCK:
            return result
    return None
