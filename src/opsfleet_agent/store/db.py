"""SQLite database `data/app.db` with simple numbered migrations (spec 00, B3).

Later specs add tables by appending migrations to `MIGRATIONS`. A migration never changes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: spec 00
    """
    CREATE TABLE usage_daily (
        day TEXT PRIMARY KEY,               -- UTC date, YYYY-MM-DD
        llm_calls INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        bq_queries INTEGER NOT NULL DEFAULT 0,
        bq_bytes_billed INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        title TEXT
    );
    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        user_id TEXT NOT NULL,
        conversation_id TEXT,
        action TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}'  -- JSON
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    """Open the database and apply the migrations that are not applied yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {number}")
