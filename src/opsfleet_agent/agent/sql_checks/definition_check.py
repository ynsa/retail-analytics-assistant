"""The definition check: does the SQL apply the status filter of each cited definition? (spec 31, B7)

A glossary entry with an `Excludes` line (`revenue`, `orders`, `items_sold`, `purchase` and others)
needs a filter that leaves out exactly these statuses of `Cancelled` and `Returned`. The footer
cites the keys of `definitions_used`, so each such key needs its filter.

One more case: an output column named like a metric (`orders`, `total_revenue`, `items_sold_prev`)
in a query with no status condition at all. The Docker check of 2026-11-12 had this query: an
`orders` count without a status filter.

A count of customers (an output column with the word `customers`, for example
`active_customers`) when `customer_of_brand` is cited: the query needs the purchase filter, a
filter that leaves out only `Cancelled`. A stricter filter does not count here, also when another
cited key explains it. In round 2 of spec 50 (RP-05), a report cited "Customer of a brand = at least
one purchase" but counted the customers with the revenue filter only (697, not 783). With only a
revenue filter in the query, the count cannot use the purchase filter. A customer count without
this citation (for example the denominator of spend per customer) keeps the normal rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from opsfleet_agent.agent.context import Definition
from opsfleet_agent.agent.sql_checks.status_filters import StatusFilters

# Name parts that do not change the metric of a column: `total_revenue`, `orders_prev`.
_NEUTRAL_PARTS = frozenset(
    {"total", "all", "groups", "prev", "previous", "current", "cur", "change", "pct"}
)


CUSTOMER_COUNT_KEY = "customer_of_brand"


def is_customer_count(name: str) -> bool:
    """`customers`, `active_customers`, `customers_prev`: a count of customers. Not `spend_per_…`."""
    parts = name.lower().split("_")
    return "customers" in parts and "per" not in parts


@dataclass(frozen=True)
class DefinitionVerdict:
    status: str  # match, mismatch, unverified, none
    missing: tuple[str, ...] = ()  # keys whose filter the SQL does not apply
    applied: tuple[str, ...] = ()  # the cited keys that the footer may show
    checked: tuple[str, ...] = field(default=())  # the keys with a status rule


def metric_of_column(name: str, keys: set[str]) -> str | None:
    """`total_orders` -> `orders`, `revenue_2024` -> `revenue`. None for other names."""
    parts = [
        p
        for p in name.lower().split("_")
        if p and p not in _NEUTRAL_PARTS and not p.isdigit()
    ]
    joined = "_".join(parts)
    return joined if joined in keys else None


def check_definitions(
    cited: list[str],
    output_columns: list[str],
    definitions: dict[str, Definition],
    filters: StatusFilters | None,
    reads_order_items: bool = True,
    also_cited: Iterable[str] = (),
) -> DefinitionVerdict:
    """`also_cited`: the keys of the report (spec 32). They apply only to the customer count."""
    with_rule = {k for k, d in definitions.items() if d.excludes}
    keys = list(dict.fromkeys(k for k in cited if k in definitions))
    required = [k for k in keys if k in with_rule]
    if filters is not None and not filters.any_condition and reads_order_items:
        for column in output_columns:
            key = metric_of_column(column, with_rule)
            if key and key not in required:
                required.append(key)
    strict: set[str] = set()  # keys whose exact filter must be in the query
    if (
        reads_order_items
        and CUSTOMER_COUNT_KEY in with_rule
        and CUSTOMER_COUNT_KEY in {*cited, *also_cited}
        and any(is_customer_count(c) for c in output_columns)
    ):
        strict.add(CUSTOMER_COUNT_KEY)
        if CUSTOMER_COUNT_KEY not in required:
            required.append(CUSTOMER_COUNT_KEY)
    if not required:
        return DefinitionVerdict("none", applied=tuple(keys))
    if filters is None:
        return DefinitionVerdict(
            "unverified", applied=tuple(keys), checked=tuple(required)
        )
    explained = frozenset(definitions[k].excludes for k in required)
    missing = tuple(
        k
        for k in required
        if not filters.applies(
            definitions[k].excludes, frozenset() if k in strict else explained
        )
    )
    if missing and filters.unreadable:
        # A status condition that code cannot read may apply the definition: cannot verify.
        return DefinitionVerdict(
            "unverified", applied=tuple(keys), checked=tuple(required)
        )
    applied = tuple(k for k in keys if k not in missing)
    status = "mismatch" if missing else "match"
    return DefinitionVerdict(status, missing, applied, tuple(required))
