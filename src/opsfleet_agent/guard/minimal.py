"""Minimal SQL guard of the walking skeleton (spec 01): read-only, one statement, four tables,
and a basic block of the PII columns (so no personal data reaches the model, even in development).

Spec 30 replaces it with the full guard (brand scope, PII, pairing rule, empty-result checks).
The guard is an allowlist: everything that is not a known read-only shape is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot
from sqlglot import exp

READ_ONLY_MESSAGE = (
    "I can only read the sales data. I cannot change, create or delete anything."
)
PII_MESSAGE = "I cannot use personal data of customers, for example names, email addresses, street addresses or locations. I can show totals, or customers by user ID."

# Direct identifiers (gap analysis B3). Spec 30 adds the aggregate-only rule for demographic fields.
PII_COLUMNS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "user_geom",
        "ip_address",
    }
)

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Create",
        "Drop",
        "Merge",
        "Alter",
        "Command",
        "Grant",
        "TruncateTable",
        "Copy",
        "Export",
        "LoadData",
    )
    if hasattr(exp, name)
)


class GuardOutcome(StrEnum):
    PASS = "pass"
    POLICY_BLOCK = "policy_block"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class GuardResult:
    outcome: GuardOutcome
    reason_code: str | None = None
    user_message: str | None = None
    tables: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.outcome is GuardOutcome.PASS


def _block(reason: str, message: str = READ_ONLY_MESSAGE) -> GuardResult:
    return GuardResult(
        GuardOutcome.POLICY_BLOCK, reason_code=reason, user_message=message
    )


def check_read_only(sql: str, dataset: str, tables: list[str]) -> GuardResult:
    """Check one SQL text against the read-only rules of spec 01."""
    project, _, dataset_name = dataset.rpartition(".")
    allowed = set(tables)
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.ParseError:
        return GuardResult(GuardOutcome.PARSE_ERROR, reason_code="parse_error")
    if len(statements) != 1:
        return _block("multiple_statements" if statements else "empty")
    root = statements[0]
    if any(isinstance(node, _FORBIDDEN_NODES) for node in root.walk()):
        return _block("write_statement")
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        return GuardResult(GuardOutcome.PARSE_ERROR, reason_code="not_a_query")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    used: list[str] = []
    for table in root.find_all(exp.Table):
        name, db, catalog = table.name, table.db, table.catalog
        if not db and not catalog and name.lower() in cte_names:
            continue
        if name not in allowed:
            return _block(
                "table_not_allowed",
                f"I can only read these tables: {', '.join(tables)}.",
            )
        if db and db != dataset_name:
            return _block(
                "dataset_not_allowed", f"I can only read the dataset {dataset}."
            )
        if catalog and catalog != project:
            return _block(
                "dataset_not_allowed", f"I can only read the dataset {dataset}."
            )
        used.append(name)
    if not used:
        return _block("no_table", "The query must read the sales data.")
    if any(col.name.lower() in PII_COLUMNS for col in root.find_all(exp.Column)):
        return _block("pii_column", PII_MESSAGE)
    if "users" in used and any(
        star.find_ancestor(exp.AggFunc) is None for star in root.find_all(exp.Star)
    ):
        return _block("pii_star", PII_MESSAGE)
    return GuardResult(GuardOutcome.PASS, tables=tuple(sorted(set(used))))
