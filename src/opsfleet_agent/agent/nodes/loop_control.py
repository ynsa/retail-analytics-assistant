"""The bounded self-correction of the data loop (spec 31, B2.4).

`turn.attempt` counts the SQL attempts of the current step and resets at each new step. A failed
attempt goes back to `write_sql` with the feedback, until the step has used all its attempts.
Then the loop ends with `give_up`. The spec 00 budget is the outer limit.
"""

from __future__ import annotations

from opsfleet_agent.agent.state import StepRecord, TurnState
from opsfleet_agent.errors import ErrorType

# Errors that the model can correct. Other errors end the turn with their message (spec 36).
RETRYABLE: frozenset[ErrorType] = frozenset(
    {
        ErrorType.GUARD_PARSE_ERROR,
        ErrorType.BQ_SYNTAX,
        ErrorType.BQ_NOT_FOUND,
        ErrorType.BQ_BYTES_CAP,
        ErrorType.EMPTY_RESULT,
    }
)
MAX_FEEDBACK_CHARS = 800


def current_step(turn: TurnState) -> StepRecord:
    steps = turn.get("steps") or []
    index = turn.get("step_index", 0)
    return dict(steps[index]) if index < len(steps) else {}  # type: ignore[return-value]


def with_step(turn: TurnState, **fields: object) -> TurnState:
    """The turn with the fields of the current step record updated."""
    steps = list(turn.get("steps") or [])
    index = turn.get("step_index", 0)
    while len(steps) <= index:
        steps.append({})
    steps[index] = {**steps[index], **fields}  # type: ignore[typeddict-item]
    return {**turn, "steps": steps}


def fail_attempt(
    turn: TurnState, error_type: ErrorType, message: str, max_attempts: int
) -> TurnState:
    """Record a failed attempt. Go back to `write_sql`, or end the loop with `give_up`."""
    message = message[:MAX_FEEDBACK_CHARS]
    turn = with_step(turn, error_type=error_type.value)
    turn = {
        **turn,
        "retry": {
            "sql": turn.get("sql", ""),
            "error_type": error_type.value,
            "message": message,
        },
        "model_feedback": message,
    }
    if turn.get("attempt", 0) >= max_attempts:
        return {**turn, "loop_outcome": "give_up", "error_type": error_type.value}
    return {**turn, "next": "write_sql"}
