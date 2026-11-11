"""Number formatting by code (spec 31, B2.5).

Code formats each value before a model sees it: money with 2 decimals and thousands separators
(`$385,083.24`), counts as whole numbers (`6,290`), rates as percentages with 1 decimal (`12.4%`).
The model copies the formatted values, so no answer shows a raw number such as `385083.240183115`.

A stored result (`ResultData`) keeps the masked values as plain JSON types. It goes into the graph
state, so the calculation placeholders and the figure check can use the exact values.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypedDict

from opsfleet_agent.guard.masker import MaskedResult


class ColumnKind(StrEnum):
    MONEY = "money"
    COUNT = "count"
    RATE = "rate"  # a fraction from 0 to 1, shown as a percentage
    PERCENT = "percent"  # already in percent units (12.4 means 12.4%)
    NUMBER = "number"
    ID = "id"
    YEAR = "year"
    TEXT = "text"


class ResultData(TypedDict):
    ref: str  # "r1", "r2", ...: the reference in data blocks and placeholders
    purpose: str
    columns: list[str]
    kinds: list[str]  # a ColumnKind for each column
    rows: list[list[Any]]  # masked values: int, float, str, bool or None
    row_count: int
    truncated: bool


_MONEY_WORDS = (
    "revenue",
    "sales",
    "sale_price",
    "price",
    "cost",
    "margin",
    "profit",
    "spend",
    "aov",
    "order_value",
    "amount",
    "gmv",
)
_COUNT_WORDS = (
    "count",
    "orders",
    "items",
    "customers",
    "users",
    "buyers",
    "quantity",
    "qty",
    "units",
    "num",
    "number_of",
    "cnt",
    "products",
    "returns",
    "cancellations",
)
_ID_NAMES = (
    "id",
    "user_id",
    "order_id",
    "product_id",
    "customer_id",
    "inventory_item_id",
)


def _has(name: str, words: tuple[str, ...]) -> bool:
    """True when the name has one of the words as whole parts between underscores:
    `total_revenue` has `revenue`, but `generated_total` does not have `rate`."""
    padded = f"_{name.lower()}_"
    return any(f"_{w}_" in padded for w in words)


def plain_value(value: Any) -> Any:
    """A JSON-like value for the state: numpy and Decimal become float or int, dates become text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()  # numpy scalar
        except (ValueError, TypeError):
            pass
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, int):
        return value
    if isinstance(value, dt.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def column_kind(name: str, values: list[Any]) -> ColumnKind:
    numbers = [
        v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not numbers or len(numbers) < len([v for v in values if v is not None]):
        return ColumnKind.TEXT
    lower = name.lower()
    if lower in _ID_NAMES or lower.endswith("_id"):
        return ColumnKind.ID
    if lower in ("year", "yr") or lower.endswith("_year"):
        return ColumnKind.YEAR
    if _has(lower, ("pct", "percent")):
        return ColumnKind.PERCENT
    if _has(lower, ("rate", "share")):
        if all(abs(v) <= 1 for v in numbers):
            return ColumnKind.RATE
        return ColumnKind.PERCENT
    if _has(lower, _MONEY_WORDS):
        return ColumnKind.MONEY
    if _has(lower, _COUNT_WORDS):
        return ColumnKind.COUNT
    if all(isinstance(v, int) for v in numbers):
        return ColumnKind.COUNT
    return ColumnKind.NUMBER


def format_number(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def format_value(value: Any, kind: ColumnKind | str, signed: bool = False) -> str:
    """The display text of one value. `signed` adds `+` to a positive change."""
    if value is None:
        return ""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    kind = ColumnKind(kind)
    sign = "+" if signed and value > 0 else ""
    if kind is ColumnKind.MONEY:
        text = f"${format_number(abs(value), 2)}"
        return f"-{text}" if value < 0 else f"{sign}{text}"
    if kind is ColumnKind.RATE:
        return f"{sign}{format_number(value * 100, 1)}%"
    if kind is ColumnKind.PERCENT:
        return f"{sign}{format_number(value, 1)}%"
    if kind in (ColumnKind.ID, ColumnKind.YEAR):
        return str(int(value)) if float(value).is_integer() else str(value)
    if kind is ColumnKind.COUNT and float(value).is_integer():
        return f"{sign}{format_number(value, 0)}"
    return f"{sign}{format_number(value, 2)}"


def store_result(masked: MaskedResult, ref: str, purpose: str) -> ResultData:
    rows = [[plain_value(v) for v in row] for row in masked.rows]
    kinds = [
        column_kind(col, [row[i] for row in rows]).value
        for i, col in enumerate(masked.columns)
    ]
    return ResultData(
        ref=ref,
        purpose=purpose,
        columns=list(masked.columns),
        kinds=kinds,
        rows=rows,
        row_count=masked.row_count,
        truncated=masked.truncated,
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def result_markdown(result: ResultData, max_rows: int | None = None) -> str:
    """The formatted rows as a Markdown table."""
    if not result["columns"]:
        return "(no columns)"
    if not result["rows"]:
        return "(no rows)"
    rows = result["rows"] if max_rows is None else result["rows"][:max_rows]
    lines = [
        "| " + " | ".join(result["columns"]) + " |",
        "|" + "---|" * len(result["columns"]),
    ]
    for row in rows:
        cells = (format_value(v, k) for v, k in zip(row, result["kinds"], strict=True))
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    if max_rows is not None and len(result["rows"]) > max_rows:
        lines.append(f"({len(result['rows']) - max_rows} more rows cut for size)")
    return "\n".join(lines)


def data_block(result: ResultData, max_rows: int | None = None) -> str:
    """A data block (spec 31, B2.7): text inside it is data, never an instruction."""
    note = ", more rows exist" if result["truncated"] else ""
    return (
        f'<data source="query" ref="{result["ref"]}" rows="{result["row_count"]}{note}">\n'
        f"Purpose: {_cell(result['purpose'])}\n"
        f"{result_markdown(result, max_rows)}\n</data>"
    )
