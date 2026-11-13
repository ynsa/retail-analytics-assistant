"""Error types shared by all parts of the assistant (spec 00, B2).

Every error has exactly one type, so the statistics (spec 35) can count failures by type.
"""

from enum import StrEnum


class ErrorType(StrEnum):
    LLM_TIMEOUT = "llm_timeout"
    LLM_RATE_LIMITED = "llm_rate_limited"
    LLM_ERROR = "llm_error"
    BQ_SYNTAX = "bq_syntax"
    BQ_NOT_FOUND = "bq_not_found"
    BQ_PERMISSION = "bq_permission"
    BQ_TIMEOUT = "bq_timeout"
    BQ_BYTES_CAP = "bq_bytes_cap"
    BQ_ERROR = "bq_error"
    GUARD_POLICY_BLOCK = "guard_policy_block"
    GUARD_PARSE_ERROR = "guard_parse_error"
    EMPTY_RESULT = "empty_result"
    # The SQL does not filter the stated period or does not apply a cited definition (spec 31, B7)
    SQL_MISMATCH = "sql_mismatch"
    LIMIT_EXCEEDED = "limit_exceeded"
    CONFIRMATION_REJECTED = "confirmation_rejected"
    TOOL_ERROR = "tool_error"
    INTERNAL_ERROR = "internal_error"


class AgentError(Exception):
    """An error with a type and a message that is safe to show to the user."""

    def __init__(
        self, error_type: ErrorType, user_message: str, detail: str = ""
    ) -> None:
        super().__init__(detail or user_message)
        self.error_type = error_type
        self.user_message = user_message
        self.detail = detail


class LimitExceeded(AgentError):
    """A cost limit stopped the work. `limit` names the limit, for example `max_llm_calls`."""

    def __init__(self, limit: str, user_message: str) -> None:
        super().__init__(
            ErrorType.LIMIT_EXCEEDED, user_message, detail=f"limit reached: {limit}"
        )
        self.limit = limit


class ReportNotFound(AgentError):
    """A report that does not exist, is deleted, or belongs to another user (spec 32, B2.6).
    All three cases give the same message, so a user cannot learn about the reports of others."""

    def __init__(self, report_id: str) -> None:
        shown = report_id.strip()[:20] or "?"
        super().__init__(
            ErrorType.TOOL_ERROR, f"Report {shown} not found.", "report not found"
        )
        self.report_id = report_id


class ConfigError(Exception):
    """The configuration files or the user selection are not valid."""
