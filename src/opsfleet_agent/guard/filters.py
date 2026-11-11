"""Filters with literal values in a qualified query: input for the brand-literal rule (spec 30, B2.2
step 15) and for the empty-result checks (B2.7)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from opsfleet_agent.guard.lineage import Lineage
from opsfleet_agent.schema import Schema

_COMPARE: dict[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "!=": "!="}
# Conversions that keep the value of a column or a literal (no truncation).
_SAME_VALUE = (exp.Cast, exp.Date, exp.Timestamp, exp.TsOrDsToDate, exp.Datetime)


@dataclass(frozen=True)
class FilterLiteral:
    table: str  # base table, for example "orders"
    column: str  # for example "status"
    op: str  # "=", "!=", "<", "<=", ">", ">=", "in", "between", "like", "year"
    values: tuple[str, ...]
    column_type: str  # BigQuery type from the schema, for example "STRING"
    exact: bool  # False when the condition is under OR or NOT: it does not limit the result alone
    sql: str  # the condition text, for the notes


def literal_text(node: exp.Expression | None) -> str | None:
    """The text of a literal, also inside `TIMESTAMP('…')`, `DATE '…'` or `CAST('…' AS …)`."""
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return str(node.this)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return f"-{node.this.this}"
    if isinstance(node, exp.Boolean):
        return "TRUE" if node.this else "FALSE"
    if isinstance(node, _SAME_VALUE) and isinstance(node.this, exp.Literal):
        return str(node.this.this)
    return None


def string_literals(node: exp.Expression | None) -> list[str]:
    """String literals of `IN UNNEST([...])`. The guard has already refused a subquery in UNNEST."""
    if node is None:
        return []
    return [str(lit.this) for lit in node.find_all(exp.Literal) if lit.is_string]


def _column_side(node: exp.Expression) -> tuple[exp.Column, bool] | None:
    """The column of a condition side, and True for `EXTRACT(YEAR FROM column)`."""
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
        return node, False
    if (
        isinstance(node, exp.Extract)
        and isinstance(node.expression, exp.Column)
        and node.name.upper() == "YEAR"
    ):
        return node.expression, True
    if isinstance(node, _SAME_VALUE) and isinstance(node.this, exp.Column):
        return node.this, False
    return None


def _exact(condition: exp.Expression) -> bool:
    parent = condition.parent
    while parent is not None and not isinstance(
        parent, (exp.Where, exp.Having, exp.Join, exp.Select)
    ):
        if isinstance(parent, (exp.Or, exp.Not)):
            return False
        parent = parent.parent
    return True


def extract_filters(
    tree: exp.Expression, lineage: Lineage, schema: Schema
) -> list[FilterLiteral]:
    out: list[FilterLiteral] = []
    for cond in tree.find_all(*_COMPARE, exp.In, exp.Between, exp.Like):
        found = _parse_condition(cond)
        if found is None:
            continue
        column, op, values = found
        base = lineage.base_of(column)
        if base is None:
            continue
        table, name = base
        out.append(
            FilterLiteral(
                table=table,
                column=name,
                op=op,
                values=tuple(values),
                column_type=schema.column_type(table, name) or "",
                exact=_exact(cond),
                sql=cond.sql(dialect="bigquery"),
            )
        )
    return out


def _parse_condition(
    cond: exp.Expression,
) -> tuple[exp.Column, str, list[str]] | None:
    if isinstance(cond, exp.In):
        side = _column_side(cond.this)
        if side is None or side[1] or cond.args.get("query"):
            return None
        values = string_literals(cond.args.get("unnest")) + [
            v for v in (literal_text(e) for e in cond.expressions) if v is not None
        ]
        return (side[0], "in", values) if values else None
    if isinstance(cond, exp.Between):
        side = _column_side(cond.this)
        low, high = (
            literal_text(cond.args.get("low")),
            literal_text(cond.args.get("high")),
        )
        if side is None or side[1] or low is None or high is None:
            return None
        return side[0], "between", [low, high]
    if isinstance(cond, exp.Like):
        side = _column_side(cond.this)
        value = literal_text(cond.expression)
        if side is None or side[1] or value is None:
            return None
        return side[0], "like", [value]
    op = _COMPARE[type(cond)]
    for col_node, lit_node, flip in (
        (cond.this, cond.expression, False),
        (cond.expression, cond.this, True),
    ):
        side = _column_side(col_node)
        value = literal_text(lit_node)
        if side is None or value is None:
            continue
        column, year = side
        if year:
            return (column, "year", [value]) if op == "=" else None
        return column, _FLIP[op] if flip else op, [value]
    return None
