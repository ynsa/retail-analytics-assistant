"""No crash after an error (spec 36, B2.6).

The node wrapper (`agent.nodes.common.node`) and the session use these helpers:

- `failure_answer()`: the user message of an error, with "Trace: <8 characters>." for a failure.
- `log_exception()`: the masked stack trace goes to `data/logs/<UTC date>.jsonl` (spec 35),
  never to the screen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opsfleet_agent.errors import AgentError, ErrorType, ReportNotFound
from opsfleet_agent.observability import logging_setup
from opsfleet_agent.observability.logging_setup import LOGGER_NAME
from opsfleet_agent.resilience import messages

_logger = logging.getLogger(LOGGER_NAME)
_logger.propagate = False  # the CLI shows only the messages, never a stack trace
_logger.addHandler(logging.NullHandler())


def init_logging(log_dir: Path, debug: bool | None = None) -> Path:
    """The JSON logs of spec 35 in `<log_dir>/<UTC date>.jsonl`."""
    return logging_setup.init_logging(log_dir, debug=debug)


def log_exception(where: str, trace_id: str | None) -> None:
    """The stack trace of the current exception, with the trace ID. Call it in `except`."""
    _logger.exception("unexpected error in %s (trace %s)", where, trace_id or "-")


def internal_message(trace_id: str | None) -> str:
    return messages.with_trace(messages.text("internal"), trace_id)


def failure_answer(exc: AgentError, trace_id: str | None) -> str:
    """The message of an error that ends the turn. A failure gets the trace ID. A report that is
    not found is a user error (spec 32, B7 #16), so it gets no trace."""
    if isinstance(exc, ReportNotFound) or not messages.is_failure(exc.error_type):
        return exc.user_message
    if exc.error_type is ErrorType.INTERNAL_ERROR and not exc.user_message:
        return internal_message(trace_id)
    return messages.with_trace(exc.user_message, trace_id)
