"""Fixtures of the safety set (spec 22, B2.2).

- `inject_row_text` puts an attack text into a query result (instructions inside data).
- `seed_reports` and `seed_reports_injection` fill the report library of a case.

Each case works on its own copy of `data/app.db`. A fixture never changes the real library.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

import yaml

from opsfleet_agent.config import PROJECT_ROOT
from opsfleet_agent.store.db import connect

SEED_FILE = PROJECT_ROOT / "evals" / "fixtures" / "reports_seed.yaml"
SEED_FIXTURES = ("seed_reports", "seed_reports_injection")
JSON_COLUMNS = {"scope_brands", "sql", "sections", "charts", "definitions", "entities"}


class CapabilityMissing(Exception):
    """The agent does not have the part that a fixture or a check needs (for example spec 32)."""


# --- inject_row_text --------------------------------------------------------------------------
def inject_row_text(
    columns: list[str], rows: list[list[Any]], text: str
) -> list[list[Any]]:
    """Return a copy of the rows with the first text value of the first row replaced by `text`."""
    new_rows = copy.deepcopy(rows)
    if not new_rows:
        return new_rows
    first = new_rows[0]
    for index, value in enumerate(first):
        if isinstance(value, str):
            first[index] = text
            break
    return new_rows


# --- seed data --------------------------------------------------------------------------------
def load_seed(name: str, path: Path = SEED_FILE) -> list[dict[str, Any]]:
    """The reports of one seed fixture. `extends` and `changes` build a variant of another one."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if name not in data:
        raise KeyError(f"Unknown seed fixture {name!r} in {path}")
    spec = data[name]
    if isinstance(spec, list):
        return copy.deepcopy(spec)
    reports = load_seed(spec["extends"], path)
    changes = spec.get("changes") or {}
    for report in reports:
        report.update(changes.get(report["id"], {}))
    return reports


class ReportLibrary(Protocol):
    def seed(
        self, user_id: str, conversation_id: str, reports: list[dict[str, Any]]
    ) -> dict[str, list[str]]: ...

    def state(self, user_id: str | None = None) -> dict[str, str | None]: ...

    def delete_events(self) -> int: ...


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row | tuple]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _filler(col_type: str) -> Any:
    upper = (col_type or "").upper()
    if "INT" in upper:
        return 0
    if "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
        return 0.0
    return ""


class SqliteReportLibrary:
    """The report library of spec 32 in a SQLite database (a copy of `data/app.db`)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _require(self) -> None:
        if not _has_table(self._conn, "reports"):
            raise CapabilityMissing(
                "The report library (spec 32, table `reports`) does not exist yet."
            )

    def _id_column(self) -> str:
        cols = _table_columns(self._conn, "reports")
        names = [c[1] for c in cols]
        if "report_id" in names:
            return "report_id"
        pks = [c[1] for c in cols if c[5]]
        return pks[0] if pks else "id"

    def _row_values(
        self, report: dict[str, Any], owner: str, conversation_id: str
    ) -> dict[str, Any]:
        brands = (report.get("entities") or {}).get("brand", [])
        known: dict[str, Any] = {
            "report_id": report["id"],
            "owner_id": owner,
            "conversation_id": conversation_id,
            "title": report.get("title", ""),
            "question": report.get("question", ""),
            "created_at": report.get("created_at", "2026-11-11T00:00:00Z"),
            "period_start": report.get("period_start"),
            "period_end": report.get("period_end"),
            "scope_brands": brands,
            "sql": [],
            "body_markdown": report.get("body_markdown", ""),
            "sections": {},
            "charts": [],
            "definitions": [],
            "entities": report.get("entities") or {},
            "calculated_figures": 0,
            "model": "seed",
            "prompt_version": "seed",
            "trace_id": "seed",
            "deleted_at": None,
            "delete_batch_id": None,
        }
        values: dict[str, Any] = {}
        for _cid, name, col_type, notnull, default, _pk in _table_columns(
            self._conn, "reports"
        ):
            if name in known:
                value = known[name]
                if name in JSON_COLUMNS:
                    value = json.dumps(value)
                if value is None and notnull and default is None:
                    value = _filler(col_type)
                values[name] = value
            elif name == "id":
                values[name] = report["id"]
            elif notnull and default is None:
                values[name] = _filler(col_type)
        return values

    def _insert_entities(self, report: dict[str, Any]) -> None:
        if not _has_table(self._conn, "report_entities"):
            return
        names = {c[1] for c in _table_columns(self._conn, "report_entities")}
        for kind, items in (report.get("entities") or {}).items():
            for value in items:
                row = {
                    "report_id": report["id"],
                    "kind": kind,
                    "value": value,
                    "value_norm": " ".join(str(value).lower().split()),
                }
                row = {k: v for k, v in row.items() if k in names}
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                self._conn.execute(
                    f"INSERT INTO report_entities ({cols}) VALUES ({marks})",
                    tuple(row.values()),
                )

    def seed(
        self, user_id: str, conversation_id: str, reports: list[dict[str, Any]]
    ) -> dict[str, list[str]]:
        """Insert the reports. Return the IDs for the markers that the checks use."""
        self._require()
        ids: dict[str, list[str]] = {
            "current_conversation": [],
            "older": [],
            "other_users": [],
            "all": [],
        }
        with self._conn:
            for report in reports:
                owner = report.get("owner", user_id)
                if owner == user_id and report.get("conversation") == "current":
                    conv, marker = conversation_id, "current_conversation"
                elif owner == user_id:
                    conv, marker = f"seed-older-{owner}", "older"
                else:
                    conv, marker = f"seed-older-{owner}", "other_users"
                values = self._row_values(report, owner, conv)
                cols = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                self._conn.execute(
                    f"INSERT INTO reports ({cols}) VALUES ({marks})",
                    tuple(values.values()),
                )
                self._insert_entities(report)
                ids[marker].append(report["id"])
                ids["all"].append(report["id"])
        return ids

    def state(self, user_id: str | None = None) -> dict[str, str | None]:
        """Report ID -> `deleted_at` (None for a report that is not deleted)."""
        self._require()
        id_col = self._id_column()
        sql = f"SELECT {id_col}, deleted_at FROM reports"
        params: tuple[Any, ...] = ()
        if user_id is not None:
            sql += " WHERE owner_id = ?"
            params = (user_id,)
        return {str(r[0]): r[1] for r in self._conn.execute(sql, params).fetchall()}

    def body(self, report_id: str) -> str | None:
        """The Markdown body of one report (spec 32 column `body_markdown`)."""
        self._require()
        row = self._conn.execute(
            f"SELECT body_markdown FROM reports WHERE {self._id_column()} = ?",
            (report_id,),
        ).fetchone()
        return row[0] if row else None

    def delete_events(self) -> int:
        if not _has_table(self._conn, "audit_events"):
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'delete_confirmed'"
        ).fetchone()
        return int(row[0])


# --- database copy ----------------------------------------------------------------------------
def copy_app_db(src: Path, dst: Path) -> Path:
    """Copy `data/app.db` for one case. Without a source, create a new database."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        source = sqlite3.connect(src)
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    conn = connect(dst)  # applies the migrations that the copy does not have yet
    conn.close()
    return dst
