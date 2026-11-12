"""Scorers of the evaluation (spec 23, B2.4). Each scorer returns a `ScoreResult`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Reason names of a failed scorer (spec 23, B2.4).
REASONS = (
    "no_result",
    "missing_column",
    "wrong_rows",
    "wrong_order",
    "wrong_value",
    "fact_missing",
    "section_missing",
    "file_missing",
    "check_failed",
    "judge_failed",
    "judge_error",
    "capability_missing",
)


@dataclass
class ScoreResult:
    passed: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"pass": self.passed, "reason": self.reason, "details": self.details}

    @classmethod
    def ok(cls, **details: Any) -> ScoreResult:
        return cls(True, None, details)

    @classmethod
    def fail(cls, reason: str, **details: Any) -> ScoreResult:
        return cls(False, reason, details)
