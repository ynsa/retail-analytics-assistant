"""One error type and one user message for each failure of Gemini and BigQuery (spec 36, B2.5).

`map_gemini_error(exc)` and `map_bq_error(exc)` return a `ServiceFailure`. Its `temporary` flag
decides the retry, the fallback model and the circuit breaker.
"""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any

from google.api_core import exceptions as gexc

from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.resilience import messages
from opsfleet_agent.resilience.policies import ServiceFailure

GEMINI_TEMPORARY_CODES = frozenset({429, 500, 502, 503, 504})
BQ_TEMPORARY_REASONS = frozenset({"backendError", "internalError", "rateLimitExceeded"})
BQ_PERMISSION_REASONS = frozenset({"accessDenied"})
_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (concurrent.futures.TimeoutError, TimeoutError)) or any(
        "Timeout" in cls.__name__ for cls in type(exc).__mro__
    )


def is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionError):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(
        names
        & {"ConnectError", "ConnectionError", "TransportError", "RemoteProtocolError"}
    )


# --- Gemini --------------------------------------------------------------------------------------


def _details_text(exc: Any) -> str:
    return f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'details', '') or ''}"


def retry_after_of(exc: Any) -> float | None:
    """The wait that the service asks for: the `Retry-After` header, or the `RetryInfo` detail of
    the Gemini API (`"retryDelay": "41s"`)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            value = None
        if value is not None:
            match = _DELAY.match(str(value))
            if match:
                return float(match.group(1))
    for item in _detail_items(getattr(exc, "details", None)):
        if isinstance(item, dict) and "retryDelay" in item:
            match = _DELAY.match(str(item["retryDelay"]))
            if match:
                return float(match.group(1))
    return None


def _detail_items(details: Any) -> list[Any]:
    """The `details` list of a Google API error body: {"error": {"details": [...]}}."""
    if isinstance(details, dict):
        inner = details.get("error", details)
        items = inner.get("details") if isinstance(inner, dict) else None
        return list(items or [])
    return []


def is_daily_quota(exc: Any) -> bool:
    """A 429 for the daily quota names a quota "per day", for example
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier`."""
    text = _details_text(exc)
    return "PerDay" in text or "per day" in text.lower()


def is_invalid_key(code: int | None, exc: Any) -> bool:
    text = _details_text(exc)
    return (
        code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text
    )


def map_gemini_error(exc: BaseException) -> ServiceFailure:
    """Turn an SDK or network exception into a `ServiceFailure`."""
    if isinstance(exc, ServiceFailure):
        return exc
    if isinstance(exc, AgentError):
        return ServiceFailure(exc.error_type, exc.user_message, exc.detail)
    detail = f"{type(exc).__name__}: {str(exc)[:300]}"
    if is_timeout(exc):
        return ServiceFailure(
            ErrorType.LLM_TIMEOUT,
            messages.text("llm_timeout"),
            detail,
            kind="timeout",
            temporary=True,
        )
    if is_connection_error(exc):
        return ServiceFailure(
            ErrorType.LLM_ERROR,
            messages.text("llm_down"),
            detail,
            kind="connection",
            temporary=True,
        )
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        return ServiceFailure(
            ErrorType.LLM_ERROR, messages.text("llm_down"), detail, kind="other"
        )
    if code == 429:
        if is_daily_quota(exc):
            return daily_quota_failure(detail)
        return ServiceFailure(
            ErrorType.LLM_RATE_LIMITED,
            messages.text("llm_busy"),
            detail,
            kind="busy",
            temporary=True,
            retry_after=retry_after_of(exc),
        )
    if code in GEMINI_TEMPORARY_CODES:
        return ServiceFailure(
            ErrorType.LLM_ERROR,
            messages.text("llm_down"),
            detail,
            kind="server",
            temporary=True,
        )
    if code == 402:
        return ServiceFailure(
            ErrorType.LLM_ERROR, messages.text("llm_billing"), detail, kind="billing"
        )
    if is_invalid_key(code, exc):
        return ServiceFailure(
            ErrorType.LLM_ERROR, messages.text("llm_key"), detail, kind="key"
        )
    return ServiceFailure(
        ErrorType.LLM_ERROR, messages.text("llm_down"), detail, kind="other"
    )


def daily_quota_failure(detail: str = "circuit open: daily quota") -> ServiceFailure:
    return ServiceFailure(
        ErrorType.LLM_RATE_LIMITED,
        messages.text("llm_daily"),
        detail,
        kind="daily_quota",
        daily_quota=True,
    )


def refused() -> ServiceFailure:
    """A safety block of the model: no retry, no fallback."""
    return ServiceFailure(
        ErrorType.LLM_ERROR,
        messages.text("llm_refused"),
        "safety block",
        kind="refused",
    )


def gemini_unavailable(
    last: ServiceFailure | None, circuit_open: bool
) -> ServiceFailure:
    """The final failure when no model answered. A timeout and a busy model keep their message.
    A breaker that the daily quota opened keeps the daily message."""
    if last is not None and last.kind in ("timeout", "busy", "daily_quota"):
        return ServiceFailure(
            last.error_type,
            last.user_message,
            last.detail,
            kind=last.kind,
            temporary=last.temporary,
            daily_quota=last.daily_quota,
            circuit_open=circuit_open,
        )
    return ServiceFailure(
        ErrorType.LLM_ERROR,
        messages.text("llm_down"),
        last.detail if last else "circuit open",
        kind="down",
        temporary=True,
        circuit_open=circuit_open,
    )


# --- BigQuery ------------------------------------------------------------------------------------


def bq_reasons(exc: Any) -> set[str]:
    reasons: set[str] = set()
    for item in getattr(exc, "errors", None) or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.add(str(item["reason"]))
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        reasons.add(reason)
    return reasons


def bq_down(detail: str = "", *, circuit_open: bool = False) -> ServiceFailure:
    return ServiceFailure(
        ErrorType.BQ_ERROR,
        messages.text("bq_down"),
        detail,
        kind="down",
        temporary=True,
        circuit_open=circuit_open,
    )


def map_bq_error(exc: BaseException, timeout_s: float = 30) -> ServiceFailure:
    """Turn a BigQuery or auth exception into a `ServiceFailure`."""
    if isinstance(exc, ServiceFailure):
        return exc
    if isinstance(exc, AgentError):
        return ServiceFailure(exc.error_type, exc.user_message, exc.detail)
    text = str(exc)
    reasons = bq_reasons(exc)
    names = {cls.__name__ for cls in type(exc).__mro__}
    if is_timeout(exc):
        return ServiceFailure(
            ErrorType.BQ_TIMEOUT,
            messages.text("bq_timeout", seconds=f"{timeout_s:g}"),
            text,
            kind="timeout",
        )
    if names & {"RefreshError", "DefaultCredentialsError"}:
        return ServiceFailure(
            ErrorType.BQ_PERMISSION, messages.text("bq_permission"), text, kind="auth"
        )
    if "bytes billed" in text.lower() or "bytesbilled" in text.lower():
        return ServiceFailure(
            ErrorType.BQ_BYTES_CAP,
            "This query reads too much data. Please narrow the period or the brands.",
            text,
            kind="bytes_cap",
        )
    if reasons & BQ_TEMPORARY_REASONS or isinstance(
        exc,
        (gexc.InternalServerError, gexc.ServiceUnavailable, gexc.TooManyRequests),
    ):
        return bq_down(f"{type(exc).__name__}: {text}")
    if is_connection_error(exc):
        return bq_down(f"{type(exc).__name__}: {text}")
    if "quotaExceeded" in reasons:
        return ServiceFailure(
            ErrorType.BQ_ERROR, messages.text("bq_down"), text, kind="quota"
        )
    if isinstance(exc, gexc.NotFound):
        return ServiceFailure(
            ErrorType.BQ_NOT_FOUND,
            "The query uses a table or a column that does not exist.",
            text,
            kind="not_found",
        )
    if isinstance(exc, (gexc.Forbidden, gexc.Unauthorized)) or (
        reasons & BQ_PERMISSION_REASONS
    ):
        return ServiceFailure(
            ErrorType.BQ_PERMISSION, messages.text("bq_permission"), text, kind="auth"
        )
    if isinstance(exc, gexc.BadRequest):
        return ServiceFailure(
            ErrorType.BQ_SYNTAX, "The query has an error.", text, kind="syntax"
        )
    return ServiceFailure(
        ErrorType.BQ_ERROR,
        messages.text("bq_down"),
        f"{type(exc).__name__}: {text}",
        kind="other",
    )
