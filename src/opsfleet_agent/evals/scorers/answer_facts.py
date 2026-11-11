"""Text scorers: `answer_facts` (figures of the snapshot in the answer) and `facts` (required words).

Spec 21, B2.6. A figure matches within 1%. The scorer reads "37,218", "$37.2K", "37.2 thousand",
"1.8M" and "25%". A ratio also matches as a percent.
"""

from __future__ import annotations

import re
from typing import Any

from opsfleet_agent.evals.cases import Expected, FactSpec, RequiredSpec
from opsfleet_agent.evals.normalize import TIME_KINDS, norm_key, to_float
from opsfleet_agent.evals.scorers import ScoreResult
from opsfleet_agent.evals.snapshot import top_k_rows
from opsfleet_agent.evals.textnum import contains_figure

_RANK = re.compile(r"^@rank(\d+)$")
MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def find_row(
    rows: list[dict[str, Any]], where: dict[str, Any] | None, expected: Expected | None
):
    """The snapshot row of a fact: the first row, the row of rank N (`@rankN`), or a key match."""
    if not rows:
        return None
    if not where:
        return rows[0]
    for col, wanted in where.items():
        m = _RANK.match(str(wanted))
        if m:
            index = int(m.group(1)) - 1
            return rows[index] if index < len(rows) else None
    for row in rows:
        ok = True
        for col, wanted in where.items():
            kind = (
                expected.columns[col].kind
                if expected and col in expected.columns
                else "text"
            )
            if norm_key(row.get(col), kind) != norm_key(wanted, kind):
                ok = False
                break
        if ok:
            return row
    return None


def time_words(value: Any, kind: str) -> list[str]:
    """Ways to write a month, quarter or year in a text: "2025-01", "January 2025", "Jan 2025"."""
    norm = norm_key(value, kind)
    if not norm:
        return []
    if kind == "month":
        year, month = norm.split("-")
        name = MONTH_NAMES[int(month) - 1]
        return [norm, f"{name} {year}", f"{name[:3]} {year}"]
    if kind == "quarter":
        year, q = norm.split("-")
        return [norm, f"{q} {year}", f"{year} {q}"]
    return [norm]


def _text_has(text: str, value: Any, kind: str) -> bool:
    if kind in TIME_KINDS:
        if any(w.casefold() in text.casefold() for w in time_words(value, kind)):
            return True
        norm = norm_key(value, kind)
        if kind == "month" and norm and norm[:4] in text:
            name = MONTH_NAMES[int(norm[5:7]) - 1]
            return re.search(rf"\b{name}\b", text) is not None  # "May", not "may"
        return False
    number = to_float(value)
    if number is not None and kind != "text":
        return contains_figure(text, number, kind=kind)
    return str(value).casefold() in text.casefold()


def answer_facts(
    facts: list[FactSpec],
    expected: Expected | None,
    columns: list[str],
    rows: list[list[Any]],
    text: str,
) -> ScoreResult:
    ref = [dict(zip(columns, row, strict=False)) for row in rows]
    missing: list[str] = []
    checked: list[str] = []
    for fact in facts:
        row = find_row(ref, fact.where, expected)
        label = fact.label or f"{fact.column} {fact.where or ''}".strip()
        if row is None or row.get(fact.column) is None:
            missing.append(f"{label}: not in the snapshot")
            continue
        kind = (
            expected.columns[fact.column].kind
            if expected and fact.column in expected.columns
            else "number"
        )
        if expected and fact.column in expected.key_columns():
            kind = expected.columns[fact.column].kind
        value = row[fact.column]
        checked.append(label)
        if not _text_has(text, value, kind):
            missing.append(f"{label} = {value}")
    if expected is not None and expected.mode == "top_k" and expected.key_columns():
        missing += _top_k_names(ref, expected, text)
    if missing:
        return ScoreResult.fail("fact_missing", missing=missing, checked=checked)
    return ScoreResult.ok(checked=checked)


def _top_k_names(ref: list[dict[str, Any]], expected: Expected, text: str) -> list[str]:
    """The names of the first k keys. With a tie at the border, enough of the tied names."""
    key = expected.key_columns()[0]
    kind = expected.columns[key].kind
    k = min(expected.k or len(ref), len(ref))
    rows = top_k_rows(ref, expected)
    border_value = rows[k - 1].get(expected.value_columns()[0]) if rows else None
    sure = [r for r in rows[:k] if r.get(expected.value_columns()[0]) != border_value]
    tied = [r for r in rows if r.get(expected.value_columns()[0]) == border_value]
    need_tied = k - len(sure)
    missing = [
        f"top-{k} name {r.get(key)}"
        for r in sure
        if not _text_has(text, r.get(key), kind)
    ]
    found_tied = sum(1 for r in tied if _text_has(text, r.get(key), kind))
    if found_tied < need_tied:
        missing.append(f"{need_tied - found_tied} of the tied names at rank {k}")
    return missing


def required_facts(
    required: list[RequiredSpec], forbidden: list[str], text: str
) -> ScoreResult:
    """The `facts` scorer: each required item is in the text, and no forbidden word is."""
    folded = text.casefold()
    missing: list[str] = []
    for item in required:
        options = list(item.any)
        if item.number is not None:
            options.append(item.number)
        found = False
        for option in options:
            if isinstance(option, int | float) and not isinstance(option, bool):
                found = contains_figure(text, float(option), rel=item.tol)
            else:
                found = str(option).casefold() in folded
            if found:
                break
        if not found:
            missing.append(" | ".join(str(o) for o in options))
    present = [w for w in forbidden if w.casefold() in folded]
    if missing or present:
        return ScoreResult.fail(
            "fact_missing", missing=missing, forbidden_present=present
        )
    return ScoreResult.ok()
