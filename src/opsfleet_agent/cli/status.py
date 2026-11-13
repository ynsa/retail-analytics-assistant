"""Node name to status text (spec 37, B2.2)."""

from __future__ import annotations

from opsfleet_agent.observability.progress import ProgressEvent

STATUS_TEXTS: dict[str, str] = {
    "route": "Reading the question",
    "write_sql": "Writing the query",
    "guard": "Checking the query",
    "run_query": "Running the query (BigQuery)",
    "check_empty": "Checking why the result is empty",
    "write_answer": "Writing the answer",
    "plan_report": "Planning the report",
    "write_report": "Writing the report",
    "render": "Drawing the charts",
    "summarize": "Updating the conversation memory",
}
OTHER = "Working"


def status_text(event: ProgressEvent, max_attempts: int = 3) -> str:
    """`Writing the query (attempt 2 of 3)` on a retry, else the text of the node."""
    text = STATUS_TEXTS.get(event.node, OTHER)
    if event.node == "write_sql" and event.attempt > 1:
        text += f" (attempt {event.attempt} of {max_attempts})"
    return text
