"""The saved-reports library (spec 32, B2.6). Spec 33 adds the delete on the same tables.

Each function takes the `UserContext`, and each SQL statement has `owner_id = ?`. A report of
another user, a deleted report and a missing report all give `ReportNotFound` with the same text.
`list`, `get` and `find` never return a report with `deleted_at` set.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from opsfleet_agent.config import Settings
from opsfleet_agent.errors import AgentError, ErrorType, ReportNotFound
from opsfleet_agent.guard.scanner import OutputScanner
from opsfleet_agent.identity import UserContext
from opsfleet_agent.reports.files import report_file, write_text_atomic
from opsfleet_agent.reports.ids import new_id, normalize_id
from opsfleet_agent.reports.models import Report, ReportList, ReportSummary
from opsfleet_agent.reports.render_html import html_texts, render_html
from opsfleet_agent.reports.render_md import render_md_file
from opsfleet_agent.reports.text import normalize_value
from opsfleet_agent.tracing import span

FileFormat = Literal["html", "md"]
JSON_COLUMNS = ("scope_brands", "sql", "sections", "charts", "definitions", "entities")
SUMMARY_COLUMNS = "report_id, title, period_start, period_end, created_at, conversation_id, file_html, file_md"
NEW_ID_ATTEMPTS = 10


@dataclass(frozen=True)
class Matcher:
    """Which reports `find()` returns (spec 33, B2.2). All matching is SQL in code."""

    kind: Literal["all", "conversation", "ids", "entity", "text"]
    conversation_id: str | None = None
    ids: tuple[str, ...] = ()
    value_norm: str | None = None  # for `entity`
    phrase: str | None = None  # for `text`: an FTS5 phrase query, already escaped


@dataclass(frozen=True)
class FoundReport:
    summary: ReportSummary
    reason_kind: str | None = None  # the entity kind of an entity match
    reason_value: str | None = None  # the entity value as stored


@dataclass
class ReportLibrary:
    conn: sqlite3.Connection
    settings: Settings
    scanner: OutputScanner = field(default_factory=OutputScanner)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def base(self) -> Path:
        return self.settings.reports_dir()

    # --- create ---------------------------------------------------------------------------------
    def new_report_id(self) -> str:
        for _ in range(NEW_ID_ATTEMPTS):
            report_id = new_id("R")
            row = self.conn.execute(
                "SELECT 1 FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
            if row is None:
                return report_id
        raise AgentError(ErrorType.INTERNAL_ERROR, "No free report ID.", "id collision")

    def create(self, report: Report) -> str:
        """Save a report and its entities in one transaction. Returns the report ID."""
        values = {
            "report_id": report.report_id,
            "owner_id": report.owner_id,
            "conversation_id": report.conversation_id,
            "title": report.title,
            "question": report.question,
            "created_at": report.created_at,
            "period_start": report.period_start,
            "period_end": report.period_end,
            "body_markdown": report.body_markdown,
            "calculated_figures": report.calculated_figures,
            "unverified_figures": report.unverified_figures,
            "file_html": report.file_html,
            "file_md": report.file_md,
            "model": report.model,
            "prompt_version": report.prompt_version,
            "trace_id": report.trace_id,
        }
        for name in JSON_COLUMNS:
            values[name] = json.dumps(getattr(report, name), default=str)
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with span("report.save", "tool", report_id=report.report_id), self.conn:
            self.conn.execute(
                f"INSERT INTO reports ({columns}) VALUES ({marks})",
                list(values.values()),
            )
            for kind, items in report.entities.items():
                self.conn.executemany(
                    "INSERT OR IGNORE INTO report_entities "
                    "(report_id, kind, value, value_norm) VALUES (?, ?, ?, ?)",
                    [(report.report_id, kind, v, normalize_value(v)) for v in items],
                )
        return report.report_id

    # --- read -----------------------------------------------------------------------------------
    def list(self, user: UserContext, limit: int | None = None) -> ReportList:
        limit = limit or self.settings.reports.max_listed
        rows = self.conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM reports WHERE owner_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC, report_id LIMIT ?",
            (user.id, limit),
        ).fetchall()
        total = self.conn.execute(
            "SELECT COUNT(*) FROM reports WHERE owner_id = ? AND deleted_at IS NULL",
            (user.id,),
        ).fetchone()[0]
        return ReportList(reports=[_summary(r) for r in rows], total=int(total))

    def get(self, user: UserContext, report_id: str) -> Report:
        canonical = normalize_id(report_id, "R")
        if canonical is None:
            raise ReportNotFound(report_id)
        row = self.conn.execute(
            "SELECT * FROM reports WHERE report_id = ? AND owner_id = ? AND deleted_at IS NULL",
            (canonical, user.id),
        ).fetchone()
        if row is None:
            raise ReportNotFound(canonical)
        data = dict(row)
        for name in JSON_COLUMNS:
            data[name] = json.loads(data[name] or "null")
        return Report(**data)

    def find(self, user: UserContext, matcher: Matcher) -> list[FoundReport]:
        """Active reports of the user that match (spec 33). Newest first."""
        base = (
            f"SELECT {', '.join('r.' + c.strip() for c in SUMMARY_COLUMNS.split(','))} "
            "FROM reports r WHERE r.owner_id = ? AND r.deleted_at IS NULL"
        )
        params: list[Any] = [user.id]
        order = " ORDER BY r.created_at DESC, r.report_id"
        if matcher.kind == "all":
            rows = self.conn.execute(base + order, params).fetchall()
            return [FoundReport(_summary(r)) for r in rows]
        if matcher.kind == "conversation":
            rows = self.conn.execute(
                base + " AND r.conversation_id = ?" + order,
                [*params, matcher.conversation_id],
            ).fetchall()
            return [FoundReport(_summary(r)) for r in rows]
        if matcher.kind == "ids":
            if not matcher.ids:
                return []
            marks = ", ".join("?" for _ in matcher.ids)
            rows = self.conn.execute(
                base + f" AND r.report_id IN ({marks})" + order, [*params, *matcher.ids]
            ).fetchall()
            return [FoundReport(_summary(r)) for r in rows]
        if matcher.kind == "entity":
            rows = self.conn.execute(
                f"SELECT {', '.join('r.' + c.strip() for c in SUMMARY_COLUMNS.split(','))}, "
                "MIN(e.kind) AS kind, MIN(e.value) AS value FROM reports r "
                "JOIN report_entities e ON e.report_id = r.report_id "
                "WHERE r.owner_id = ? AND r.deleted_at IS NULL AND e.value_norm = ? "
                "GROUP BY r.report_id" + order,
                [*params, matcher.value_norm],
            ).fetchall()
            return [FoundReport(_summary(r), r["kind"], r["value"]) for r in rows]
        rows = self.conn.execute(
            base
            + " AND r.report_id IN (SELECT report_id FROM reports_fts WHERE reports_fts MATCH ?)"
            + order,
            [*params, matcher.phrase],
        ).fetchall()
        return [FoundReport(_summary(r)) for r in rows]

    def entity_values(self, user: UserContext) -> list[tuple[str, str, str]]:
        """(kind, value, value_norm) of the user's active reports, for the close names."""
        rows = self.conn.execute(
            "SELECT DISTINCT e.kind, e.value, e.value_norm FROM report_entities e "
            "JOIN reports r ON r.report_id = e.report_id "
            "WHERE r.owner_id = ? AND r.deleted_at IS NULL ORDER BY e.value",
            (user.id,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- files ----------------------------------------------------------------------------------
    def render(self, report: Report, fmt: FileFormat) -> str:
        """The file text after the output scanner (spec 32, B2.5 "Scan")."""
        if fmt == "md":
            scanned = self.scanner.scan(render_md_file(report), target="report_file")
            return scanned.text
        text = render_html(report)
        found = self.scanner.scan("\n".join(html_texts(text)), target="report_file")
        if (
            found.found
        ):  # the content was scanned before; a new hit here means a bug: fail closed
            raise AgentError(
                ErrorType.TOOL_ERROR,
                "The HTML file was not written, because it contained personal data.",
                "scanner hit in the HTML file",
            )
        return text

    def export(self, user: UserContext, report_id: str, fmt: FileFormat) -> Path:
        """Write the HTML or Markdown file of an own report. A new export replaces the file."""
        report = self.get(user, report_id)
        path = report_file(self.base, report.owner_id, report.report_id, fmt)
        with span(
            "report.export", "tool", report_id=report.report_id, format=fmt
        ) as sp:
            text = self.render(report, fmt)
            size = len(text.encode("utf-8"))
            sp.set_attribute("bytes", size)
            if size > self.settings.reports.max_file_bytes:
                raise AgentError(
                    ErrorType.TOOL_ERROR,
                    "The report file is too large to write.",
                    f"file size {size}",
                )
            write_text_atomic(path, text)
        relative = str(path.relative_to(self.settings.data_path()))
        column = "file_html" if fmt == "html" else "file_md"
        with self.conn:
            self.conn.execute(
                f"UPDATE reports SET {column} = ? WHERE report_id = ? AND owner_id = ?",
                (relative, report.report_id, user.id),
            )
        return path


def _summary(row: sqlite3.Row) -> ReportSummary:
    return ReportSummary(
        report_id=row["report_id"],
        title=row["title"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        created_at=row["created_at"],
        conversation_id=row["conversation_id"],
        file_html=row["file_html"],
        file_md=row["file_md"],
    )
