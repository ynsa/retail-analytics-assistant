"""The period check: does the SQL filter the period that the answer will state? (spec 31, B7)

The stated period is `active_filters.period` of the `write_sql` output (the footer shows it). A
comparison period in `active_filters.other` ("compared with 2025-10-01..2025-10-31") is also
stated. The check uses the date columns that carry the period: the sale date columns
(`order_items.created_at`, `orders.created_at`) when the SQL filters them, else every date column
that the SQL filters (for example `users.created_at` for sign-ups).

A clear mismatch gives feedback for the model:

| Code | Rule |
|---|---|
| `outside` | A range of the SQL is outside the stated periods. One period of the same length (at least a year) before them is allowed, for a comparison with the previous period. |
| `not_covered` | The SQL ranges leave out a part of the main period. Only when the code can read all filters of the column. |
| `no_end` | The main period ends before today, and the SQL range has no end date. |
| `no_date_filter` | The SQL has no date filter at all, but a period is stated. |
| `question` | The question names a year (or a quarter, a month), and the stated period is in none of them. |

When the code cannot read a filter, the result is `unverified`: the query runs, and the span has the
attribute. No period stated and readable SQL ranges: the footer shows the period of the SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from opsfleet_agent.agent.periods import Period, parse_periods, span_of
from opsfleet_agent.agent.question_periods import NamedPeriod
from opsfleet_agent.agent.sql_checks.date_filters import (
    BaseColumn,
    ColumnRanges,
    DateFilters,
)
from opsfleet_agent.agent.sql_checks.ranges import (
    Interval,
    at,
    covers,
    hull,
    interval_text,
    of_period,
    overlaps,
    to_period,
)

SALE_DATE_COLUMNS: tuple[BaseColumn, ...] = (
    ("order_items", "created_at"),
    ("orders", "created_at"),
)
DATED_TABLES = frozenset({"order_items", "orders", "users"})
MIN_LOOKBACK = timedelta(days=365)  # a comparison with the same period a year before


@dataclass(frozen=True)
class PeriodVerdict:
    status: str  # match, mismatch, unverified, no_period
    code: str = ""  # the rule of a mismatch, or the reason of "unverified"
    values: dict[str, str] = field(
        default_factory=dict
    )  # the values of the feedback text
    period: Period | None = None  # the period that the footer may show
    sql_ranges: tuple[str, ...] = ()  # the SQL ranges of the period columns, as text


@dataclass(frozen=True)
class StatedPeriods:
    main: list[Period]
    other: list[Period]

    @classmethod
    def of_plan(cls, plan: dict) -> StatedPeriods:
        filters = plan.get("active_filters") or {}
        other = filters.get("other") or []
        return cls(
            main=parse_periods(filters.get("period")),
            other=parse_periods(" ".join(str(o) for o in other)),
        )


def period_columns(filters: DateFilters) -> list[BaseColumn]:
    sale = [c for c in SALE_DATE_COLUMNS if c in filters.columns]
    return sale or sorted(filters.columns)


def _ranges_for_cover(ranges: ColumnRanges) -> list[Interval]:
    """The row filters, or the conditional filters when the column has no row filter."""
    chosen = ranges.rows or ranges.conditions
    return [i for r in chosen for i in r]


def _closed_cover(filters: DateFilters, needed: Interval) -> bool:
    """True when some date column (any, for example `delivered_at`, or the other sale date
    column) has closed ranges that cover the period: the open end of one column is then not a
    problem."""
    for ranges in filters.columns.values():
        closed = [i for i in _ranges_for_cover(ranges) if i.closed]
        if closed and covers(closed, needed):
            return True
    return False


def _needed(period: Period, first: date | None, today: date) -> Interval | None:
    """The part of a period that has data: not before the first day, not after today."""
    start = max(period.start, first) if first else period.start
    end = min(period.end_exclusive, today + timedelta(days=1))
    return Interval(at(start), at(end)) if start < end else None


def _text(period: Period) -> str:
    return f"{period.text()} ({period.start.isoformat()}..{period.end.isoformat()})"


def _sql_condition(column: BaseColumn, period: Period) -> str:
    alias = "oi" if column[0] == "order_items" else column[0]
    return (
        f"{alias}.{column[1]} >= TIMESTAMP('{period.start.isoformat()}') AND "
        f"{alias}.{column[1]} < TIMESTAMP('{period.end_exclusive.isoformat()}')"
    )


def _question_mismatch(
    named: list[NamedPeriod], stated: list[Period]
) -> NamedPeriod | None:
    """The first named period, when the stated periods touch none of the named ones."""
    asked = [n for n in named if not n.bound]
    if not asked or not stated:
        return None
    for item in asked:
        if any(overlaps(of_period(item.period), of_period(p)) for p in stated):
            return None
    return asked[0]


def check_period(
    plan: dict,
    filters: DateFilters | None,
    tables: frozenset[str],
    named: list[NamedPeriod],
    today: date,
    first: date | None,
) -> PeriodVerdict:
    stated = StatedPeriods.of_plan(plan)
    main = span_of(stated.main)
    if filters is None:
        return PeriodVerdict("unverified", "sql_not_readable", period=main)
    columns = period_columns(filters)
    known = [i for c in columns for i in filters.columns[c].known()]
    sql_texts = tuple(interval_text(i) for i in known)
    readable = not filters.opaque and not any(
        filters.columns[c].unknown for c in columns
    )

    if main is None:
        derived = None
        whole = hull(known) if readable and known else None
        if whole is not None:
            derived = to_period(whole, first or today, today)
        asked = _question_mismatch(named, [derived] if derived else [])
        if asked and derived:
            return _question_verdict(asked, derived, derived, sql_texts)
        status = "match" if derived else "no_period"
        return PeriodVerdict(
            status, "from_sql" if derived else "", period=derived, sql_ranges=sql_texts
        )

    asked = _question_mismatch(named, stated.main + stated.other)
    if asked:
        return _question_verdict(asked, main, main, sql_texts)

    everything = hull([of_period(p) for p in stated.main + stated.other])
    assert everything is not None and everything.start is not None
    main_length = of_period(main).end - of_period(main).start  # type: ignore[operator]
    allowed = Interval(
        everything.start - max(main_length, MIN_LOOKBACK), everything.end
    )
    for column in columns:
        for interval in filters.columns[column].known():
            if not overlaps(interval, allowed):
                return _verdict("outside", main, column, interval, sql_texts)

    if not filters.seen:
        needed = _needed(main, first, today)
        whole_data = needed is None or (
            (first is not None and main.start <= first) and main.end >= today
        )
        if tables & DATED_TABLES and not whole_data:
            return PeriodVerdict(
                "mismatch",
                "no_date_filter",
                {
                    "period": _text(main),
                    "condition": _sql_condition(SALE_DATE_COLUMNS[0], main),
                },
                main,
                sql_texts,
            )
        return PeriodVerdict("unverified", "no_date_filter_needed", period=main)

    if not readable or not columns:  # no columns: an `OR` that does not limit the dates
        return PeriodVerdict(
            "unverified", "filter_not_readable", period=main, sql_ranges=sql_texts
        )

    for period in stated.main:
        needed = _needed(period, first, today)
        if needed is None:
            continue
        if not any(
            covers(_ranges_for_cover(filters.columns[c]), needed) for c in columns
        ):
            column = columns[0]
            ranges = _ranges_for_cover(filters.columns[column])
            shown = hull(ranges) or Interval(None, None)
            return _verdict("not_covered", period, column, shown, sql_texts)
        if period.end_exclusive <= today and not _closed_cover(filters, needed):
            for column in columns:
                ranges = filters.columns[column]
                if ranges.conditions or not ranges.rows:
                    continue
                cover = [i for i in _ranges_for_cover(ranges) if overlaps(i, needed)]
                if cover and all(i.end is None for i in cover):
                    return _verdict("no_end", period, column, cover[0], sql_texts)
    return PeriodVerdict("match", period=main, sql_ranges=sql_texts)


def _verdict(
    code: str,
    period: Period,
    column: BaseColumn,
    interval: Interval,
    sql_texts: tuple[str, ...],
) -> PeriodVerdict:
    return PeriodVerdict(
        "mismatch",
        code,
        {
            "period": _text(period),
            "column": ".".join(column),
            "sql_range": interval_text(interval),
            "condition": _sql_condition(column, period),
            "end": period.end_exclusive.isoformat(),
        },
        period,
        sql_texts,
    )


def _question_verdict(
    asked: NamedPeriod, stated: Period, period: Period, sql_texts: tuple[str, ...]
) -> PeriodVerdict:
    return PeriodVerdict(
        "mismatch",
        "question",
        {
            "named": asked.label,
            "named_period": _text(asked.period),
            "period": _text(stated),
            "condition": _sql_condition(SALE_DATE_COLUMNS[0], asked.period),
        },
        period,
        sql_texts,
    )
