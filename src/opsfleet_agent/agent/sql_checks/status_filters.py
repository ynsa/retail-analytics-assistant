"""The statuses that a query leaves out, read with sqlglot (spec 31, B7: the definition check).

The glossary filters use `order_items.status`: the revenue filter leaves out `Cancelled` and
`Returned`, the purchase filter leaves out `Cancelled`. For each filter of the query (a `WHERE`,
`HAVING` or `JOIN … ON` condition, and the condition of `CASE WHEN`, `IF` or `COUNTIF`), the code
computes the statuses of this pair that the filter always leaves out:

- `AND` joins the sets, `OR` keeps only the statuses that both branches leave out.
- `status NOT IN (…)`, `status != …`, `NOT status = …` leave out the listed statuses.
- `status IN (…)` and `status = …` leave out the statuses that are not in the list.
- A condition inside `CASE WHEN`, `IF` or `COUNTIF` also gets the `WHERE` of its `SELECT`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from opsfleet_agent.guard.filters import literal_text, string_literals
from opsfleet_agent.guard.lineage import Lineage

STATUS_COLUMN = ("order_items", "status")
TRACKED = frozenset({"Cancelled", "Returned"})


@dataclass
class StatusFilters:
    sets: list[frozenset[str]] = field(default_factory=list)  # one for each filter
    any_condition: bool = (
        False  # the query has a condition on a `status` column (any table)
    )
    # A condition on order_items.status that the code cannot read, for example
    # `LOWER(oi.status) NOT IN (…)` or `oi.status IN (SELECT …)`: the check cannot verify.
    unreadable: bool = False

    def applies(
        self,
        excluded: frozenset[str],
        explained: frozenset[frozenset[str]] = frozenset(),
    ) -> bool:
        """True when one filter leaves out exactly these statuses (of the tracked pair). A filter
        that leaves out more also counts when another cited definition explains it: in a query
        with revenue and purchase figures, code cannot tell which figure uses which filter."""
        return any(
            found == excluded or (found > excluded and found in explained)
            for found in self.sets
        )


class _Reader:
    def __init__(self, lineage: Lineage) -> None:
        self.lineage = lineage
        self.any_condition = False
        self.unreadable = False

    def _is_status(self, node: exp.Expression) -> bool:
        while isinstance(node, exp.Paren):
            node = node.this
        if not isinstance(node, exp.Column):
            return False
        try:
            return self.lineage.base_of(node) == STATUS_COLUMN
        except Exception:  # noqa: BLE001 - an unknown column is not the status column
            return False

    def _mentions_status(self, node: exp.Expression) -> bool:
        columns = [node] if isinstance(node, exp.Column) else node.find_all(exp.Column)
        for column in columns:
            if column.name.lower() == "status":
                self.any_condition = True
                if self._is_status(column):
                    return True
        return False

    def atom(self, node: exp.Expression) -> frozenset[str] | None:
        """The left-out statuses of one condition. None when it is not a status condition."""
        found = self._atom(node)
        if found is None and self._mentions_status(node):
            self.unreadable = True
        return found

    def _atom(self, node: exp.Expression) -> frozenset[str] | None:
        if isinstance(node, exp.In) and self._is_status(node.this):
            if node.args.get("query"):
                return None
            values = string_literals(node.args.get("unnest")) + [
                v for v in (literal_text(e) for e in node.expressions) if v is not None
            ]
            self.any_condition = True
            return TRACKED - set(values)
        if isinstance(node, (exp.EQ, exp.NEQ)):
            for column, other in (
                (node.this, node.expression),
                (node.expression, node.this),
            ):
                if (
                    self._is_status(column)
                    and (value := literal_text(other)) is not None
                ):
                    self.any_condition = True
                    if isinstance(node, exp.EQ):
                        return TRACKED - {value}
                    return TRACKED & {value}
        return None

    def evaluate(self, node: exp.Expression) -> frozenset[str] | None:
        if isinstance(node, exp.Paren):
            return self.evaluate(node.this)
        if isinstance(node, exp.And):
            a, b = self.evaluate(node.this), self.evaluate(node.expression)
            if a is None:
                return b
            return a if b is None else a | b
        if isinstance(node, exp.Or):
            a, b = self.evaluate(node.this), self.evaluate(node.expression)
            if a is None or b is None:
                return frozenset() if (a, b) != (None, None) else None
            return a & b
        if isinstance(node, exp.Not):
            inner = node.this
            while isinstance(inner, exp.Paren):
                inner = inner.this
            found = self.atom(inner) if not isinstance(inner, exp.Connector) else None
            if found is None and self._mentions_status(inner):
                self.unreadable = True
            return None if found is None else TRACKED - found
        return self.atom(node)

    def where_of(self, node: exp.Expression) -> frozenset[str]:
        select = node.find_ancestor(exp.Select)
        where = select.args.get("where") if select is not None else None
        found = self.evaluate(where.this) if where is not None else None
        return found or frozenset()

    def read(self, tree: exp.Expression) -> StatusFilters:
        out = StatusFilters()
        for node in tree.walk():
            if isinstance(node, (exp.Where, exp.Having)):
                found = self.evaluate(node.this)
            elif isinstance(node, exp.Join) and node.args.get("on") is not None:
                found = self.evaluate(node.args["on"])
            elif isinstance(node, (exp.If, exp.CountIf)) and node.this is not None:
                own = self.evaluate(node.this)
                found = None if own is None else own | self.where_of(node)
            else:
                continue
            if found is not None:
                out.sets.append(frozenset(found))
        out.any_condition = self.any_condition
        out.unreadable = self.unreadable
        return out


def read_status_filters(qualified_sql: str) -> StatusFilters | None:
    """The status filters of the qualified SQL of the guard. None when the code cannot read it."""
    try:
        tree = sqlglot.parse_one(qualified_sql, read="bigquery")
        return _Reader(Lineage(tree)).read(tree)
    except Exception:  # noqa: BLE001 - "cannot verify" is a pass, never a block
        return None
