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
    # 2: spec 32, the report library. `reports_fts` keeps its own copy of the text (see the
    # implementation notes of spec 32), and the triggers keep it in step with `reports`.
    """
    CREATE TABLE reports (
        report_id TEXT PRIMARY KEY,         -- R-7K2PQ
        owner_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        title TEXT NOT NULL,
        question TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,           -- UTC, ISO 8601
        period_start TEXT,
        period_end TEXT,
        scope_brands TEXT NOT NULL DEFAULT '[]',   -- JSON
        sql TEXT NOT NULL DEFAULT '[]',            -- JSON: id, purpose, sql, job_id, bytes_billed
        body_markdown TEXT NOT NULL,
        sections TEXT NOT NULL DEFAULT '{}',       -- JSON
        charts TEXT NOT NULL DEFAULT '[]',         -- JSON: specs with their data
        definitions TEXT NOT NULL DEFAULT '[]',    -- JSON: glossary keys
        entities TEXT NOT NULL DEFAULT '{}',       -- JSON: kind -> values
        calculated_figures INTEGER NOT NULL DEFAULT 0,
        unverified_figures INTEGER NOT NULL DEFAULT 0,
        file_html TEXT,
        file_md TEXT,
        model TEXT,
        prompt_version TEXT,
        trace_id TEXT,
        deleted_at TEXT,
        delete_batch_id TEXT
    );
    CREATE INDEX reports_owner ON reports (owner_id, deleted_at, created_at);
    CREATE INDEX reports_conversation ON reports (conversation_id);
    CREATE TABLE report_entities (
        report_id TEXT NOT NULL REFERENCES reports (report_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                 -- brand, category, department, product, state, country
        value TEXT NOT NULL,
        value_norm TEXT NOT NULL,           -- lower case, trimmed, single spaces
        PRIMARY KEY (report_id, kind, value_norm)
    );
    CREATE INDEX report_entities_norm ON report_entities (value_norm);
    CREATE VIRTUAL TABLE reports_fts USING fts5 (
        report_id UNINDEXED, title, body_markdown, tokenize = 'unicode61'
    );
    CREATE TRIGGER reports_fts_insert AFTER INSERT ON reports BEGIN
        INSERT INTO reports_fts (report_id, title, body_markdown)
        VALUES (new.report_id, new.title, new.body_markdown);
    END;
    CREATE TRIGGER reports_fts_delete AFTER DELETE ON reports BEGIN
        DELETE FROM reports_fts WHERE report_id = old.report_id;
    END;
    CREATE TRIGGER reports_fts_update AFTER UPDATE OF title, body_markdown ON reports BEGIN
        DELETE FROM reports_fts WHERE report_id = old.report_id;
        INSERT INTO reports_fts (report_id, title, body_markdown)
        VALUES (new.report_id, new.title, new.body_markdown);
    END;
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
