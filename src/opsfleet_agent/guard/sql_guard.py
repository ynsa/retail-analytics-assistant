"""The SQL guard (spec 30, B2.2). It replaces the minimal guard of spec 01.

`SqlGuard.check(sql)` runs 18 steps in a fixed order. The first failed step gives the result:

- `policy_block`: the request itself is not allowed. The user gets a fixed message. No retry.
- `parse_error`: the model can write a better query. `model_feedback` says what to change.
- `pass`: `sql` is the scoped SQL, and `params` holds `@scope_brands` for a manager.

The guard never runs a query. Each decision writes one `guard` span (no rows, no SQL values).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot
from google.cloud import bigquery
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify

from opsfleet_agent import capabilities
from opsfleet_agent.config import Role
from opsfleet_agent.errors import ErrorType
from opsfleet_agent.guard.filters import FilterLiteral, extract_filters, string_literals
from opsfleet_agent.guard.lineage import Lineage, LineageError
from opsfleet_agent.guard.rules import (
    ALLOWED_TABLES,
    FORBIDDEN_FUNCTIONS,
    FORBIDDEN_PREFIXES,
    MANAGER_HIDDEN_COLUMNS,
    MAX_SQL_CHARS,
    PII_COLUMNS,
    POLICY_REASONS,
    Reason,
    brand_out_of_scope_message,
    user_message_for,
)
from opsfleet_agent.guard.scope import (
    ScopeError,
    ScopeTemplates,
    load_templates,
    rewrite_scope,
    verify_scope,
)
from opsfleet_agent.guard.value_cache import ValueCache
from opsfleet_agent.identity import UserContext
from opsfleet_agent.observability.redact import mask_sql
from opsfleet_agent.schema import Schema
from opsfleet_agent.tracing import span

# sqlglot logs a warning for each statement that it parses as a generic Command. The guard blocks
# these statements anyway, so the warnings are noise in the chat.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_WRITE_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "TruncateTable",
        "Grant",
        "Revoke",
        "Command",
        "Copy",
        "Export",
        "LoadData",
        "Declare",
        "Set",
        "Use",
        "Transaction",
        "Commit",
        "Rollback",
        "Describe",
        "Pragma",
    )
    if hasattr(exp, name)
)
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Parameter",
        "Placeholder",
        "SessionParameter",
        "SessionUser",
        "CurrentUser",
    )
    if hasattr(exp, name)
)
_PII_IN_PATH = re.compile(
    r"(?<![a-z0-9_])(" + "|".join(sorted(PII_COLUMNS)) + r")(?![a-z0-9_])"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_UNKNOWN_COLUMN = re.compile(r"Column '([^']+)' could not be resolved")


class GuardOutcome(StrEnum):
    PASS = "pass"
    POLICY_BLOCK = "policy_block"  # ErrorType.guard_policy_block
    PARSE_ERROR = "parse_error"  # ErrorType.guard_parse_error


@dataclass(frozen=True)
class GuardResult:
    outcome: GuardOutcome
    reason_code: str | None = None  # for example "pii_column"
    sql: str | None = None  # rewritten SQL, only for PASS
    params: list[bigquery.ArrayQueryParameter] = field(default_factory=list)
    user_message: str | None = None  # only for POLICY_BLOCK (text from spec 30, A4)
    model_feedback: str | None = None  # only for PARSE_ERROR
    tables: frozenset[str] = frozenset()
    filters: list[FilterLiteral] = field(default_factory=list)
    output_columns: list[str] = field(default_factory=list)
    # Additions for the masker and the empty-result checks (spec 30 notes, 2026-11-11):
    customer_level: bool = False  # the output has one row for each customer
    source_sql: str = ""  # the SQL of the model, before the rewrite
    qualified_sql: str | None = (
        None  # the model's SQL with each column bound to its table
    )
    rewritten: bool = False  # at least one scope subquery was placed
    internal_error: bool = (
        False  # the check after the rewrite failed (a bug in the guard)
    )

    @property
    def passed(self) -> bool:
        return self.outcome is GuardOutcome.PASS

    @property
    def error_type(self) -> ErrorType | None:
        if self.outcome is GuardOutcome.POLICY_BLOCK:
            return ErrorType.GUARD_POLICY_BLOCK
        if self.outcome is GuardOutcome.PARSE_ERROR:
            return ErrorType.GUARD_PARSE_ERROR
        return None


class _Stop(Exception):
    def __init__(self, result: GuardResult) -> None:
        super().__init__(result.reason_code)
        self.result = result


def _policy(reason: Reason, message: str | None = None) -> _Stop:
    return _Stop(
        GuardResult(
            GuardOutcome.POLICY_BLOCK,
            reason_code=reason.value,
            user_message=message or user_message_for(reason),
        )
    )


def _parse_error(reason: Reason, feedback: str, internal: bool = False) -> _Stop:
    return _Stop(
        GuardResult(
            GuardOutcome.PARSE_ERROR,
            reason_code=reason.value,
            model_feedback=feedback,
            internal_error=internal,
        )
    )


def _loose(name: str) -> str:
    """Brand comparison for the hint only: no letter case, one kind of apostrophe, single spaces."""
    return " ".join(name.replace("’", "'").casefold().split())


class SqlGuard:
    def __init__(
        self,
        user: UserContext,
        schema: Schema,
        values: ValueCache,
        templates: ScopeTemplates | None = None,
    ) -> None:
        self.user = user
        self.schema = schema
        self.values = values
        self.templates = templates or load_templates(schema.dataset)
        self._mapping = schema.sqlglot_mapping()
        self._manager = user.role is Role.MANAGER

    # --- public ---------------------------------------------------------------------------------
    def check(self, sql: str, purpose: str = "answer") -> GuardResult:
        with span(
            "guard.check",
            "guard",
            user_id=self.user.id,
            role=self.user.role.value,
            purpose=purpose,
            sql_masked=mask_sql(sql),
        ) as sp:
            try:
                result = self._check(sql)
            except _Stop as stop:
                result = stop.result
            except Exception:  # noqa: BLE001 - a bug in the guard never lets a query pass
                result = GuardResult(
                    GuardOutcome.PARSE_ERROR,
                    reason_code=Reason.UNVERIFIABLE.value,
                    model_feedback="Write a simpler query.",
                    internal_error=True,
                )
            sp.set_attribute("outcome", result.outcome.value)
            if result.reason_code:
                sp.set_attribute("reason_code", result.reason_code)
            sp.set_attribute("tables", sorted(result.tables))
            sp.set_attribute("rewritten", result.rewritten)
            sp.set_attribute("masker_hit", False)
            if result.error_type is not None:
                sp.set_attribute("error_type", result.error_type.value)
            if result.internal_error:
                sp.add_event(
                    "internal_error", {"error_type": ErrorType.INTERNAL_ERROR.value}
                )
        return result

    # --- the steps of B2.2 ------------------------------------------------------------------------
    def _check(self, sql: str) -> GuardResult:
        if len(sql) > MAX_SQL_CHARS:  # step 1
            raise _parse_error(
                Reason.TOO_LONG,
                f"The SQL has more than {MAX_SQL_CHARS:,} characters. Write a shorter query.",
            )
        tree = self._parse(sql)  # steps 2 to 5
        self._forbidden_constructs(tree)  # step 6
        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        tables = self._normalize_tables(tree, cte_names)  # step 7
        shadow = sorted(cte_names & set(ALLOWED_TABLES))  # step 8
        if shadow:
            raise _parse_error(
                Reason.CTE_SHADOWS_TABLE,
                f"Rename the CTE '{shadow[0]}': a CTE cannot have the name of a table.",
            )
        self._unnest(tree)  # step 9
        self._star(tree)  # step 10
        self._pii(tree)  # step 11
        qualified, lineage = self._qualify(tree, tables)  # step 13
        self._row_values(
            qualified
        )  # step 12, on the qualified tree (see the spec notes)
        referenced = self._safe(lambda: lineage.referenced_columns(qualified))
        if self._manager and referenced & MANAGER_HIDDEN_COLUMNS:  # step 14
            raise _parse_error(
                Reason.COLUMN_NOT_IN_SCOPE,
                "orders.num_of_item is not available, because an order can contain items of "
                "other brands. Count the items from order_items.",
            )
        if self._manager:  # step 15
            self._brand_literals(qualified, lineage)
        if self._safe(lambda: lineage.demographic_pairing(qualified)):  # step 16
            raise _policy(Reason.DEMOGRAPHIC_PAIRING)
        filters = self._safe(lambda: extract_filters(qualified, lineage, self.schema))
        customer_level = self._safe(lineage.customer_level)
        output_columns = self._safe(lineage.output_columns)
        try:  # step 17
            rewritten, params = rewrite_scope(tree, self.user, self.templates)
        except ScopeError as exc:
            raise _parse_error(
                Reason.UNVERIFIABLE, f"Write a simpler query ({exc})."
            ) from exc
        scoped_sql = rewritten.sql(dialect="bigquery")
        if not verify_scope(scoped_sql, self.user, self.templates):  # step 18
            raise _parse_error(
                Reason.UNVERIFIABLE, "Write a simpler query.", internal=True
            )
        return GuardResult(
            GuardOutcome.PASS,
            sql=scoped_sql,
            params=params,
            tables=frozenset(tables),
            filters=filters,
            output_columns=output_columns,
            customer_level=customer_level,
            source_sql=sql,
            qualified_sql=qualified.sql(dialect="bigquery"),
            rewritten=scoped_sql != tree.sql(dialect="bigquery"),
        )

    def _parse(self, sql: str) -> exp.Expression:
        try:
            statements = [
                s for s in sqlglot.parse(sql, read="bigquery") if s is not None
            ]
        except (ParseError, TokenError) as exc:
            raise _parse_error(Reason.SYNTAX, _syntax_feedback(exc)) from exc
        if not statements:
            raise _parse_error(Reason.SYNTAX, "The SQL text is empty.")
        for statement in statements:  # step 3 (and step 5: the root of each statement)
            root = _unwrap(statement)
            if not isinstance(root, (exp.Select, exp.SetOperation)) or any(
                isinstance(node, _WRITE_NODES) for node in statement.walk()
            ):
                raise _policy(Reason.NOT_SELECT)
        if len(statements) > 1:  # step 4
            raise _parse_error(
                Reason.MULTI_STATEMENT, "Write exactly one SELECT statement."
            )
        return _unwrap(statements[0])

    def _forbidden_constructs(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                raise _policy(Reason.FORBIDDEN_CONSTRUCT)
            if isinstance(node, exp.Table) and (
                node.args.get("version") is not None
                or not isinstance(node.this, exp.Identifier)
            ):
                raise _policy(
                    Reason.FORBIDDEN_CONSTRUCT
                )  # FOR SYSTEM_TIME, table functions
            if isinstance(node, exp.Func) and _forbidden_function(node):
                raise _policy(Reason.FORBIDDEN_CONSTRUCT)
            if (
                isinstance(node, exp.Dot)
                and _prefix_name(node.this) in FORBIDDEN_PREFIXES
            ):
                raise _policy(Reason.FORBIDDEN_CONSTRUCT)  # ML.*, AI.*, KEYS.*, AEAD.*

    def _normalize_tables(self, tree: exp.Expression, cte_names: set[str]) -> set[str]:
        """Check each table and write its full name, so qualify and the rewrite can find it."""
        used: set[str] = set()
        project, dataset = self.schema.project, self.schema.dataset_name
        for table in list(tree.find_all(exp.Table)):
            name, db, catalog = table.name, table.db, table.catalog
            if not db and not catalog and name.lower() in cte_names:
                continue
            if (
                name.lower() not in ALLOWED_TABLES
                or (db and db.lower() != dataset.lower())
                or (catalog and catalog.lower() != project.lower())
                or len(table.parts) > 3
            ):
                raise _policy(Reason.TABLE_NOT_ALLOWED)
            table.set("this", exp.to_identifier(name.lower()))
            table.set("db", exp.to_identifier(dataset))
            table.set("catalog", exp.to_identifier(project))
            used.add(name.lower())
        return used

    @staticmethod
    def _unnest(tree: exp.Expression) -> None:
        for unnest in tree.find_all(exp.Unnest):
            if unnest.find(exp.Select, exp.SetOperation, exp.Subquery) is not None:
                raise _parse_error(
                    Reason.UNNEST_SUBQUERY,
                    "UNNEST cannot contain a subquery. Use a JOIN or a CTE instead.",
                )

    @staticmethod
    def _star(tree: exp.Expression) -> None:
        for star in tree.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise _parse_error(
                    Reason.STAR_SELECT,
                    "List the columns by name. SELECT * and t.* are not allowed "
                    "(COUNT(*) is allowed).",
                )

    @staticmethod
    def _pii(tree: exp.Expression) -> None:
        for node in tree.walk():
            if (
                isinstance(node, (exp.Identifier, exp.Var))
                and node.name.lower() in PII_COLUMNS
            ):
                raise _policy(Reason.PII_COLUMN)
            if (
                isinstance(node, exp.JSONPathKey)
                and str(node.this).lower() in PII_COLUMNS
            ):
                raise _policy(
                    Reason.PII_COLUMN
                )  # sqlglot parses '$.email' as a JSON path
            if (
                isinstance(node, exp.Literal)
                and node.is_string
                and str(node.this).lstrip().startswith("$")
                and _PII_IN_PATH.search(str(node.this).lower())
            ):
                raise _policy(Reason.PII_COLUMN)  # a JSON path such as '$.email'

    @staticmethod
    def _row_values(tree: exp.Expression) -> None:
        """After qualify, each real column has a table qualifier, and a reference to a whole row is a
        `TableColumn`, for example `u` in `TO_JSON_STRING(u)`. A column without a qualifier that
        names a table, a CTE or a subquery is also a whole row."""
        if tree.find(exp.TableColumn) is not None:
            raise _policy(Reason.ROW_VALUE)
        row_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            row_names.add((table.alias or table.name).lower())
        for sub in tree.find_all(exp.Subquery):
            if sub.alias:
                row_names.add(sub.alias.lower())
        for column in tree.find_all(exp.Column):
            if not column.table and column.name.lower() in row_names:
                raise _policy(Reason.ROW_VALUE)

    def _qualify(
        self, tree: exp.Expression, tables: set[str]
    ) -> tuple[exp.Expression, Lineage]:
        try:
            qualified = qualify(
                tree.copy(),
                schema=self._mapping,
                dialect="bigquery",
                validate_qualify_columns=True,
                quote_identifiers=False,
            )
            return qualified, Lineage(qualified)
        except OptimizeError as exc:
            found = _UNKNOWN_COLUMN.search(str(exc))
            if found:
                raise _parse_error(
                    Reason.UNKNOWN_COLUMN,
                    f"The column '{found.group(1)}' does not exist in the tables of this "
                    f"query. Columns: {self._column_lists(tables)}.",
                ) from exc
            raise _parse_error(
                Reason.UNVERIFIABLE, f"Write a simpler query ({_clean(exc)})."
            ) from exc
        except Exception as exc:
            raise _parse_error(Reason.UNVERIFIABLE, "Write a simpler query.") from exc

    def _column_lists(self, tables: set[str]) -> str:
        return "; ".join(
            f"{t}({', '.join(self.schema.visible_columns(t, self._manager))})"
            for t in sorted(tables)
        )

    def _brand_literals(self, tree: exp.Expression, lineage: Lineage) -> None:
        allowed = set(self.user.brands)
        loose = {_loose(b): b for b in self.user.brands}
        out_of_scope: list[str] = []
        wrong_case: list[tuple[str, str]] = []
        for literal in self._safe(lambda: _brand_values(tree, lineage)):
            if literal in allowed:
                continue
            if _loose(literal) in loose:
                wrong_case.append((literal, loose[_loose(literal)]))
            else:
                out_of_scope.append(literal)
        if out_of_scope:
            raise _policy(
                Reason.BRAND_OUT_OF_SCOPE,
                brand_out_of_scope_message(out_of_scope[0], self.user.brands),
            )
        if wrong_case:
            given, exact = wrong_case[0]
            raise _parse_error(
                Reason.BRAND_NAME_CASE,
                f"The brand '{given}' is written differently in the data. "
                f"Use the exact name '{exact}'.",
            )

    @staticmethod
    def _safe(fn):  # type: ignore[no-untyped-def]
        """Run a lineage step. A failure means the guard cannot verify the query."""
        try:
            return fn()
        except _Stop:
            raise
        except (LineageError, RecursionError, KeyError, AttributeError) as exc:
            raise _parse_error(Reason.UNVERIFIABLE, "Write a simpler query.") from exc


# --- helpers ----------------------------------------------------------------------------------


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Subquery, exp.Paren)) and not node.alias:
        node = node.this
    return node


def _prefix_name(node: exp.Expression | None) -> str:
    if isinstance(node, (exp.Identifier, exp.Var, exp.Column)):
        return node.name.lower()
    return ""


def _forbidden_function(node: exp.Func) -> bool:
    name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
    if name in FORBIDDEN_FUNCTIONS:
        return True
    prefix, dot, _ = name.partition(".")
    return bool(dot) and prefix in FORBIDDEN_PREFIXES


def _brand_values(tree: exp.Expression, lineage: Lineage) -> list[str]:
    """The string literals that a condition compares with `products.brand` (= and IN)."""
    out: list[str] = []
    brand = ("products", "brand")
    for node in tree.find_all(exp.EQ, exp.In):
        if isinstance(node, exp.In):
            if (
                isinstance(node.this, exp.Column)
                and lineage.base_of(node.this) == brand
            ):
                out += [
                    str(e.this)
                    for e in node.expressions
                    if isinstance(e, exp.Literal) and e.is_string
                ]
                out += string_literals(node.args.get("unnest"))
            continue
        for col, lit in ((node.this, node.expression), (node.expression, node.this)):
            if (
                isinstance(col, exp.Column)
                and isinstance(lit, exp.Literal)
                and lit.is_string
                and lineage.base_of(col) == brand
            ):
                out.append(str(lit.this))
    return out


def _clean(exc: Exception) -> str:
    return (
        _ANSI.sub("", str(exc)).splitlines()[0][:300]
        if str(exc)
        else type(exc).__name__
    )


def _syntax_feedback(exc: Exception) -> str:
    errors = getattr(exc, "errors", None) or []
    if errors:
        first = errors[0]
        text = f"{first.get('description', 'syntax error')} (line {first.get('line')}, column {first.get('col')})"
    else:
        text = _clean(exc)
    return f"The SQL has a syntax error: {_ANSI.sub('', text)[:300]}"


__all__ = ["POLICY_REASONS", "FilterLiteral", "GuardOutcome", "GuardResult", "SqlGuard"]

capabilities.register("guard_full")  # spec 37, B2.5
