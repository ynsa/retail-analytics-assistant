"""Time ranges for the period check: half-open intervals of UTC instants, `None` for no limit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from opsfleet_agent.agent.periods import Period

TICK = timedelta(microseconds=1)  # TIMESTAMP precision: `x <= t` is `x < t + 1 µs`
EDGE_TOLERANCE = timedelta(
    seconds=1
)  # `< 23:59:59` at the end of a period still covers it


@dataclass(frozen=True)
class Interval:
    start: datetime | None  # included; None: no lower limit
    end: datetime | None  # not included; None: no upper limit

    @property
    def empty(self) -> bool:
        return (
            self.start is not None and self.end is not None and self.start >= self.end
        )

    @property
    def closed(self) -> bool:
        return self.start is not None and self.end is not None


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def of_period(period: Period) -> Interval:
    return Interval(at(period.start), at(period.end_exclusive))


def _max(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    return a if b is None else max(a, b)


def _min(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    return a if b is None else min(a, b)


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out = [Interval(_max(x.start, y.start), _min(x.end, y.end)) for x in a for y in b]
    return merge([i for i in out if not i.empty])


def merge(intervals: list[Interval], gap: timedelta = timedelta(0)) -> list[Interval]:
    """Join intervals that overlap or touch (or have a gap of at most `gap`)."""
    items = sorted(
        (i for i in intervals if not i.empty),
        key=lambda i: (i.start is not None, i.start or datetime.min),
    )
    out: list[Interval] = []
    for item in items:
        if out:
            last = out[-1]
            if last.end is None or item.start is None or item.start <= last.end + gap:
                end = (
                    None
                    if last.end is None or item.end is None
                    else max(last.end, item.end)
                )
                out[-1] = Interval(last.start, end)
                continue
        out.append(item)
    return out


def overlaps(a: Interval, b: Interval) -> bool:
    return not Interval(_max(a.start, b.start), _min(a.end, b.end)).empty


def covers(ranges: list[Interval], needed: Interval) -> bool:
    """True when the ranges together hold all of `needed` (one second of tolerance at a seam)."""
    for item in merge(ranges, EDGE_TOLERANCE):
        start_ok = item.start is None or (
            needed.start is not None and item.start <= needed.start + EDGE_TOLERANCE
        )
        end_ok = item.end is None or (
            needed.end is not None and item.end >= needed.end - EDGE_TOLERANCE
        )
        if start_ok and end_ok:
            return True
    return False


def hull(intervals: list[Interval]) -> Interval | None:
    if not intervals:
        return None
    starts = [i.start for i in intervals]
    ends = [i.end for i in intervals]
    start = None if None in starts else min(s for s in starts if s is not None)
    end = None if None in ends else max(e for e in ends if e is not None)
    return Interval(start, end)


def _moment(value: datetime) -> str:
    if value.time() == datetime.min.time():
        return value.date().isoformat()
    return value.isoformat(sep=" ")


def interval_text(interval: Interval) -> str:
    """`2025-01-01 to 2026-01-01` (the end is not included), `2024-01-01 with no end date`."""
    if interval.start is None and interval.end is None:
        return "all dates"
    if interval.start is None:
        return f"all dates before {_moment(interval.end)}"  # type: ignore[arg-type]
    if interval.end is None:
        return f"{_moment(interval.start)} with no end date"
    if interval.end.microsecond == TICK.microseconds:  # `<= TIMESTAMP('…')`
        last = (interval.end - TICK).isoformat(sep=" ")
        return f"{_moment(interval.start)} to {last} (included)"
    return f"{_moment(interval.start)} to {_moment(interval.end)}"


def to_period(interval: Interval, first: date, today: date) -> Period | None:
    """The days of an interval for the footer. An open start is the first day of the data, an
    open end is today."""
    start = interval.start.date() if interval.start else first
    if interval.end is None:
        end = today
    else:
        last = interval.end - TICK
        end = min(last.date(), today)
    if end < start:
        return None
    return Period(start, end)
