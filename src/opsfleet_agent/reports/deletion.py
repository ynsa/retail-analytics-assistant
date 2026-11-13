"""Soft delete, undo and purge of reports (spec 33, B2.4).

- `DeleteReportsTool` (side effect `destructive`): the tool registry runs it only with a valid
  `ConfirmationGrant`. In one transaction it checks that the listed reports are still active,
  moves their files to `data/reports/<owner>/.trash/<batch>/`, writes the batch row, sets
  `deleted_at`, and writes the audit event. A failed commit moves the files back.
- `undo_batch()`: restores the last batch of the user (or a named one) within 30 days.
- `purge()`: the operator command. It removes batches older than 30 days for good.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from opsfleet_agent.agent.tools import ToolContext, ToolResult
from opsfleet_agent.config import Settings
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.identity import UserContext
from opsfleet_agent.reports.files import display_path, owner_dir, trash_dir
from opsfleet_agent.reports.ids import new_id, normalize_id
from opsfleet_agent.store.audit import confirm_span, write_audit

UNDO_DAYS = 30
FILE_COLUMNS = ("file_html", "file_md")


class DeleteArgs(BaseModel):
    report_ids: list[str]
    ids_hash: str
    conversation_id: str | None = None


@dataclass
class FileMove:
    report_id: str
    column: str
    path: str  # relative to the data folder, as in the report row
    trash_path: str  # relative to the data folder


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _inside(path: Path, folder: Path) -> bool:
    return path.resolve().is_relative_to(folder)


class DeleteReportsTool:
    name = "delete_reports"
    description = (
        "Soft-delete the listed reports of the user. Needs the typed confirmation."
    )
    args_model = DeleteArgs
    side_effect = "destructive"

    def __init__(self, settings: Settings, clock: Callable[[], datetime]) -> None:
        self.settings = settings
        self.clock = clock

    def _new_batch_id(self, conn: sqlite3.Connection) -> str:
        for _ in range(10):
            batch_id = new_id("D")
            row = conn.execute(
                "SELECT 1 FROM delete_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                return batch_id
        raise AgentError(ErrorType.INTERNAL_ERROR, "No free batch ID.", "id collision")

    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        assert isinstance(args, DeleteArgs)
        conn: sqlite3.Connection = ctx.store
        user = ctx.user
        ids = sorted(set(args.report_ids))
        data_dir = self.settings.data_path().resolve()
        base = self.settings.reports_dir()
        folder = owner_dir(base, user.id)
        batch_id = self._new_batch_id(conn)
        trash = trash_dir(base, user.id, batch_id)
        now = self.clock().isoformat()
        moved: list[FileMove] = []
        missing: list[str] = []
        marks = ", ".join("?" for _ in ids)
        _begin(conn)
        try:
            rows = conn.execute(
                f"SELECT report_id, file_html, file_md FROM reports WHERE owner_id = ? "
                f"AND deleted_at IS NULL AND report_id IN ({marks})",
                [user.id, *ids],
            ).fetchall()
            if sorted(r["report_id"] for r in rows) != ids:
                conn.rollback()
                return ToolResult(
                    ok=False,
                    data={"outcome": "changed"},
                    error_type=ErrorType.CONFIRMATION_REJECTED,
                )
            for row in rows:
                for column in FILE_COLUMNS:
                    rel = row[column]
                    if not rel:
                        continue
                    src = (data_dir / rel).resolve()
                    if not _inside(src, folder) or not src.is_file():
                        missing.append(display_path(data_dir / rel))
                        continue
                    dst = trash / src.name
                    _move(src, dst)
                    moved.append(
                        FileMove(
                            row["report_id"],
                            column,
                            rel,
                            str(dst.relative_to(data_dir)),
                        )
                    )
            conn.execute(
                "INSERT INTO delete_batches (batch_id, owner_id, conversation_id, created_at, "
                "report_ids, ids_hash, files) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    user.id,
                    args.conversation_id,
                    now,
                    json.dumps(ids),
                    args.ids_hash,
                    json.dumps([m.__dict__ for m in moved]),
                ),
            )
            conn.execute(
                f"UPDATE reports SET deleted_at = ?, delete_batch_id = ? "
                f"WHERE owner_id = ? AND report_id IN ({marks})",
                [now, batch_id, user.id, *ids],
            )
            write_audit(
                conn,
                user_id=user.id,
                conversation_id=args.conversation_id,
                action="delete_confirmed",
                details={
                    "batch_id": batch_id,
                    "report_ids": ids,
                    "ids_hash": args.ids_hash,
                    "files_moved": len(moved),
                    "files_missing": len(missing),
                },
                at=self.clock(),
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            for move in reversed(moved):  # put the files back
                src = data_dir / move.trash_path
                if src.exists():
                    _move(src, data_dir / move.path)
            raise
        return ToolResult(
            ok=True,
            data={
                "outcome": "deleted",
                "batch_id": batch_id,
                "count": len(ids),
                "files_moved": [display_path(data_dir / m.path) for m in moved],
                "files_missing": missing,
                "folder": display_path(folder),
            },
        )


# --- undo -----------------------------------------------------------------------------------------
@dataclass
class UndoResult:
    outcome: Literal["restored", "nothing", "too_old"]
    batch_id: str | None = None
    report_ids: list[str] = field(default_factory=list)
    files_restored: int = 0
    files_missing: list[str] = field(default_factory=list)


def _batch_row(
    conn: sqlite3.Connection, user: UserContext, batch_id: str | None
) -> sqlite3.Row | None:
    if batch_id:
        canonical = normalize_id(batch_id, "D")
        if canonical is None:
            return None
        return conn.execute(
            "SELECT * FROM delete_batches WHERE batch_id = ? AND owner_id = ? "
            "AND undone_at IS NULL",
            (canonical, user.id),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM delete_batches WHERE owner_id = ? AND undone_at IS NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (user.id,),
    ).fetchone()


def undo_batch(
    conn: sqlite3.Connection,
    settings: Settings,
    user: UserContext,
    batch_id: str | None = None,
    *,
    now: datetime | None = None,
    conversation_id: str | None = None,
) -> UndoResult:
    """Restore a delete batch of this user: rows and files, in one transaction. No confirmation:
    an undo destroys nothing (spec 33, B1)."""
    now = now or datetime.now(UTC)
    row = _batch_row(conn, user, batch_id)
    if row is None:
        confirm_span("undo", "nothing")
        return UndoResult("nothing")
    created = datetime.fromisoformat(row["created_at"])
    if row["purged_at"] or now - created > timedelta(days=UNDO_DAYS):
        confirm_span("undo", "too_old", batch_id=row["batch_id"])
        return UndoResult("too_old", row["batch_id"])
    data_dir = settings.data_path().resolve()
    folder = owner_dir(settings.reports_dir(), user.id)
    ids = json.loads(row["report_ids"])
    moves = [FileMove(**m) for m in json.loads(row["files"])]
    restored: list[FileMove] = []
    missing: list[str] = []
    _begin(conn)
    try:
        for move in moves:
            src, dst = data_dir / move.trash_path, data_dir / move.path
            if not (_inside(src, folder) and _inside(dst, folder)) or not src.is_file():
                missing.append(display_path(dst))
                continue
            _move(src, dst)
            restored.append(move)
        conn.execute(
            "UPDATE reports SET deleted_at = NULL, delete_batch_id = NULL "
            "WHERE delete_batch_id = ? AND owner_id = ?",
            (row["batch_id"], user.id),
        )
        conn.execute(
            "UPDATE delete_batches SET undone_at = ? WHERE batch_id = ?",
            (now.isoformat(), row["batch_id"]),
        )
        write_audit(
            conn,
            user_id=user.id,
            conversation_id=conversation_id,
            action="delete_undone",
            details={"batch_id": row["batch_id"], "report_ids": ids},
            at=now,
            commit=False,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        for move in reversed(restored):
            _move(data_dir / move.path, data_dir / move.trash_path)
        raise
    shutil.rmtree(
        trash_dir(settings.reports_dir(), user.id, row["batch_id"]), ignore_errors=True
    )
    confirm_span("undo", "restored", batch_id=row["batch_id"], count=len(ids))
    return UndoResult("restored", row["batch_id"], ids, len(restored), missing)


# --- purge ----------------------------------------------------------------------------------------
@dataclass
class PurgeResult:
    dry_run: bool
    batches: list[dict[str, Any]] = field(
        default_factory=list
    )  # batch_id, owner_id, count
    files_deleted: int = 0


def purge(
    conn: sqlite3.Connection, settings: Settings, now: datetime, dry_run: bool = False
) -> PurgeResult:
    """Delete for good the batches older than 30 days that are not undone (spec 33, B2.4)."""
    cutoff = (now - timedelta(days=UNDO_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT * FROM delete_batches WHERE created_at < ? AND undone_at IS NULL "
        "AND purged_at IS NULL ORDER BY created_at",
        (cutoff,),
    ).fetchall()
    result = PurgeResult(dry_run=dry_run)
    for row in rows:
        ids = json.loads(row["report_ids"])
        result.batches.append(
            {
                "batch_id": row["batch_id"],
                "owner_id": row["owner_id"],
                "count": len(ids),
            }
        )
        if dry_run:
            continue
        files = 0
        trash = trash_dir(settings.reports_dir(), row["owner_id"], row["batch_id"])
        if trash.exists():
            files = sum(1 for p in trash.rglob("*") if p.is_file())
        _begin(conn)
        try:
            conn.execute(
                "DELETE FROM reports WHERE delete_batch_id = ? AND owner_id = ? "
                "AND deleted_at IS NOT NULL",
                (row["batch_id"], row["owner_id"]),
            )
            conn.execute(
                "UPDATE delete_batches SET purged_at = ? WHERE batch_id = ?",
                (now.isoformat(), row["batch_id"]),
            )
            write_audit(
                conn,
                user_id=row["owner_id"],
                conversation_id=row["conversation_id"],
                action="reports_purged",
                details={
                    "batch_id": row["batch_id"],
                    "report_ids": ids,
                    "files_deleted": files,
                },
                at=now,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        shutil.rmtree(trash, ignore_errors=True)
        result.files_deleted += files
        confirm_span("purge", "purged", batch_id=row["batch_id"], count=len(ids))
    if dry_run:
        confirm_span("purge", "dry_run", count=len(rows))
    return result
