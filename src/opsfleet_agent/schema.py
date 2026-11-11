"""Table schemas for the prompts and the guard (spec 01, spec 30). Reading table metadata is free.

The schema is read once with `BigQueryRunner.get_table_schema()` and cached in
`data/schema_cache.json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opsfleet_agent.guard.rules import MANAGER_HIDDEN_COLUMNS, PII_COLUMNS


def load_schema(
    runner: Any, tables: list[str], cache_path: Path, refresh: bool = False
) -> dict[str, list[dict]]:
    if cache_path.exists() and not refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if set(cached) == set(tables):
            return cached
    schema = {table: runner.get_table_schema(table) for table in tables}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(schema, indent=1), encoding="utf-8")
    return schema


@dataclass(frozen=True)
class Schema:
    """The columns of the allowed tables: table -> column -> BigQuery type (lower-case names)."""

    dataset: str  # "project.dataset"
    tables: dict[str, dict[str, str]]

    @classmethod
    def from_cache(cls, raw: dict[str, list[dict]], dataset: str) -> Schema:
        return cls(
            dataset=dataset,
            tables={
                t.lower(): {f["name"].lower(): f["type"].upper() for f in fields}
                for t, fields in raw.items()
            },
        )

    @property
    def project(self) -> str:
        return self.dataset.rpartition(".")[0]

    @property
    def dataset_name(self) -> str:
        return self.dataset.rpartition(".")[2]

    def column_type(self, table: str, column: str) -> str | None:
        return self.tables.get(table, {}).get(column)

    def visible_columns(self, table: str, manager: bool) -> list[str]:
        """The columns that the model can use: no PII, and for a manager no mixed-brand field."""
        return [
            c
            for c in self.tables.get(table, {})
            if c not in PII_COLUMNS
            and not (manager and (table, c) in MANAGER_HIDDEN_COLUMNS)
        ]

    def sqlglot_mapping(self) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
        return {self.project: {self.dataset_name: self.tables}}


def model_schema_text(schema: Schema, manager: bool) -> str:
    """One line for each table: `dataset.table(column TYPE, ...)`, without hidden columns."""
    lines = []
    for table, columns in schema.tables.items():
        cols = ", ".join(
            f"{c} {columns[c]}" for c in schema.visible_columns(table, manager)
        )
        lines.append(f"`{schema.dataset}.{table}`({cols})")
    return "\n".join(lines)
