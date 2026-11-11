"""Table schemas for the prompts (spec 01). Reading table metadata is free in BigQuery.

The schema is read once with `BigQueryRunner.get_table_schema()` and cached in
`data/schema_cache.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def schema_to_text(schema: dict[str, list[dict]], dataset: str) -> str:
    """One line for each table: `dataset.table(column TYPE, ...)`."""
    lines = []
    for table, fields in schema.items():
        cols = ", ".join(f"{f['name']} {f['type']}" for f in fields)
        lines.append(f"`{dataset}.{table}`({cols})")
    return "\n".join(lines)
