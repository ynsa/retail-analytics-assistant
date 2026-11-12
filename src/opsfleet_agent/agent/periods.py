"""Calendar periods for the prompts and the footer (spec 31, B2.6 and B2.9).

Code computes the dates of "last month", "last quarter" and similar words, so the model copies
exact dates and never does date arithmetic. "Today" is the current UTC date (gap analysis N4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
# Words that state a period in a question (spec 31, B7 note of 2026-11-12). A year, a month name,
# a quarter, a half year, a date, or a relative period such as "last month" or "this year".
_MONTH_NAMES = (
    "january|february|march|april|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_STATED_PERIOD = re.compile(
    r"\b(?:19|20)\d{2}\b"
    rf"|\b(?:{_MONTH_NAMES})\b|\bin may\b|\bmay\s+(?:\d|to\b)"
    r"|\bq[1-4]\b|\bh[12]\b|\b(?:first|second) half\b|\bquarter\b"
    r"|\b(?:last|this|previous|past|current|next)\s+\w*(?:day|week|month|year|quarter)s?\b"
    r"|\b(?:today|yesterday|ytd|mtd|qtd)\b"
    r"|\d{4}-\d{2}-\d{2}",
    re.IGNORECASE,
)
_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to|/|–|—)\s*(\d{4}-\d{2}-\d{2})")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)

    def text(self) -> str:
        return format_range(self.start, self.end)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _previous_month(start: date) -> Period:
    end = start - timedelta(days=1)
    return Period(end.replace(day=1), end)


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


@dataclass(frozen=True)
class Calendar:
    today: date

    @classmethod
    def now(cls) -> Calendar:
        return cls(datetime.now(UTC).date())

    @property
    def last_month(self) -> Period:
        return _previous_month(_month_start(self.today))

    @property
    def month_before_last(self) -> Period:
        return _previous_month(self.last_month.start)

    @property
    def this_month(self) -> Period:
        return Period(_month_start(self.today), self.today)

    @property
    def this_quarter(self) -> Period:
        return Period(_quarter_start(self.today), self.today)

    @property
    def last_quarter(self) -> Period:
        end = _quarter_start(self.today) - timedelta(days=1)
        return Period(_quarter_start(end), end)

    @property
    def this_year(self) -> Period:
        return Period(date(self.today.year, 1, 1), self.today)

    @property
    def last_year(self) -> Period:
        year = self.today.year - 1
        return Period(date(year, 1, 1), date(year, 12, 31))

    def named(self) -> dict[str, Period]:
        return {
            "last_month": self.last_month,
            "month_before_last_month": self.month_before_last,
            "this_month_so_far": self.this_month,
            "this_quarter_so_far": self.this_quarter,
            "last_quarter": self.last_quarter,
            "this_year_so_far": self.this_year,
            "last_year": self.last_year,
        }


def format_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def format_range(start: date, end: date) -> str:
    """`1–31 Oct 2026`, `1 Sep – 31 Oct 2026` or `1 Dec 2025 – 31 Jan 2026`."""
    if start == end:
        return format_day(start)
    if start.year != end.year:
        return f"{format_day(start)} – {format_day(end)}"
    if start.month != end.month:
        return f"{start.day} {MONTHS[start.month - 1]} – {format_day(end)}"
    return f"{start.day}–{format_day(end)}"


def states_period(text: str | None) -> bool:
    """True when the text names a period: a year, a month, a quarter, a date or "last month"."""
    return bool(text and _STATED_PERIOD.search(text))


def parse_periods(text: str | None) -> list[Period]:
    """Periods in a text such as `2026-09-01..2026-10-31`. A single date is a one-day period."""
    if not text:
        return []
    found: list[Period] = []
    rest = text
    for match in _RANGE.finditer(text):
        try:
            start, end = (date.fromisoformat(match.group(i)) for i in (1, 2))
        except ValueError:
            continue
        found.append(Period(min(start, end), max(start, end)))
        rest = rest.replace(match.group(0), " ")
    for match in _DAY.finditer(rest):
        try:
            day = date.fromisoformat(match.group(0))
        except ValueError:
            continue
        found.append(Period(day, day))
    return found


def span_of(periods: list[Period]) -> Period | None:
    if not periods:
        return None
    return Period(min(p.start for p in periods), max(p.end for p in periods))
