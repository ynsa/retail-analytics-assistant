"""Resilience to failures of Gemini and BigQuery (spec 36).

- `policies.py`: the retry loop with fixed limits, waits and the time check.
- `breaker.py`: one circuit breaker for each model and one for BigQuery.
- `errors_map.py`: one error type and one user message for each failure.
- `messages.py`: the user messages from `agent/prompts/replies.yaml`, with the trace ID.
- `safe_node.py`: the log file for unexpected errors.
- `checkpoint.py`: a checkpoint saver that does not stop the turn when a write fails.
"""

from opsfleet_agent import capabilities

capabilities.register("resilience")  # spec 37, B2.5
