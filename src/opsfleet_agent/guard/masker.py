"""Result masker: cleans every query result before a model sees it (spec 30, B2.5).

The guard keeps PII out of the SQL, so a PII column here means that the guard missed a case. The
masker then drops the column and writes a `guard` span with `masker_hit = true` and the error type
`internal_error`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from opentelemetry.trace import Status, StatusCode

from opsfleet_agent.bq import QueryResult
from opsfleet_agent.errors import ErrorType
from opsfleet_agent.guard.rules import CUSTOMER_ID_NAMES, DEMOGRAPHIC_NAMES, PII_COLUMNS
from opsfleet_agent.guard.scanner import looks_like_pii
from opsfleet_agent.tracing import span

MASKED = "[masked]"


@dataclass(frozen=True)
class MaskedResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool
    masked_cells: int = 0
    dropped_columns: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        if not self.columns:
            return "(no columns)"
        if not self.rows:
            return "(no rows)"
        lines = [
            "| " + " | ".join(self.columns) + " |",
            "|" + "---|" * len(self.columns),
        ]
        for row in self.rows:
            lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
        return "\n".join(lines)

    def data_block(self, ref: str = "r1") -> str:
        """The rows as a data block. The prompts treat a data block as data (spec 31, B2.7)."""
        return f'<data source="query" ref="{ref}">\n{self.to_markdown()}\n</data>'


def _cell(value: Any) -> str:
    if value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)):
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _matches(name: str, names: frozenset[str]) -> bool:
    lower = name.lower()
    return lower in names or any(lower.endswith(f"_{n}") for n in names)


class ResultMasker:
    def __init__(self, max_rows: int = 200, max_chars: int = 200) -> None:
        self.max_rows = max_rows
        self.max_chars = max_chars

    def mask(self, result: QueryResult, guard_result: Any = None) -> MaskedResult:
        """`guard_result` is the GuardResult of the query (its `customer_level` flag), or None."""
        frame = result.dataframe
        columns = [str(c) for c in frame.columns]
        pii = [c for c in columns if c.lower() in PII_COLUMNS]
        customer_level = bool(getattr(guard_result, "customer_level", False)) or any(
            _matches(c, CUSTOMER_ID_NAMES) for c in columns
        )
        demographic = (
            [c for c in columns if _matches(c, DEMOGRAPHIC_NAMES)]
            if customer_level
            else []
        )
        dropped = pii + [c for c in demographic if c not in pii]
        keep = [c for c in columns if c not in dropped]

        truncated = result.truncated or len(frame) > self.max_rows
        masked_cells = 0
        rows: list[tuple[Any, ...]] = []
        for raw in frame[keep].head(self.max_rows).itertuples(index=False, name=None):
            row = []
            for value in raw:
                if isinstance(value, str):
                    if looks_like_pii(value):
                        value = MASKED
                        masked_cells += 1
                    value = value[: self.max_chars]
                row.append(value)
            rows.append(tuple(row))

        with span("guard.mask", "guard") as sp:
            sp.set_attribute("masker_hit", bool(pii))
            sp.set_attribute("masked_cells", masked_cells)
            sp.set_attribute("dropped_columns", len(dropped))
            sp.set_attribute("rows", len(rows))
            if pii:
                sp.set_attribute("error_type", ErrorType.INTERNAL_ERROR.value)
                sp.set_status(Status(StatusCode.ERROR, ErrorType.INTERNAL_ERROR.value))
        return MaskedResult(
            columns=keep,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            masked_cells=masked_cells,
            dropped_columns=dropped,
        )
