"""Observability and statistics (spec 35).

- `redact.py`: the masking of SQL text, free text and prompts, and the attribute allowlist.
- `logging_setup.py`: JSON logs in `data/logs/<UTC date>.jsonl`.
- `turn_metrics.py`: the figures of each turn in `conversation_turns`.
- `progress.py`: the node events of a running turn (spec 37 status line).
- `stats.py`: `/stats`, `opsfleet-agent stats` and the export.
- `trace_view.py`: `/trace`.
- `retention.py`: `prune(now)`.
"""

from opsfleet_agent import capabilities

capabilities.register("observability")
