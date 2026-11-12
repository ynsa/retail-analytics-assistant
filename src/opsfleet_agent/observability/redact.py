"""Masking of logs and traces (spec 35, B2.3). No personal data goes into a log line or a span.

- `mask_sql(sql)`: string literals become `'<s:N>'` (N = the length), except the business values
  in `KEEP_LITERAL_COLUMNS`. Numbers compared to a PII column become `<n>`.
- `mask_text(text)`: the patterns of the output scanner (spec 30). Each hit becomes `[removed]`.
- `mask_prompt(prompt, version)`: data blocks become references, the system text becomes its
  version, and the rest goes through `mask_text`.
- `Redactor`: the attribute allowlist of B2.2 and the length limit. The span exporter and the log
  formatter call it before each write.

All functions are idempotent: a masked text stays the same when it is masked again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from opsfleet_agent.guard.rules import PII_COLUMNS
from opsfleet_agent.guard.scanner import scan_text

MAX_CHARS = 2000
CUT_MARK = "…"

# B2.3 rule 1: a string literal compared to one of these columns stays (business data, not PII).
KEEP_LITERAL_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("products", "brand"),
        ("products", "category"),
        ("products", "department"),
        ("products", "name"),
        ("orders", "status"),
        ("order_items", "status"),
        ("users", "state"),
        ("users", "city"),
        ("users", "country"),
        ("users", "gender"),
        ("orders", "gender"),
        ("users", "traffic_source"),
    }
)
_KEEP_NAMES = frozenset(column for _, column in KEEP_LITERAL_COLUMNS)
# A number compared to one of these columns becomes `<n>`. A user ID identifies a customer.
NUMBER_PII_COLUMNS = PII_COLUMNS | {"user_id"}

MASKED_NUMBER = "<n>"
_NUMBER_TOKEN = "masked_number_7f3a"  # `<n>` while the masked SQL is parsed again
_MASKED_STRING = re.compile(r"^<s:\d+>$")
# A date or a timestamp is not PII, and the deep dive needs the period of a query (B7 #2).
_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
# A closed string, or an open quote that runs to the end of the text.
_QUOTED = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|['\"].*", re.DOTALL)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?(?:\*/|$)", re.DOTALL)
_LONG_NUMBER = re.compile(r"(?<![\w.])\d{5,}(?![\w.])")
_DATA_BLOCK = re.compile(r"<data\b([^>]*)>.*?</data>", re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_COMPARISONS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.ILike,
    exp.In,
    exp.Between,
)


# --- SQL -------------------------------------------------------------------------------------------
def mask_sql(sql: str) -> str:
    """The SQL text for a trace. SQL that does not parse: each quoted string becomes `'<s:N>'`
    and each number of 5 or more digits becomes `<n>`."""
    if not sql:
        return sql
    tree = _parse(sql.replace(MASKED_NUMBER, _NUMBER_TOKEN))
    if tree is None:
        return _mask_sql_text(sql)
    aliases = _table_aliases(tree)
    for literal in list(tree.find_all(exp.Literal)):
        column = _compared_column(literal)
        if literal.is_string:
            if _keeps_string(literal.this, column, aliases):
                continue
            literal.replace(exp.Literal.string(f"<s:{len(literal.this)}>"))
        elif column is not None and column.name.lower() in NUMBER_PII_COLUMNS:
            literal.replace(exp.column(_NUMBER_TOKEN))
    try:
        text = tree.sql(dialect="bigquery", comments=False)
    except Exception:  # noqa: BLE001 - a masked text is always better than no text
        return _mask_sql_text(sql)
    return text.replace(_NUMBER_TOKEN, MASKED_NUMBER)


def _keeps_string(
    value: str, column: exp.Column | None, aliases: Mapping[str, str]
) -> bool:
    """An already masked value, a date, or a value of a business column stays."""
    return bool(
        _MASKED_STRING.match(value) or _DATE.match(value) or _keeps(column, aliases)
    )


def _parse(sql: str) -> exp.Expression | None:
    # BigQuery does not accept `''` in a string, but a model can write it. The MySQL dialect
    # reads it (and the back-quotes), so a brand such as 'Levi''s' stays readable in the trace.
    for dialect in ("bigquery", "mysql"):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001, S112 - the text masking handles every other case
            continue
        if tree is not None:
            return tree
    return None


def _mask_sql_text(sql: str) -> str:
    """Comments go away. Each quoted string becomes `'<s:N>'`, also a quote that never closes
    (it runs to the end of the text). Each number of 5 or more digits becomes `<n>`."""

    def quoted(match: re.Match[str]) -> str:
        token = match.group(0)
        closed = len(token) >= 2 and token[-1] == token[0]
        inner = token[1:-1] if closed else token[1:]
        if closed and (_MASKED_STRING.match(inner) or _DATE.match(inner)):
            return token
        return f"'<s:{len(inner)}>'"

    text = _QUOTED.sub(quoted, _COMMENT.sub(" ", sql))
    return _LONG_NUMBER.sub(MASKED_NUMBER, text)


def _table_aliases(tree: exp.Expression) -> dict[str, str]:
    """alias (or name) -> table name, for the tables of the dataset."""
    out: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        out[(table.alias_or_name or name).lower()] = name
    return out


def _compared_column(literal: exp.Literal) -> exp.Column | None:
    """The column on the other side of the comparison that contains the literal."""
    node: exp.Expression | None = literal.parent
    child: exp.Expression = literal
    while node is not None and isinstance(node, exp.Paren | exp.Tuple | exp.Neg):
        child, node = node, node.parent
    if not isinstance(node, _COMPARISONS):
        return None
    if isinstance(node, exp.In | exp.Between):
        side = node.this
    else:
        side = node.right if child is node.left else node.left
    if isinstance(side, exp.Column):
        return side
    return side.find(exp.Column) if side is not None else None


def _keeps(column: exp.Column | None, aliases: Mapping[str, str]) -> bool:
    if column is None:
        return False
    name = column.name.lower()
    qualifier = column.table.lower()
    table = aliases.get(qualifier) if qualifier else None
    if table is not None and table.split(".")[-1] in {
        t for t, _ in KEEP_LITERAL_COLUMNS
    }:
        return (table.split(".")[-1], name) in KEEP_LITERAL_COLUMNS
    return name in _KEEP_NAMES  # an unqualified column or a column of a CTE


# --- free text and prompts -------------------------------------------------------------------------
def mask_text(text: str) -> str:
    """B2.3 rule 2: email addresses, phone numbers, street addresses and coordinates."""
    if not text:
        return text
    return scan_text(text).text


def mask_prompt(prompt: str, version: str | None = None) -> str:
    """B2.3 rule 3: `<system version="…"/>`, then the prompt with each data block as a
    reference (`<data ref="res_…" rows="N"/>`), then the scanner on the rest."""

    def block(match: re.Match[str]) -> str:
        attrs = dict(_ATTR.findall(match.group(1)))
        if "ref" in attrs:
            return f'<data ref="{attrs["ref"]}" rows="{attrs.get("rows", "?")}"/>'
        source = attrs.get("source", "data")
        return f'<data source="{source}" chars="{len(match.group(0))}"/>'

    head = f'<system version="{version or "unknown"}"/>\n'
    return head + mask_text(_DATA_BLOCK.sub(block, prompt))


def cut(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + CUT_MARK


# --- attributes ------------------------------------------------------------------------------------
# B2.2: the attributes of each span kind. The exporter drops every other attribute. The extra
# names after the spec list come from specs 30-36 (B7 of spec 35 lists them). None of them holds
# a row value or a free text that is not masked.
_COMMON = frozenset(
    {"error_type", "error_detail", "error_temporary", "interrupted", "purpose"}
)
ATTRIBUTE_ALLOWLIST: dict[str, frozenset[str]] = {
    "turn": frozenset(
        {
            "user_id",
            "conversation_id",
            "turn_no",
            "intent",
            "status",
            "error_type",
            "model",
            "prompt_version",
            # spec 36 and the session: the totals of the turn
            "resume",
            "checkpoint_failed",
            "llm_calls",
            "llm_attempts",
            "fallback_calls",
            "bq_queries",
            "tokens_in",
            "tokens_out",
            "bytes_billed",
            "seconds",
        }
    ),
    "node": frozenset(
        {
            "node",
            "attempt",
            "step_index",
            "error_type",
            "intent",
            # specs 31-34: counts and codes only
            "tokens_estimated",
            "persona_chars",
            "message_too_long",
            "prompt_leak",
            "calculated_figures",
            "calc_errors",
            "checked_figures",
            "unverified_figures",
            "report_action",
            "planned_queries",
            "delete_scope",
            "confirm_outcome",
            "summary_source",
            "folded_turns",
            "summary_words",
            "scanner_hits",
        }
    ),
    "llm": frozenset(
        {
            "model",
            "fallback_used",
            "tokens_in",
            "tokens_out",
            "tokens_estimated",
            "retries",
            "finish_reason",
            "prompt_masked",
            "output_masked",
            "error_type",
            "prompt_version",
        }
    ),
    "tool": frozenset(
        {"tool", "side_effect", "report_ids", "error_type", "ok", "report_id", "bytes"}
    ),
    "sql": frozenset(
        {
            "sql_masked",
            "job_id",
            "bytes_processed",
            "bytes_billed",
            "bytes_estimated",
            "row_count",
            "rows",
            "truncated",
            "retries",
            "error_type",
            "circuit_open",
        }
    ),
    "guard": frozenset(
        {
            "outcome",
            "reason_code",
            "tables",
            "rewritten",
            "masker_hit",
            "scan_hits",
            "scan_patterns",
            # spec 30: the role, the target and the counts of the masker, scanner and checks
            "user_id",
            "role",
            "target",
            "hits_total",
            "masked_cells",
            "dropped_columns",
            "rows",
            "diagnosis",
            "finding",
            "queries_run",
            "sql_masked",
        }
    ),
    "limit": frozenset({"limit_name", "value", "max"}),
    "confirm": frozenset(
        {"outcome", "reason", "count", "ids_hash", "batch_id", "scope"}
    ),
}
_HITS_PREFIX = "hits_"  # guard.scan: hits_email, hits_phone, ...


class Redactor:
    """Applies the allowlist, the masking rules and the length limit to span attributes, span
    events and log fields."""

    def __init__(self, max_chars: int = MAX_CHARS) -> None:
        self.max_chars = max_chars

    def attributes(self, kind: str, attrs: Mapping[str, Any]) -> dict[str, Any]:
        allowed = ATTRIBUTE_ALLOWLIST.get(kind, frozenset()) | _COMMON
        out: dict[str, Any] = {}
        for key, value in attrs.items():
            if key in allowed or (kind == "guard" and key.startswith(_HITS_PREFIX)):
                out[key] = self.value(key, value)
        return out

    def value(self, key: str, value: Any) -> Any:
        if isinstance(value, str):
            masked = mask_text(mask_sql(value) if key == "sql_masked" else value)
            return cut(masked, self.max_chars)
        if isinstance(value, Sequence) and not isinstance(value, bytes):
            return [self.value(key, v) for v in value]
        return value

    def event(self, name: str, attrs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "attributes": {k: self.value(k, v) for k, v in attrs.items()},
        }

    def fields(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Log fields: every string is masked. SQL fields use `mask_sql`."""
        out: dict[str, Any] = {}
        for key, value in values.items():
            name = "sql_masked" if key in ("sql", "sql_masked") else key
            out[key] = self.value(name, value)
        return out
