"""Retention of local data (spec 35, B2.5). `prune(now)` runs at the start of `chat` and in
`opsfleet-agent purge`.

- Files in `data/traces/` and `data/logs/` older than 30 days are deleted.
- `user_text` and `answer_text` of turns older than 90 days become NULL.
- Turn rows older than 1 year are deleted (the figures stay for 1 year).
- `result_cache` rows after their `expires_at` are deleted (spec 34).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from opsfleet_agent.config import Settings
from opsfleet_agent.store.db import retry_locked

_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_SUFFIXES = frozenset(
    {".jsonl", ".log"}
)  # `.log`: the text log of the first spec 36 build


@dataclass(frozen=True)
class PruneResult:
    trace_files: int = 0
    log_files: int = 0
    texts_cleared: int = 0
    turns_deleted: int = 0
    cache_rows: int = 0

    def summary(self) -> str:
        return (
            f"Retention: {self.trace_files} trace files, {self.log_files} log files, "
            f"{self.texts_cleared} turn texts, {self.turns_deleted} turn rows and "
            f"{self.cache_rows} cached results removed."
        )


def file_day(path: Path) -> date:
    """The date in the file name (`2026-11-12.jsonl`), else the date of the last change."""
    match = _DATED.match(path.name)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).date()


def prune_files(directory: Path, now: datetime, days: int) -> int:
    if not directory.is_dir():
        return 0
    oldest = (now - timedelta(days=days)).date()
    removed = 0
    for path in directory.iterdir():
        if path.suffix not in _SUFFIXES or not path.is_file():
            continue
        if file_day(path) < oldest:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def prune_rows(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> tuple[int, int, int]:
    keep = settings.retention
    text_limit = (now - timedelta(days=keep.turn_text_days)).isoformat()
    row_limit = (now - timedelta(days=keep.turn_metrics_days)).isoformat()

    def write() -> tuple[int, int, int]:
        with conn:
            deleted = conn.execute(
                "DELETE FROM conversation_turns WHERE started_at < ?", (row_limit,)
            ).rowcount
            cleared = conn.execute(
                "UPDATE conversation_turns SET user_text = NULL, answer_text = NULL "
                "WHERE started_at < ? AND (user_text IS NOT NULL OR answer_text IS NOT NULL)",
                (text_limit,),
            ).rowcount
            cache = conn.execute(
                "DELETE FROM result_cache WHERE expires_at < ?", (now.isoformat(),)
            ).rowcount
        return cleared, deleted, cache

    return retry_locked(write)


def prune(conn: sqlite3.Connection, settings: Settings, now: datetime) -> PruneResult:
    cleared, deleted, cache = prune_rows(conn, settings, now)
    return PruneResult(
        trace_files=prune_files(
            settings.data_path("traces"), now, settings.retention.traces_days
        ),
        log_files=prune_files(
            settings.data_path("logs"), now, settings.retention.logs_days
        ),
        texts_cleared=cleared,
        turns_deleted=deleted,
        cache_rows=cache,
    )
