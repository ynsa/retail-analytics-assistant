"""Calendar periods that a question names with numbers: a year, a quarter, a half year or a month
with its year (spec 31, B7 note "A named year became last year").

Code reads these periods from the question, so that:

1. the `<calendar>` block of `write_sql` gives the exact dates and the SQL condition of each one,
   in the first lines. The Docker check of 2026-11-12 showed the cause: for "How many orders were
   placed in 2024?" the model copied the `last_year` line (2025) of the calendar.
2. the period check (`agent/sql_checks/period_check.py`) can compare the stated period with the
   years of the question.

Relative words ("last month", "this year") stay with the named periods of `Calendar`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from opsfleet_agent.agent.periods import Period

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_WORD = (
    r"(january|february|march|april|may|june|july|august|september|october|november|"
    r"december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
)
_YEAR = r"((?:19|20)\d{2})"
_QUARTER = re.compile(
    rf"\bq([1-4])\s*(?:of\s+)?{_YEAR}\b|\b{_YEAR}\s*q([1-4])\b", re.IGNORECASE
)
_HALF = re.compile(
    rf"\bh([12])\s*(?:of\s+)?{_YEAR}\b|\b{_YEAR}\s*h([12])\b", re.IGNORECASE
)
_MONTH = re.compile(rf"\b{_MONTH_WORD}\.?,?\s+(?:of\s+)?{_YEAR}\b", re.IGNORECASE)
_ISO_MONTH = re.compile(rf"\b{_YEAR}-(0[1-9]|1[0-2])\b(?!-\d)")
_ISO_DAY = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
_PLAIN_YEAR = re.compile(rf"(?<![\d$#.,]){_YEAR}\b(?![\d%]|\.\d|,\d)")
# A number such as "top 2000" or "2020 orders" is not a year.
_COUNT_BEFORE = re.compile(
    r"\b(?:top|first|bottom|over|above|below|under)\s*$", re.IGNORECASE
)
_COUNT_AFTER = re.compile(
    r"^\s*(?:usd|dollars?|orders?|items?|products?|customers?|users?|rows?|brands?|units?)\b",
    re.IGNORECASE,
)
# A year after one of these words is a bound ("before 2020"), not the period of the question.
_BOUND_BEFORE = re.compile(
    r"\b(?:before|after|until|till|since|prior\s+to|earlier\s+than|later\s+than|"
    r"older\s+than|newer\s+than)\s+(?:the\s+)?(?:start\s+of\s+|end\s+of\s+)?$",
    re.IGNORECASE,
)
_CONTEXT_CHARS = 30


@dataclass(frozen=True)
class NamedPeriod:
    label: str  # the words of the question, for example "Q3 2025" or "2024"
    period: Period
    bound: bool = False  # "before 2020", "since 2023": a limit, not the asked period


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _quarter(year: int, quarter: int) -> Period:
    return Period(date(year, 3 * quarter - 2, 1), _month_end(year, 3 * quarter))


def _month(year: int, month: int) -> Period:
    return Period(date(year, month, 1), _month_end(year, month))


def _year(year: int) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31))


def _is_bound(text: str, start: int) -> bool:
    return bool(_BOUND_BEFORE.search(text[max(0, start - _CONTEXT_CHARS) : start]))


def named_periods(question: str | None, today: date) -> list[NamedPeriod]:
    """The periods that the question names with numbers, in the order of the text. Years after
    today's year and before 2000 are not periods of this data."""
    if not question:
        return []
    text = question
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, NamedPeriod]] = []

    def add(match: re.Match[str], period: Period) -> None:
        taken.append(match.span())
        label = " ".join(match.group(0).split())
        found.append(
            (match.start(), NamedPeriod(label, period, _is_bound(text, match.start())))
        )

    for match in _ISO_DAY.finditer(text):
        taken.append(match.span())  # a full date is exact already
    for match in _QUARTER.finditer(text):
        q, year = (
            (match.group(1), match.group(2))
            if match.group(1)
            else (
                match.group(4),
                match.group(3),
            )
        )
        add(match, _quarter(int(year), int(q)))
    for match in _HALF.finditer(text):
        h, year = (
            (match.group(1), match.group(2))
            if match.group(1)
            else (
                match.group(4),
                match.group(3),
            )
        )
        start_month = 1 if h == "1" else 7
        add(
            match,
            Period(
                date(int(year), start_month, 1), _month_end(int(year), start_month + 5)
            ),
        )
    for match in _MONTH.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        add(match, _month(int(match.group(2)), _MONTHS[match.group(1).lower()[:3]]))
    for match in _ISO_MONTH.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        add(match, _month(int(match.group(1)), int(match.group(2))))
    for match in _PLAIN_YEAR.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        before, after = text[: match.start()], text[match.end() :]
        if _COUNT_BEFORE.search(before) or _COUNT_AFTER.search(after):
            continue
        add(match, _year(int(match.group(1))))
    periods = [
        p
        for _, p in sorted(found, key=lambda item: item[0])
        if 2000 <= p.period.start.year <= today.year
    ]
    unique: dict[tuple[date, date, bool], NamedPeriod] = {}
    for p in periods:
        unique.setdefault((p.period.start, p.period.end, p.bound), p)
    return list(unique.values())


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)
