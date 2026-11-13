"""How a turn result looks (spec 37, B2.3). The CLI and the local UI use the same rules.

`describe(result)` gives the kind of output, the color, the prefixed text and whether the footer
line follows. `footer_line(result)` gives "12.4 s · 3 model calls · 2 queries · 34.0 MB · trace
4f2a9c1e". No rule here changes what the assistant does: it only selects the presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opsfleet_agent.errors import ErrorType
from opsfleet_agent.resilience import messages

Kind = Literal["answer", "confirmation", "message", "error"]
Style = Literal["default", "yellow", "green", "red"]

_DONE = frozenset({"deleted", "undo_restored"})
_CANCELLED = frozenset(
    {
        "wrong_number",
        "not_exact",
        "other_text",
        "expired",
        "abandoned",
        "changed",
        "internal_error",
        "undo_nothing",
        "undo_too_old",
    }
)
_NOT_POSSIBLE = frozenset(
    {ErrorType.GUARD_POLICY_BLOCK.value, ErrorType.EMPTY_RESULT.value}
)
_REFUSALS = frozenset({"off_topic", "unsafe"})
NOT_POSSIBLE = "Not possible:"
LIMIT = "Limit:"
ERROR = "Error:"


@dataclass(frozen=True)
class Display:
    kind: Kind
    style: Style
    text: str
    markdown: bool = False  # the text is Markdown (answers and reports)
    footer: bool = False  # the footer line follows (answers and reports only)


def _prefixed(prefix: str, text: str) -> str:
    text = text.strip()
    return text if text.startswith(prefix) else f"{prefix} {text}"


def _trace(text: str, trace_id: str | None) -> str:
    return messages.with_trace(text, trace_id)


def describe(result: Any) -> Display:
    """The presentation of a `TurnResult` (B2.3, "Message lines")."""
    text = str(result.answer or "")
    error = result.error_type
    if result.confirmation is not None:
        return Display("confirmation", "red", text)
    if result.delete_outcome in _DONE:
        return Display("message", "green", text)
    if (
        result.delete_outcome in _CANCELLED
        or getattr(result, "status", "ok") == "cancelled"
        or error == ErrorType.CONFIRMATION_REJECTED.value
    ):
        return Display("message", "yellow", text)
    if error == ErrorType.LIMIT_EXCEEDED.value:
        return Display(
            "message", "yellow", _trace(_prefixed(LIMIT, text), result.trace_id)
        )
    if error in _NOT_POSSIBLE:
        return Display("message", "yellow", _prefixed(NOT_POSSIBLE, text))
    if error == ErrorType.TOOL_ERROR.value and "Trace:" not in text:
        return Display("message", "default", text)  # a report that is not found
    if error:
        return Display("error", "red", _trace(_prefixed(ERROR, text), result.trace_id))
    if result.intent in _REFUSALS:
        return Display("message", "default", text, markdown=True)
    return Display("answer", "default", text, markdown=True, footer=True)


def footer_line(result: Any) -> str:
    totals = result.totals
    seconds = float(totals.get("seconds") or 0)
    calls = int(totals.get("llm_calls") or 0)
    queries = int(totals.get("bq_queries") or 0)
    mb = int(totals.get("bytes_billed") or 0) / 1e6
    trace = (result.trace_id or "")[: messages.TRACE_CHARS]
    return (
        f"{seconds:.1f} s · {calls} model {'call' if calls == 1 else 'calls'} · "
        f"{queries} {'query' if queries == 1 else 'queries'} · {mb:,.1f} MB · trace {trace}"
    )
