"""Normalize result values before a comparison (spec 21, B2.6, step 1).

Text: trimmed and case-folded. Dates: to the granularity of the column (day, month, quarter, year).
Numbers: float. The agent can write a month as a date, a timestamp, "2025-01", "January 2025" or a
month number, so the parser accepts all of these.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
TEXT_ALIASES = {"f": "female", "m": "male"}
_ISO = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?")
_YYYYMM = re.compile(r"^(\d{4})(\d{2})$")
_QUARTER = re.compile(
    r"(?:(\d{4})\s*[-/ ]?\s*q([1-4]))|(?:q([1-4])\s*[-/ ]?\s*(\d{4}))"
)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

TIME_KINDS = ("date", "month", "quarter", "year")
NUMBER_KINDS = ("money", "count", "ratio", "number")


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def to_float(value: Any) -> float | None:
    if is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if hasattr(value, "item"):
        return to_float(value.item())
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _date_parts(value: Any) -> tuple[int, int | None, int | None] | None:
    """(year, month, day) of a date-like value, or None."""
    if isinstance(value, dt.datetime | dt.date):
        return value.year, value.month, value.day
    if hasattr(value, "to_pydatetime"):
        d = value.to_pydatetime()
        return d.year, d.month, d.day
    text = str(value).strip().lower()
    m = _ISO.match(text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    m = _YYYYMM.match(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2)), None
    words = re.findall(r"[a-z]+", text)
    month = next((MONTHS[w] for w in words if w in MONTHS), None)
    year_m = _YEAR.search(text)
    if month and year_m:
        return int(year_m.group(0)), month, None
    return None


def norm_time(value: Any, kind: str, context_year: int | None = None) -> str | None:
    """A date, month, quarter or year as text: 2025-01-31, 2025-01, 2025-Q1, 2025."""
    if is_missing(value):
        return None
    if kind == "year":
        number = to_float(value)
        if number is not None and 1900 <= number <= 2100:
            return str(int(number))
        parts = _date_parts(value)
        return str(parts[0]) if parts else None
    if kind == "quarter":
        text = str(value).strip().lower()
        m = _QUARTER.search(text)
        if m:
            year = m.group(1) or m.group(4)
            q = m.group(2) or m.group(3)
            return f"{year}-Q{q}"
        number = to_float(value)
        if (
            number is not None
            and context_year
            and 1 <= number <= 4
            and number == int(number)
        ):
            return f"{context_year}-Q{int(number)}"
        parts = _date_parts(value)
        if parts and parts[1]:
            return f"{parts[0]}-Q{(parts[1] - 1) // 3 + 1}"
        return None
    if kind == "month":
        number = to_float(value)
        if (
            number is not None
            and context_year
            and 1 <= number <= 12
            and number == int(number)
        ):
            return f"{context_year}-{int(number):02d}"
        text = str(value).strip().lower()
        if context_year and text in MONTHS:
            return f"{context_year}-{MONTHS[text]:02d}"
        parts = _date_parts(value)
        if parts and parts[1]:
            return f"{parts[0]}-{parts[1]:02d}"
        return None
    parts = _date_parts(value)  # kind == "date"
    if parts and parts[1] and parts[2]:
        return f"{parts[0]}-{parts[1]:02d}-{parts[2]:02d}"
    return None


def norm_key(
    value: Any, kind: str = "text", context_year: int | None = None
) -> str | None:
    """A key value as comparable text."""
    if is_missing(value):
        return None
    if kind in TIME_KINDS:
        return norm_time(value, kind, context_year)
    if kind == "id":
        number = to_float(value)
        if number is not None and number == int(number):
            return str(int(number))
        return str(value).strip().casefold()
    text = " ".join(str(value).split()).casefold()
    return TEXT_ALIASES.get(text, text)


def context_year(values: list[str | None], kind: str) -> int | None:
    """The single year of normalized month or quarter keys, if all keys share one year."""
    if kind not in ("month", "quarter"):
        return None
    years = {v[:4] for v in values if v}
    return int(years.pop()) if len(years) == 1 else None
