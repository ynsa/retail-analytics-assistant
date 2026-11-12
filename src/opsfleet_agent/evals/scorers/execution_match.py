"""Execution match: compare the reference result with each result of the agent (spec 21, B2.6).

Steps: normalize the values, map each reference column to the agent column whose values match best
(the names do not matter), compare the rows with the mode of the case, and apply the tolerance.
One matching agent result is enough.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from opsfleet_agent.evals.cases import Expected
from opsfleet_agent.evals.normalize import (
    NUMBER_KINDS,
    context_year,
    is_missing,
    norm_key,
    to_float,
)
from opsfleet_agent.evals.records import QueryRecord
from opsfleet_agent.evals.scorers import ScoreResult
from opsfleet_agent.evals.snapshot import top_k_rows

DEFAULT_REL = 0.005
DEFAULT_ABS = 0.01
RATIO_ABS = 0.0001  # 0.01 percentage points: 0.01 is too wide for a ratio
# The `value` mode matches a column by its meaning (spec 21 B7, review point of 2026-11-11). These
# words say nothing about the meaning of a column: "total_revenue" means "revenue".
GENERIC_WORDS = frozenset(
    {
        "all", "amount", "avg", "average", "count", "distinct", "num", "number", "of",
        "pct", "percent", "percentage", "rate", "ratio", "share", "sum", "the", "total",
        "unique", "usd", "value",
    }
)  # fmt: skip
# Words of a key or a period: such a column is not the answer number (for example `year`).
KEY_WORDS = frozenset({"id", "year", "quarter", "month", "week", "day", "date"})
SYNONYMS: dict[str, frozenset[str]] = {
    "customer": frozenset({"buyer", "user", "shopper", "client", "purchaser"}),
    "revenue": frozenset({"sale", "gmv", "income", "turnover"}),
    "item": frozenset({"unit", "quantity", "qty", "piece"}),
    "repeat": frozenset({"returning", "repurchase", "rebuy", "retention", "again"}),
    "return": frozenset({"returned"}),
    "order": frozenset({"purchase"}),
}
# The order of the reasons, from "nearly correct" to "no result". The best failure is reported.
_REASON_RANK = [
    "wrong_value",
    "wrong_order",
    "wrong_rows",
    "missing_column",
    "no_result",
]


@dataclass
class _Table:
    columns: list[str]
    rows: list[list[Any]]

    def column(self, name: str) -> list[Any]:
        i = self.columns.index(name)
        return [row[i] if i < len(row) else None for row in self.rows]


def values_match(ref: Any, got: Any, kind: str, expected: Expected) -> bool:
    """The tolerance rule: 0.5% or 0.01 (the larger), counts exactly, a ratio also as a percent."""
    if is_missing(ref) or is_missing(got):
        return is_missing(ref) and is_missing(got)
    if kind not in NUMBER_KINDS:
        return norm_key(ref, kind) == norm_key(got, kind)
    a, b = to_float(ref), to_float(got)
    if a is None or b is None:
        return False
    if kind == "count" and expected.abs_tol is None and expected.rel_tol is None:
        return abs(a - b) < 0.5
    rel = DEFAULT_REL if expected.rel_tol is None else expected.rel_tol
    floor = RATIO_ABS if kind == "ratio" else DEFAULT_ABS
    floor = floor if expected.abs_tol is None else expected.abs_tol
    if abs(a - b) <= max(abs(a) * rel, floor):
        return True
    if kind == "ratio":  # the agent shows the ratio as a percent
        return abs(a * 100 - b) <= max(abs(a * 100) * rel, floor * 100)
    return False


def _map_keys(
    ref: list[dict[str, Any]], table: _Table, expected: Expected
) -> tuple[dict[str, str], dict[str, int | None]] | None:
    """Map each reference key column to the agent column with the best value overlap."""
    mapping: dict[str, str] = {}
    years: dict[str, int | None] = {}
    for key in expected.key_columns():
        kind = expected.columns[key].kind
        ref_vals = [norm_key(r.get(key), kind) for r in ref]
        year = context_year(ref_vals, kind)
        years[key] = year
        wanted = {v for v in ref_vals if v is not None}
        best, best_score = None, 0.0
        for col in table.columns:
            if col in mapping.values():
                continue
            got = {norm_key(v, kind, year) for v in table.column(col)}
            score = len(wanted & got) / max(len(wanted), 1)
            if score > best_score:
                best, best_score = col, score
        if best is None:
            return None
        mapping[key] = best
    return mapping, years


def _agent_keys(
    table: _Table,
    mapping: dict[str, str],
    years: dict[str, int | None],
    expected: Expected,
) -> list[tuple[str | None, ...]]:
    cols = {k: table.column(c) for k, c in mapping.items()}
    out = []
    for i in range(len(table.rows)):
        out.append(
            tuple(
                norm_key(cols[k][i], expected.columns[k].kind, years[k])
                for k in expected.key_columns()
            )
        )
    return out


def _ref_key(row: dict[str, Any], expected: Expected) -> tuple[str | None, ...]:
    return tuple(
        norm_key(row.get(k), expected.columns[k].kind) for k in expected.key_columns()
    )


def _map_values(
    pairs: list[tuple[dict[str, Any], int]],
    table: _Table,
    expected: Expected,
    used: set[str],
) -> tuple[dict[str, str], list[str]]:
    """Map each reference value column to the agent column that matches the most aligned rows."""
    mapping: dict[str, str] = {}
    wrong: list[str] = []
    for col in expected.value_columns():
        kind = expected.columns[col].kind
        best, best_hits = None, -1
        for agent_col in table.columns:
            if agent_col in used or agent_col in mapping.values():
                continue
            values = table.column(agent_col)
            hits = sum(
                1
                for ref_row, i in pairs
                if values_match(ref_row.get(col), values[i], kind, expected)
            )
            if hits > best_hits:
                best, best_hits = agent_col, hits
        if best is None:
            wrong.append(col)
            continue
        mapping[col] = best
        if best_hits < len(pairs):
            wrong.append(col)
    return mapping, wrong


def meaning_words(name: str) -> set[str]:
    """The words of a column name without the generic words: `total_customers` -> {customer}."""
    words = re.findall(r"[a-z]+", name.lower())
    singular = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}
    return singular - GENERIC_WORDS


def same_meaning(ref_column: str, agent_column: str) -> bool:
    """True when the agent column name has a meaning word of the reference column, or a synonym."""
    wanted = meaning_words(ref_column)
    for word in list(wanted):
        wanted |= SYNONYMS.get(word, frozenset())
    return bool(wanted & meaning_words(agent_column))


def _number_columns(table: _Table) -> list[str]:
    """The agent columns that hold numbers, without the key and period columns."""
    out = []
    for name in table.columns:
        values = [v for v in table.column(name) if not is_missing(v)]
        if not values or any(to_float(v) is None for v in values):
            continue
        if not meaning_words(name) & KEY_WORDS:
            out.append(name)
    return out


def _score_value(
    ref: list[dict[str, Any]], table: _Table, expected: Expected
) -> ScoreResult:
    """One number. A matching cell counts only in a column with the meaning of the reference:
    the only number column of the result, or a column whose name has the same meaning. So a
    wrong column with the same value (orders instead of customers) does not pass."""
    if not ref:
        return ScoreResult.fail("no_result", note="the reference has no row")
    col = expected.value_columns()[0]
    kind = expected.columns[col].kind
    target = ref[0].get(col)
    numbers = _number_columns(table)
    other_meaning: list[str] = []
    for i, row in enumerate(table.rows):
        for j, value in enumerate(row):
            if not values_match(target, value, kind, expected):
                continue
            name = table.columns[j]
            if numbers == [name]:
                return ScoreResult.ok(row=i, column=name, rule="only_number_column")
            if same_meaning(col, name):
                return ScoreResult.ok(row=i, column=name, rule="column_name")
            if name not in other_meaning:
                other_meaning.append(name)
    if other_meaning:
        return ScoreResult.fail(
            "wrong_value",
            expected=target,
            problem="column_meaning",
            note=f"the value is only in columns without the meaning of {col!r}",
            columns=other_meaning,
        )
    return ScoreResult.fail("wrong_value", expected=target, got=table.rows[:3])


def score_table(
    ref: list[dict[str, Any]], table: _Table, expected: Expected
) -> ScoreResult:
    """Compare one agent result with the reference rows."""
    if not table.rows:
        empty_ref = all(all(is_missing(v) or v == 0 for v in r.values()) for r in ref)
        return (
            ScoreResult.ok(note="both empty")
            if empty_ref
            else ScoreResult.fail("no_result")
        )
    if expected.mode == "value" or not expected.key_columns():
        return _score_value(ref, table, expected)
    mapped = _map_keys(ref, table, expected)
    if mapped is None:
        return ScoreResult.fail("missing_column", columns=table.columns)
    key_map, years = mapped
    agent_keys = _agent_keys(table, key_map, years, expected)
    ref_rows = top_k_rows(ref, expected) if expected.mode == "top_k" else ref
    ref_index = {_ref_key(r, expected): r for r in ref_rows}
    ref_keys = [_ref_key(r, expected) for r in ref_rows]

    if expected.mode == "top_k":
        k = min(expected.k or len(ref), len(ref))
        first = agent_keys[:k]
        distinct = len(set(first)) == len(first)
        if len(first) < k or not distinct or any(key not in ref_index for key in first):
            return ScoreResult.fail("wrong_rows", expected=ref_keys, got=first)
        compare = list(range(k))
    elif expected.mode == "contains":
        missing = [key for key in ref_keys if key not in agent_keys]
        if missing:
            return ScoreResult.fail("wrong_rows", missing=missing[:10])
        compare = [agent_keys.index(key) for key in ref_keys]
    else:
        if sorted(map(str, agent_keys)) != sorted(map(str, ref_keys)):
            return ScoreResult.fail(
                "wrong_rows",
                missing=[k for k in ref_keys if k not in agent_keys][:10],
                extra=[k for k in agent_keys if k not in ref_index][:10],
            )
        if expected.mode == "rows_ordered" and agent_keys != ref_keys:
            return ScoreResult.fail("wrong_order", expected=ref_keys, got=agent_keys)
        compare = list(range(len(agent_keys)))

    pairs = [(ref_index[agent_keys[i]], i) for i in compare]
    value_map, wrong = _map_values(pairs, table, expected, set(key_map.values()))
    if wrong:
        return ScoreResult.fail(
            "wrong_value", columns=wrong, mapping={**key_map, **value_map}
        )
    return ScoreResult.ok(mapping={**key_map, **value_map})


def execution_match(
    expected: Expected,
    ref_columns: list[str],
    ref_rows: list[list[Any]],
    queries: list[QueryRecord],
) -> ScoreResult:
    """Pass when one agent result matches the reference. Otherwise report the best failure."""
    ref = [dict(zip(ref_columns, row, strict=False)) for row in ref_rows]
    missing = [c for c in expected.columns if c not in ref_columns]
    if missing:
        return ScoreResult.fail("missing_column", note=f"reference lacks {missing}")
    results = [q for q in queries if q.error_type is None]
    if not results:
        return ScoreResult.fail("no_result", note="the agent ran no successful query")
    best: ScoreResult | None = None
    for i, query in enumerate(results):
        outcome = score_table(ref, _Table(query.columns, query.rows), expected)
        if outcome.passed:
            outcome.details["query"] = i
            return outcome
        if best is None or _REASON_RANK.index(
            outcome.reason or "no_result"
        ) < _REASON_RANK.index(best.reason or "no_result"):
            best = outcome
    assert best is not None
    best.details["queries"] = len(results)
    return best
