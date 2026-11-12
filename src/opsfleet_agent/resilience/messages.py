"""User messages for failures (spec 36, B2.5). The texts are in `agent/prompts/replies.yaml`.

The clients (`llm.py`, `bq.py`) need the texts without a `PromptLibrary`, so this module reads the
`errors` section of the file once. Each failure message ends with the first 8 characters of the
trace ID, so a user can give the trace to a developer.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from opsfleet_agent.errors import ErrorType

REPLIES_FILE = (
    Path(__file__).resolve().parents[1] / "agent" / "prompts" / "replies.yaml"
)
TRACE_CHARS = 8
# The error types of a failure: the message gets the trace. Other types (a limit, a guard block,
# a rejected confirmation, an empty result) are answers about the request, not failures.
FAILURE_TYPES = frozenset(
    {
        ErrorType.LLM_TIMEOUT,
        ErrorType.LLM_RATE_LIMITED,
        ErrorType.LLM_ERROR,
        ErrorType.BQ_PERMISSION,
        ErrorType.BQ_TIMEOUT,
        ErrorType.BQ_ERROR,
        ErrorType.TOOL_ERROR,
        ErrorType.INTERNAL_ERROR,
    }
)
_ENV = Environment(undefined=StrictUndefined, autoescape=False)


@cache
def _errors() -> dict[str, str]:
    data: dict[str, Any] = yaml.safe_load(REPLIES_FILE.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (data.get("errors") or {}).items()}


def text(key: str, /, **values: Any) -> str:
    """The message `errors.<key>` of `replies.yaml`."""
    return _ENV.from_string(_errors()[key]).render(**values).strip()


def with_trace(message: str, trace_id: str | None) -> str:
    """`<message> Trace: 4f2a9c1e.` A message that has the trace already stays the same."""
    if not trace_id:
        return message
    suffix = text("trace", trace=trace_id[:TRACE_CHARS])
    if suffix in message:
        return message
    return f"{message.rstrip()} {suffix}"


def is_failure(error_type: str | ErrorType | None) -> bool:
    if not error_type:
        return False
    try:
        return ErrorType(error_type) in FAILURE_TYPES
    except ValueError:
        return False
