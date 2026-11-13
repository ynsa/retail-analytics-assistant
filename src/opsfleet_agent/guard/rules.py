"""Fixed rules of the query safety layer (spec 30): tables, PII and demographic columns, reason codes
and the messages that the user sees (spec 30, A4).

The guard is an allowlist. A shape that it does not know is blocked or goes back to the model.
"""

from __future__ import annotations

from enum import StrEnum

ALLOWED_TABLES: tuple[str, ...] = ("orders", "order_items", "products", "users")

# Direct identifiers (gap analysis B3). They never reach the model or the user.
PII_COLUMNS: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "user_geom",
    }
)

# Fields that can appear only in aggregates (gap analysis B3), as (table, column).
DEMOGRAPHIC_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "city"),
        ("users", "state"),
        ("users", "country"),
        ("users", "age"),
        ("users", "gender"),
        ("users", "traffic_source"),
        ("orders", "gender"),
    }
)
DEMOGRAPHIC_NAMES: frozenset[str] = frozenset(c for _, c in DEMOGRAPHIC_COLUMNS)

# Columns that identify one customer.
CUSTOMER_ID_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {("users", "id"), ("orders", "user_id"), ("order_items", "user_id")}
)
# Result column names that show one customer for each row (the masker works on names only).
CUSTOMER_ID_NAMES: frozenset[str] = frozenset({"user_id", "customer_id"})

# Order-level fields that mix brands (gap analysis B2). A manager cannot use them.
MANAGER_HIDDEN_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {("orders", "num_of_item")}
)

# Function names and name prefixes that can read outside data or secrets.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {"external_query", "session_user", "appends", "changes", "vector_search"}
)
FORBIDDEN_PREFIXES: frozenset[str] = frozenset({"ml", "ai", "keys", "aead"})

MAX_SQL_CHARS = 20_000


class Reason(StrEnum):
    # policy_block: the request itself is not allowed. No retry.
    NOT_SELECT = "not_select"
    FORBIDDEN_CONSTRUCT = "forbidden_construct"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    PII_COLUMN = "pii_column"
    ROW_VALUE = "row_value"
    BRAND_OUT_OF_SCOPE = "brand_out_of_scope"
    DEMOGRAPHIC_PAIRING = "demographic_pairing"
    # parse_error: the model can correct the query (spec 31 loop).
    TOO_LONG = "too_long"
    SYNTAX = "syntax"
    MULTI_STATEMENT = "multi_statement"
    CTE_SHADOWS_TABLE = "cte_shadows_table"
    UNNEST_SUBQUERY = "unnest_subquery"
    STAR_SELECT = "star_select"
    UNKNOWN_COLUMN = "unknown_column"
    UNVERIFIABLE = "unverifiable"
    COLUMN_NOT_IN_SCOPE = "column_not_in_scope"
    BRAND_NAME_CASE = "brand_name_case"


POLICY_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.NOT_SELECT,
        Reason.FORBIDDEN_CONSTRUCT,
        Reason.TABLE_NOT_ALLOWED,
        Reason.PII_COLUMN,
        Reason.ROW_VALUE,
        Reason.BRAND_OUT_OF_SCOPE,
        Reason.DEMOGRAPHIC_PAIRING,
    }
)

# --- Messages for the user (spec 30, A4) ----------------------------------------------------------
READ_ONLY_MESSAGE = "I can only read the sales data. I cannot change it."
PII_MESSAGE = (
    "I cannot show or use personal data such as names, email addresses, street addresses, "
    "postal codes or exact locations. I can show customer IDs and totals."
)
PAIRING_MESSAGE = (
    "I cannot show a single customer together with the city, state, country, age, gender or "
    "traffic source. I can show totals by these fields, or customers without them."
)
SCANNER_NOTE = "Some text was removed because it looked like personal data."


def brand_out_of_scope_message(brand: str, user_brands: tuple[str, ...]) -> str:
    return f"{brand} is outside your brands. Your brands are {_join(user_brands)}."


def user_message_for(reason: Reason) -> str:
    if reason in (Reason.PII_COLUMN, Reason.ROW_VALUE):
        return PII_MESSAGE
    if reason is Reason.DEMOGRAPHIC_PAIRING:
        return PAIRING_MESSAGE
    return READ_ONLY_MESSAGE


def _join(names: tuple[str, ...]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
