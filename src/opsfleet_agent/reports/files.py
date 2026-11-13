"""Paths and writes of report files (spec 32, B2.5, and spec 33, B2.4).

Code builds each path from the owner ID and the report ID only. The owner ID must match
`[a-z0-9_-]+`, and the resolved path must stay inside `<reports dir>/<owner_id>/`. A wrong ID
stops the work before any file write.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from opsfleet_agent.config import PROJECT_ROOT
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.reports.ids import normalize_id

OWNER_ID = re.compile(r"^[a-z0-9_-]+$")
EXTENSIONS = {"html": "html", "md": "md"}
TRASH = ".trash"
FILE_ERROR = "The report file could not be written."


def _refuse(detail: str) -> AgentError:
    return AgentError(ErrorType.TOOL_ERROR, FILE_ERROR, detail)


def owner_dir(base: Path, owner_id: str) -> Path:
    if not OWNER_ID.match(owner_id or "") or owner_id in (".", ".."):
        raise _refuse("unsafe owner id")
    root = base.resolve()
    folder = (root / owner_id).resolve()
    if folder.parent != root:
        raise _refuse("owner folder outside the reports folder")
    return folder


def report_file(base: Path, owner_id: str, report_id: str, fmt: str) -> Path:
    if fmt not in EXTENSIONS:
        raise _refuse("unknown file format")
    if normalize_id(report_id, "R") != report_id:
        raise _refuse("unsafe report id")
    folder = owner_dir(base, owner_id)
    path = (folder / f"{report_id}.{EXTENSIONS[fmt]}").resolve()
    if path.parent != folder:
        raise _refuse("report file outside the owner folder")
    return path


def trash_dir(base: Path, owner_id: str, batch_id: str) -> Path:
    if normalize_id(batch_id, "D") != batch_id:
        raise _refuse("unsafe batch id")
    folder = owner_dir(base, owner_id)
    path = (folder / TRASH / batch_id).resolve()
    if not path.is_relative_to(folder):
        raise _refuse("trash folder outside the owner folder")
    return path


def write_text_atomic(path: Path, text: str) -> None:
    """Write a new file, then replace the old one. A reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def display_path(path: Path) -> str:
    """`data/reports/maya/R-7K2PQ.html` for a file in the project, else the full path."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
