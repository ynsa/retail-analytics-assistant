"""Structured JSON logs (spec 35, B1 and B2.3 rule 5).

Each log record becomes one JSON line in `data/logs/<UTC date>.jsonl`, with `trace_id` and
`span_id` of the current span. The message, each extra field and the stack trace are masked
before the write. Loggers of other libraries write only `WARNING` and above.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace

from opsfleet_agent.observability.redact import Redactor, mask_text

LOGGER_NAME = "opsfleet_agent"
LIBRARY_LOGGERS = ("google", "urllib3", "httpx", "httpcore", "langgraph", "matplotlib")
# The standard attributes of a `LogRecord`. Every other attribute is an extra field.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
_redactor = Redactor()


def log_file(log_dir: Path, when: datetime) -> Path:
    return log_dir / f"{when.astimezone(UTC):%Y-%m-%d}.jsonl"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_text(record.getMessage()),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD and not k.startswith("_")
        }
        if extra:
            line["fields"] = _redactor.fields(extra)
        if record.exc_info:
            line["exception"] = mask_text(self.formatException(record.exc_info))
        return json.dumps(line, default=str, ensure_ascii=False)


class DailyJsonlHandler(logging.Handler):
    """Appends each record to the file of its UTC date."""

    def __init__(self, log_dir: Path) -> None:
        super().__init__(level=logging.INFO)
        self.log_dir = log_dir
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            path = log_file(self.log_dir, datetime.fromtimestamp(record.created, UTC))
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:  # noqa: BLE001 - a log problem never stops the chat
            self.handleError(record)


def init_logging(log_dir: Path, debug: bool | None = None) -> Path:
    """Send the logs of this process to `log_dir`. A second call with another folder replaces
    the handler (the tests use one folder for each test). `debug=True` also prints the log
    records, with their stack traces, on stderr (`chat --debug`). `None` keeps the current
    choice."""
    log_dir = log_dir.resolve()
    ours = logging.getLogger(LOGGER_NAME)
    ours.propagate = False  # the CLI shows only the messages, never a stack trace
    ours.setLevel(logging.INFO)
    root = logging.getLogger()
    remove = DailyJsonlHandler if debug is None else (DailyJsonlHandler, _DebugHandler)
    for logger in (ours, root):
        for handler in list(logger.handlers):
            if isinstance(handler, remove):
                logger.removeHandler(handler)
    handler = DailyJsonlHandler(log_dir)
    ours.addHandler(handler)
    root.addHandler(handler)  # the WARNING records of other libraries
    if debug:
        ours.addHandler(_DebugHandler())
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return log_dir


class _DebugHandler(logging.StreamHandler):  # type: ignore[type-arg]
    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
