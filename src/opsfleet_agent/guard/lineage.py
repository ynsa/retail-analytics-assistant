"""Column lineage on a qualified query (spec 30, B2.4): which base columns an output shows, and if
the output has one row for each customer.

Spec 30 names `sqlglot.lineage`. This module uses the sqlglot scope tree instead (deviation, see the
spec notes): the rule needs to know which values stay visible, and a value inside `COUNT(...)` is not
visible. The scope tree gives this directly, for CTEs, subqueries and each branch of a set operation.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from opsfleet_agent.guard.rules import CUSTOMER_ID_COLUMNS, DEMOGRAPHIC_COLUMNS

BaseColumn = tuple[str, str]  # (table, column), lower case

# A value inside these functions is not visible in the output: only a count is.
_HIDING = (exp.Count, exp.ApproxDistinct, exp.CountIf)
_MAX_DEPTH = 40


class LineageError(Exception):
    """The query is too deep or has a shape that the lineage cannot follow."""


class Lineage:
    def __init__(self, tree: exp.Expression) -> None:
        root = build_scope(tree)
        if root is None:
            raise LineageError("no scope")
        self.root = root
        self._scopes = {id(s.expression): s for s in root.traverse()}

    # --- scope helpers ----------------------------------------------------------------------------
    def scope_of(self, node: exp.Expression) -> Scope | None:
        current: exp.Expression | None = node
        while current is not None:
            scope = self._scopes.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return None

    @staticmethod
    def source_of(column: exp.Column, scope: Scope | None) -> object | None:
        """The Table or the Scope that the column's qualifier names (outer scopes too)."""
        while scope is not None:
            source = scope.sources.get(column.table)
            if source is not None:
                return source
            scope = scope.parent
        return None

    def leaf_selects(self, scope: Scope) -> list[Scope]:
        """The SELECT scopes of a scope: itself, or each branch of a set operation."""
        if isinstance(scope.expression, exp.SetOperation):
            out: list[Scope] = []
            for branch in scope.set_operation_scopes:
                out += self.leaf_selects(branch)
            return out
        return [scope]

    def _projections(
        self, scope: Scope, name: str
    ) -> Iterator[tuple[exp.Expression, Scope]]:
        """The projection named `name` in each branch of `scope` (by position after the first)."""
        leaves = self.leaf_selects(scope)
        if not leaves:
            return
        first = leaves[0].expression
        names = [p.alias_or_name.lower() for p in first.expressions]
        if name.lower() not in names:
            return
        index = names.index(name.lower())
        for leaf in leaves:
            projections = leaf.expression.expressions
            if index < len(projections):
                yield projections[index], leaf

    # --- lineage ------------------------------------------------------------------------------
    def base_of(self, column: exp.Column, depth: int = 0) -> BaseColumn | None:
        """Follow a plain column through CTEs and subqueries to its base table column."""
        if depth > _MAX_DEPTH:
            raise LineageError("too deep")
        source = self.source_of(column, self.scope_of(column))
        if isinstance(source, exp.Table):
            return source.name.lower(), column.name.lower()
        if isinstance(source, Scope):
            for projection, _ in self._projections(source, column.name):
                inner = projection.unalias()
                if isinstance(inner, exp.Column):
                    return self.base_of(inner, depth + 1)
        return None

    def visible(
        self, node: exp.Expression, scope: Scope, depth: int = 0
    ) -> set[BaseColumn]:
        """The base columns whose values stay visible in the value of `node`."""
        if depth > _MAX_DEPTH:
            raise LineageError("too deep")
        if isinstance(node, _HIDING):
            return set()
        if isinstance(node, exp.Window):  # PARTITION BY and ORDER BY do not show values
            return self.visible(node.this, scope, depth + 1)
        if isinstance(node, exp.Column):
            if isinstance(node.this, exp.Star):
                return set()
            source = self.source_of(node, scope)
            if isinstance(source, exp.Table):
                return {(source.name.lower(), node.name.lower())}
            out: set[BaseColumn] = set()
            if isinstance(source, Scope):
                for projection, leaf in self._projections(source, node.name):
                    out |= self.visible(projection, leaf, depth + 1)
            return out
        if isinstance(node, (exp.Select, exp.SetOperation)):
            sub = self._scopes.get(id(node))
            out = set()
            if sub is not None:
                for leaf in self.leaf_selects(sub):
                    for projection in leaf.expression.expressions:
                        out |= self.visible(projection, leaf, depth + 1)
            return out
        out = set()
        for child in node.iter_expressions():
            out |= self.visible(child, scope, depth + 1)
        return out

    # --- the rules of B2.4 --------------------------------------------------------------------
    def output_columns(self) -> list[str]:
        first = self.leaf_selects(self.root)[0].expression
        return [p.alias_or_name for p in first.expressions]

    def customer_level(self) -> bool:
        """True when the final output shows a customer ID, or groups by one (one row per customer)."""
        for leaf in self.leaf_selects(self.root):
            select = leaf.expression
            group = select.args.get("group")
            shown = list(select.expressions) + (
                list(group.expressions) if group else []
            )
            for node in shown:
                if self.visible(node, leaf) & CUSTOMER_ID_COLUMNS:
                    return True
        return False

    def referenced_columns(self, tree: exp.Expression) -> set[BaseColumn]:
        """Each base column that the query reads, in any position."""
        out: set[BaseColumn] = set()
        for column in tree.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            source = self.source_of(column, self.scope_of(column))
            if isinstance(source, exp.Table):
                out.add((source.name.lower(), column.name.lower()))
        return out

    def demographic_pairing(self, tree: exp.Expression) -> bool:
        return self.customer_level() and bool(
            self.referenced_columns(tree) & DEMOGRAPHIC_COLUMNS
        )
