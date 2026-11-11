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
