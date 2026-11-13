"""The date ranges that a query filters, read with sqlglot (spec 31, B7: the period check).

Input: the qualified SQL of the guard (the model's SQL before the scope rewrite, each column bound
to its table). Output: for each date column of a base table, the ranges of each filter.

- A **row filter** is a `WHERE`, `HAVING`, `QUALIFY` or `JOIN … ON` condition.
- A **conditional filter** is the condition of `CASE WHEN`, `IF` or `COUNTIF`.
- Each filter gives a union of half-open intervals. `AND` intersects them, `OR` joins them.

The forms that the code reads, on a column, also through CTEs and subqueries:
`>=`, `>`, `<`, `<=`, `=`, `BETWEEN`, `IN` with `TIMESTAMP('…')`, `DATE '…'`, `'…'` and `CAST`
literals; `DATE(x)`, `CAST(x AS DATE)`; `DATE_TRUNC`, `TIMESTAMP_TRUNC`, `DATETIME_TRUNC` by day,
month, quarter or year; `EXTRACT(YEAR | QUARTER | MONTH | DATE FROM x)`; `FORMAT_DATE` and
`FORMAT_TIMESTAMP` with `%Y`, `%Y-%m` or `%Y-%m-%d`.

Another condition on a date column (`CURRENT_DATE()`, `DATE_DIFF`, `NOT`, `!=`) makes the column
`unknown`. A date column inside an aggregate (`MAX(created_at) < …`) makes the query `opaque`. The
period check then passes with the result "cannot verify", never with a block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from opsfleet_agent.agent.sql_checks.ranges import TICK, Interval, intersect, merge
from opsfleet_agent.guard.lineage import Lineage
from opsfleet_agent.schema import Schema

BaseColumn = tuple[str, str]
DATE_TYPES = frozenset({"TIMESTAMP", "DATE", "DATETIME"})
_UNITS = ("instant", "day", "month", "quarter", "year")
_TRUNC_UNITS = {"DAY": "day", "MONTH": "month", "QUARTER": "quarter", "YEAR": "year"}
_FORMATS = {"%Y": "year", "%Y-%m": "month", "%Y-%m-%d": "day"}
_PARTS = {"MONTH": "month_of_year", "QUARTER": "quarter_of_year"}
_KEEP_VALUE = (
    exp.Timestamp,
    exp.Datetime,
    exp.TsOrDsToDatetime,
    exp.TsOrDsToTimestamp,
)
_TO_DAY = (exp.Date, exp.TsOrDsToDate)
_TRUNC = (exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)
_COMPARE = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "!=": "!="}
_MAX_DEPTH = 30
_MAX_MONTHS = 1200
_DAY_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


class _Unknown:
    """A condition on a date column that the code cannot read."""


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class Side:
    """A date expression: the base column and what the expression keeps of it."""

    column: BaseColumn
    unit: str  # instant, day, month, quarter, year, month_of_year, quarter_of_year


@dataclass(frozen=True)
class _Opaque:
    columns: frozenset[BaseColumn]


@dataclass(frozen=True)
class Constraint:
    intervals: tuple[Interval, ...] | None = None  # None: no limit
    months: frozenset[int] | None = None  # the allowed months of the year; None: all


Value = Constraint | _Unknown


@dataclass
class ColumnRanges:
    rows: list[list[Interval]] = field(default_factory=list)
    conditions: list[list[Interval]] = field(default_factory=list)
    unknown: bool = False

    def known(self) -> list[Interval]:
        return [i for ranges in self.rows + self.conditions for i in ranges]


@dataclass
class DateFilters:
    columns: dict[BaseColumn, ColumnRanges] = field(default_factory=dict)
    seen: bool = False  # a condition on a date column exists (read or not)
    opaque: bool = False  # a date filter that the code cannot map to a column range

    def column(self, column: BaseColumn) -> ColumnRanges:
        return self.columns.setdefault(column, ColumnRanges())


# --- units and literals ---------------------------------------------------------------------------
def _coarser(a: str, b: str) -> str:
    return a if _UNITS.index(a) >= _UNITS.index(b) else b


def floor(value: datetime, unit: str) -> datetime:
    if unit == "instant":
        return value
    if unit == "day":
        return datetime(value.year, value.month, value.day)
    if unit == "month":
        return datetime(value.year, value.month, 1)
    if unit == "quarter":
        return datetime(value.year, 3 * ((value.month - 1) // 3) + 1, 1)
    return datetime(value.year, 1, 1)


def step(value: datetime, unit: str) -> datetime:
    """The start of the next bucket after the bucket that starts at `value`."""
    if unit == "instant":
        return value + TICK
    if unit == "day":
        return value + timedelta(days=1)
    months = {"month": 1, "quarter": 3, "year": 12}[unit]
    total = value.year * 12 + value.month - 1 + months
    return datetime(total // 12, total % 12 + 1, 1)


def literal_value(node: exp.Expression | None) -> int | str | None:
    """The value of a literal, also inside `TIMESTAMP('…')`, `DATE '…'` or `CAST('…' AS …)`."""
    if node is None:
        return None
    if isinstance(node, exp.Paren):
        return literal_value(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return str(node.this)
        text = str(node.this)
        return int(text) if text.isdigit() else None
    if isinstance(node, exp.Timestamp):
        zone = node.args.get("zone")
        if zone is not None and literal_value(zone) not in ("UTC", "Etc/UTC", "+00"):
            return None
        return literal_value(node.this)
    if isinstance(node, (exp.Cast, *_TO_DAY, exp.Datetime)):
        return literal_value(node.this)
    return None


def to_datetime(value: int | str | None) -> datetime | None:
    """`2024` and `'2024'` are 1 Jan 2024, `'2024-10'` is 1 Oct 2024, `'2024-10-05 13:00'` is that
    instant. Other values: None."""
    if isinstance(value, int):
        return datetime(value, 1, 1) if 1900 <= value <= 2200 else None
    if value is None:
        return None
    text = value.strip().removesuffix(" UTC").removesuffix("Z").removesuffix("+00")
    try:
        if re.fullmatch(r"\d{4}", text):
            return datetime(int(text), 1, 1)
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return datetime(int(text[:4]), int(text[5:7]), 1)
        if _DAY_TEXT.match(text):
            return datetime.fromisoformat(text.replace("T", " "))
    except ValueError:
        return None
    return None


def bucket_ranges(unit: str, op: str, value: datetime) -> list[Interval] | None:
    """The values of the column for `f(column) <op> value`, where `f` keeps `unit` of the column.
    None when the condition is not a range (a value that no bucket starts with, for `=`)."""
    low = floor(value, unit)
    aligned = low == value
    up = step(low, unit)
    if op == "=":
        return [Interval(value, up)] if aligned else None
    if op == ">=":
        return [Interval(value if aligned else up, None)]
    if op == ">":
        return [Interval(up, None)]
    if op == "<":
        return [Interval(None, value if aligned else up)]
    if op == "<=":
        return [Interval(None, up)]
    return None


def _part_months(unit: str, op: str, number: int) -> frozenset[int] | None:
    top = 12 if unit == "month_of_year" else 4
    if not 1 <= number <= top:
        return None
    allowed = {
        "=": {number},
        ">=": set(range(number, top + 1)),
        ">": set(range(number + 1, top + 1)),
        "<": set(range(1, number)),
        "<=": set(range(1, number + 1)),
    }.get(op)
    if allowed is None:
        return None
    if unit == "month_of_year":
        return frozenset(allowed)
    return frozenset(3 * q - k for q in allowed for k in (0, 1, 2))


# --- the reader -----------------------------------------------------------------------------------
class _Reader:
    def __init__(self, lineage: Lineage, schema: Schema) -> None:
        self.lineage = lineage
        self.schema = schema
        self.out = DateFilters()

    # sides
    def base_side(
        self, column: exp.Column, depth: int
    ) -> Side | _Unknown | _Opaque | None:
        if isinstance(column.this, exp.Star):
            return None
        source = self.lineage.source_of(column, self.lineage.scope_of(column))
        if isinstance(source, exp.Table):
            kind = (
                self.schema.column_type(source.name.lower(), column.name.lower()) or ""
            )
            if kind.upper() not in DATE_TYPES:
                return None
            unit = "day" if kind.upper() == "DATE" else "instant"
            return Side((source.name.lower(), column.name.lower()), unit)
        if not isinstance(source, Scope):
            return None
        sides = [
            self.side(projection.unalias(), depth + 1)
            for projection, _ in self.lineage.projections_of(column) or []
        ]
        found = [s for s in sides if s is not None]
        if not found:
            return None
        if all(s == found[0] for s in found) and len(found) == len(sides):
            return found[0]
        return UNKNOWN

    def side(
        self, node: exp.Expression, depth: int = 0
    ) -> Side | _Unknown | _Opaque | None:
        """What a date expression keeps of its column. None when it has no date column."""
        if depth > _MAX_DEPTH:
            return UNKNOWN
        if isinstance(node, exp.Paren):
            return self.side(node.this, depth + 1)
        if isinstance(node, exp.Column):
            return self.base_side(node, depth)
        columns = self.date_columns(node, depth)
        if not columns:
            return None
        if node.find(exp.AggFunc, exp.Window) is not None:
            return _Opaque(frozenset(columns))
        inner = self._inner(node)
        if inner is None:
            return UNKNOWN
        target, unit = inner
        sub = self.side(target, depth + 1)
        if not isinstance(sub, Side):
            return sub
        return self._combine(sub, unit)

    @staticmethod
    def _combine(sub: Side, unit: str | None) -> Side | _Unknown:
        if unit is None:
            return sub
        if sub.unit in ("month_of_year", "quarter_of_year"):
            return UNKNOWN
        if unit in ("month_of_year", "quarter_of_year"):
            return Side(sub.column, unit)
        return Side(sub.column, _coarser(sub.unit, unit))

    @staticmethod
    def _inner(node: exp.Expression) -> tuple[exp.Expression, str | None] | None:
        """The argument of a date function and the unit that the function keeps."""
        if isinstance(node, exp.Timestamp):
            zone = node.args.get("zone")
            if zone is not None and literal_value(zone) not in ("UTC", "Etc/UTC"):
                return None
            return node.this, None
        if isinstance(node, _KEEP_VALUE):
            return node.this, None
        if isinstance(node, _TO_DAY) and node.args.get("zone") is None:
            return node.this, "day"
        if isinstance(node, exp.Cast):
            kind = node.to.this if node.to else None
            if kind == exp.DataType.Type.DATE:
                return node.this, "day"
            if kind in (
                exp.DataType.Type.TIMESTAMP,
                exp.DataType.Type.TIMESTAMPTZ,
                exp.DataType.Type.DATETIME,
            ):
                return node.this, None
            if node.to.is_type(*exp.DataType.INTEGER_TYPES):
                return node.this, None  # CAST(FORMAT_DATE('%Y', …) AS INT64)
            return None
        if isinstance(node, _TRUNC):
            unit_node = node.args.get("unit")
            unit = (
                _TRUNC_UNITS.get(
                    str(literal_value(unit_node) or unit_node.name).upper()
                )
                if isinstance(unit_node, (exp.Literal, exp.Var))
                else None
            )
            return (node.this, unit) if unit else None
        if isinstance(node, exp.Extract):
            part = node.name.upper()
            if part == "YEAR":
                return node.expression, "year"
            if part == "DATE":
                return node.expression, "day"
            if part in _PARTS:
                return node.expression, _PARTS[part]
            return None
        if isinstance(node, exp.TimeToStr):
            unit = _FORMATS.get(str(literal_value(node.args.get("format")) or ""))
            return (node.this, unit) if unit else None
        return None

    def date_columns(self, node: exp.Expression, depth: int = 0) -> set[BaseColumn]:
        found: set[BaseColumn] = set()
        for column in node.find_all(exp.Column):
            side = self.base_side(column, depth + 1)
            if isinstance(side, Side):
                found.add(side.column)
            elif side is UNKNOWN:
                found |= self._deep_columns(column, depth + 1)
            elif isinstance(side, _Opaque):
                found |= side.columns
        return found

    def _deep_columns(self, column: exp.Column, depth: int) -> set[BaseColumn]:
        if depth > _MAX_DEPTH:
            return set()
        found: set[BaseColumn] = set()
        for projection, _ in self.lineage.projections_of(column) or []:
            found |= self.date_columns(projection, depth + 1)
        return found

    # conditions
    def atom(self, node: exp.Expression) -> dict[BaseColumn, Value]:
        if isinstance(node, exp.Between):
            low = self.compare(node.this, ">=", node.args.get("low"))
            high = self.compare(node.this, "<=", node.args.get("high"))
            return _and(low, high)
        if isinstance(node, exp.In):
            return self.in_list(node)
        op = _COMPARE.get(type(node))
        if op is not None:
            left, right = node.this, node.expression
            if self.side(left) is None and self.side(right) is not None:
                left, right, op = right, left, _FLIP[op]
            return self.compare(left, op, right)
        if isinstance(node, exp.Is):
            return {}  # `returned_at IS NOT NULL` is not a period
        return self.unknown(node)

    def unknown(self, node: exp.Expression) -> dict[BaseColumn, Value]:
        columns = self.date_columns(node)
        if columns:
            self.out.seen = True
        return dict.fromkeys(columns, UNKNOWN)

    def compare(
        self, left: exp.Expression, op: str, right: exp.Expression | None
    ) -> dict[BaseColumn, Value]:
        side = self.side(left)
        if side is None:
            return {}
        self.out.seen = True
        if isinstance(side, _Opaque):
            self.out.opaque = True
            return {}
        if right is not None and self.side(right) is not None:
            return {}  # a column against a column (`delivered_at > created_at`): not a period
        if side is UNKNOWN or right is None:
            return self.unknown(left)
        assert isinstance(side, Side)
        value = literal_value(right)
        if value is None or op == "!=":
            return {side.column: UNKNOWN}
        if side.unit in ("month_of_year", "quarter_of_year"):
            number = value if isinstance(value, int) else None
            months = _part_months(side.unit, op, number) if number is not None else None
            return {side.column: Constraint(months=months) if months else UNKNOWN}
        moment = to_datetime(value)
        if moment is None:
            return {side.column: UNKNOWN}
        ranges = bucket_ranges(side.unit, op, moment)
        if ranges is None:
            return {side.column: UNKNOWN}
        return {side.column: Constraint(intervals=tuple(ranges))}

    def in_list(self, node: exp.In) -> dict[BaseColumn, Value]:
        if node.args.get("query") or node.args.get("unnest"):
            return self.unknown(node.this)
        result: dict[BaseColumn, Value] | None = None
        for item in node.expressions:
            one = self.compare(node.this, "=", item)
            result = one if result is None else _or(result, one)
        return result or {}

    def evaluate(self, node: exp.Expression) -> dict[BaseColumn, Value]:
        if isinstance(node, exp.Paren):
            return self.evaluate(node.this)
        if isinstance(node, exp.And):
            return _and(self.evaluate(node.this), self.evaluate(node.expression))
        if isinstance(node, exp.Or):
            return _or(self.evaluate(node.this), self.evaluate(node.expression))
        if isinstance(node, exp.Not):
            inner = self.evaluate(node.this)
            return dict.fromkeys(inner, UNKNOWN)
        return self.atom(node)

    def read(self, tree: exp.Expression) -> DateFilters:
        for kind, condition in _roots(tree):
            for column, value in self.evaluate(condition).items():
                ranges = _final(value)
                target = self.out.column(column)
                if ranges is UNKNOWN:
                    target.unknown = True
                elif ranges is not None:
                    getattr(target, kind).append(ranges)
        return self.out


def _roots(tree: exp.Expression) -> list[tuple[str, exp.Expression]]:
    roots: list[tuple[str, exp.Expression]] = []
    for node in tree.walk():
        if isinstance(node, (exp.Where, exp.Having, exp.Qualify)):
            roots.append(("rows", node.this))
        elif isinstance(node, exp.Join) and node.args.get("on") is not None:
            roots.append(("rows", node.args["on"]))
        elif isinstance(node, (exp.If, exp.CountIf)) and node.this is not None:
            roots.append(("conditions", node.this))
    return roots


def _and_one(a: Value, b: Value) -> Value:
    if isinstance(a, _Unknown) or isinstance(b, _Unknown):
        return UNKNOWN
    if a.intervals is None:
        intervals = b.intervals
    elif b.intervals is None:
        intervals = a.intervals
    else:
        intervals = tuple(intersect(list(a.intervals), list(b.intervals)))
    if a.months is None:
        months = b.months
    elif b.months is None:
        months = a.months
    else:
        months = a.months & b.months
    return Constraint(intervals, months)


def _and(
    a: dict[BaseColumn, Value], b: dict[BaseColumn, Value]
) -> dict[BaseColumn, Value]:
    out = dict(a)
    for column, value in b.items():
        out[column] = _and_one(out[column], value) if column in out else value
    return out


def _or(
    a: dict[BaseColumn, Value], b: dict[BaseColumn, Value]
) -> dict[BaseColumn, Value]:
    """A column that only one branch limits is not limited by the `OR`."""
    out: dict[BaseColumn, Value] = {}
    for column in a.keys() & b.keys():
        x, y = a[column], b[column]
        if (
            isinstance(x, _Unknown)
            or isinstance(y, _Unknown)
            or x.months is not None
            or y.months is not None
        ):
            out[column] = UNKNOWN
        elif x.intervals is None or y.intervals is None:
            continue
        else:
            out[column] = Constraint(tuple(merge([*x.intervals, *y.intervals])))
    for column in a.keys() ^ b.keys():
        value = a.get(column, b.get(column))
        if isinstance(value, _Unknown):
            out[column] = UNKNOWN
    return out


def _final(value: Value) -> list[Interval] | _Unknown | None:
    """The ranges of one filter, with the month parts applied. None: no limit."""
    if isinstance(value, _Unknown):
        return UNKNOWN
    if value.months is None:
        return None if value.intervals is None else list(value.intervals)
    if value.intervals is None or not all(i.closed for i in value.intervals):
        return UNKNOWN  # `EXTRACT(MONTH …) = 10` without a year
    out: list[Interval] = []
    for interval in value.intervals:
        start = floor(interval.start, "month")  # type: ignore[arg-type]
        for _ in range(_MAX_MONTHS):
            if start >= interval.end:  # type: ignore[operator]
                break
            nxt = step(start, "month")
            if start.month in value.months:
                out.append(
                    Interval(max(start, interval.start), min(nxt, interval.end))  # type: ignore[type-var]
                )
            start = nxt
        else:
            return UNKNOWN
    return merge(out)


def read_date_filters(qualified_sql: str, schema: Schema) -> DateFilters | None:
    """The date filters of the qualified SQL of the guard. None when the code cannot read it."""
    try:
        tree = sqlglot.parse_one(qualified_sql, read="bigquery")
        return _Reader(Lineage(tree), schema).read(tree)
    except Exception:  # noqa: BLE001 - "cannot verify" is a pass, never a block
        return None
