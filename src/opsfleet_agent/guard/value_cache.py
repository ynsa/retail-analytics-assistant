"""Value cache: distinct filter values and the date range of the data (spec 30, B2.8).

One small query builds `data/value_cache.json` (about 40 MB billed: only short columns). The code
builds it again on each new UTC day, or on request (`chat --refresh-cache`). The dataset changes
its full history each day, so the date range of an older cache can be wrong (spec 50 round 1). The cache also holds the brand
list, so the brand names in `config/users.yaml` can be checked at the start.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from opsfleet_agent.config import Role, UsersConfig
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.limits import TurnBudget

MAX_AGE_DAYS = 1  # a cache of an earlier UTC day is stale

# Columns with a small set of values (spec 30, B2.7 check 1), by table.
CACHED_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": ("status", "gender"),
    "order_items": ("status",),
    "products": ("department", "category", "brand"),
    "users": ("gender", "traffic_source", "country"),
}
DATED_TABLES: frozenset[str] = frozenset({"orders", "order_items", "users"})


@dataclass(frozen=True)
class ValueCache:
    built_at: str  # ISO date (UTC)
    values: dict[str, tuple[str, ...]]  # "table.column" -> distinct values
    date_min: dict[str, str]  # table -> ISO date of the first `created_at`

    @property
    def brands(self) -> tuple[str, ...]:
        return self.values.get("products.brand", ())

    def has(self, table: str, column: str) -> bool:
        return f"{table}.{column}" in self.values

    def get(self, table: str, column: str) -> tuple[str, ...]:
        return self.values.get(f"{table}.{column}", ())

    def first_date(self, table: str | None = None) -> date | None:
        dates = [self.date_min[table]] if table in self.date_min else []
        dates = dates or list(self.date_min.values())
        return date.fromisoformat(min(dates)) if dates else None

    def is_stale(self, today: date | None = None) -> bool:
        today = today or datetime.now(UTC).date()
        return today - date.fromisoformat(self.built_at) >= timedelta(days=MAX_AGE_DAYS)

    def to_json(self) -> str:
        return json.dumps(
            {
                "built_at": self.built_at,
                "values": {k: list(v) for k, v in self.values.items()},
                "date_min": self.date_min,
            },
            indent=1,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> ValueCache:
        raw = json.loads(text)
        return cls(
            built_at=raw["built_at"],
            values={k: tuple(v) for k, v in raw["values"].items()},
            date_min=dict(raw["date_min"]),
        )


def build_query(dataset: str) -> str:
    """One row for each table, with the distinct values as a JSON text (4 rows in total)."""
    parts = []
    for table, columns in CACHED_COLUMNS.items():
        fields = [
            f"ARRAY_AGG(DISTINCT CAST({c} AS STRING) IGNORE NULLS) AS {c}"
            for c in columns
        ]
        if table in DATED_TABLES:
            fields.append("CAST(DATE(MIN(created_at)) AS STRING) AS min_created_at")
        parts.append(
            f"SELECT '{table}' AS tbl, TO_JSON_STRING(STRUCT({', '.join(fields)})) AS data "
            f"FROM `{dataset}.{table}`"
        )
    return "\nUNION ALL\n".join(parts)


def build_value_cache(
    bq: Any, budget: TurnBudget, dataset: str, today: date | None = None
) -> ValueCache:
    result = bq.query(build_query(dataset), budget, purpose="value_cache")
    values: dict[str, tuple[str, ...]] = {}
    date_min: dict[str, str] = {}
    for tbl, data in zip(
        result.dataframe["tbl"], result.dataframe["data"], strict=True
    ):
        parsed = json.loads(data)
        for column in CACHED_COLUMNS[tbl]:
            values[f"{tbl}.{column}"] = tuple(sorted(parsed.get(column) or []))
        if parsed.get("min_created_at"):
            date_min[tbl] = parsed["min_created_at"]
    missing = set(CACHED_COLUMNS) - {k.split(".")[0] for k in values}
    if missing:
        raise AgentError(
            ErrorType.INTERNAL_ERROR,
            "The value cache could not be built.",
            f"no rows for tables {sorted(missing)}",
        )
    built = (today or datetime.now(UTC).date()).isoformat()
    return ValueCache(built_at=built, values=values, date_min=date_min)


def load_value_cache(
    path: Path,
    bq: Any,
    budget: TurnBudget,
    dataset: str,
    refresh: bool = False,
    today: date | None = None,
) -> ValueCache:
    """Read the cache file. Build it again when it is missing, from an earlier day, or on request."""
    if path.exists() and not refresh:
        try:
            cache = ValueCache.from_json(path.read_text(encoding="utf-8"))
        except (ValueError, KeyError):
            cache = None  # a broken file is built again
        if cache is not None and not cache.is_stale(today):
            return cache
    cache = build_value_cache(bq, budget, dataset, today)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.to_json(), encoding="utf-8")
    return cache


# --- Brand names in users.yaml (spec 30, B2.8) ----------------------------------------------------


@dataclass(frozen=True)
class BrandProblem:
    user_id: str
    brand: str
    close_names: tuple[str, ...]

    def message(self) -> str:
        text = f"Brand '{self.brand}' for user '{self.user_id}' does not exist in the data."
        if self.close_names:
            text += f" Close names: {', '.join(self.close_names)}."
        return text


def _loose(name: str) -> str:
    """For the suggestions only: no letter case, one kind of apostrophe, single spaces."""
    return " ".join(name.replace("’", "'").casefold().split())


def close_names(name: str, known: tuple[str, ...], limit: int = 3) -> tuple[str, ...]:
    loose = _loose(name)
    found = [k for k in known if _loose(k) == loose]
    found += difflib.get_close_matches(name, known, n=limit, cutoff=0.8)
    return tuple(dict.fromkeys(found))[:limit]


def validate_brands(users: UsersConfig, values: ValueCache) -> list[BrandProblem]:
    """Exact match, character by character: `'` and `’` are different characters."""
    known = set(values.brands)
    problems = []
    for user in users.users:
        if user.role is not Role.MANAGER:
            continue
        for brand in user.brands:
            if brand not in known:
                problems.append(
                    BrandProblem(user.id, brand, close_names(brand, values.brands))
                )
    return problems
