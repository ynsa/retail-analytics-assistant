"""Build a report from the draft and the stored results (spec 32, B2.4). All code, no model.

1. Text: strip links, images and HTML. Cut at about 1,200 words.
2. Tables: the columns of a stored result, at most 15 rows.
3. Charts: see `charts.py`.
4. Date range and definitions: from the plan period, today, the brand scope and the glossary.
5. Method and SQL appendix: purpose, SQL, job ID and bytes billed of each query.
6. Calculations and the figure check: `check_slots()`. The flow sends one rewrite request for the
   texts with untraceable numbers.
7. Entities for the delete flow (spec 33).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from opsfleet_agent.agent.calc import (
    check_figures,
    numbers_in,
    resolve_placeholders,
    result_numbers,
)
from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.formatting import ResultData, format_value
from opsfleet_agent.agent.nodes.common import brands_text, is_manager
from opsfleet_agent.agent.periods import Period, format_day
from opsfleet_agent.config import ReportSettings
from opsfleet_agent.identity import UserContext
from opsfleet_agent.reports.models import ReportDraft
from opsfleet_agent.reports.text import (
    clean_model_text,
    normalize_value,
    one_line,
    word_count,
)

ENTITY_KINDS = ("brand", "category", "department", "product", "state", "country")
MAX_ENTITIES_PER_KIND = 50
# Result columns that name an entity (spec 32, B2.4 step 7).
ENTITY_RESULT_COLUMNS = {
    "brand": "brand",
    "brand_name": "brand",
    "category": "category",
    "product_category": "category",
    "department": "department",
    "name": "product",
    "product": "product",
    "product_name": "product",
    "state": "state",
    "user_state": "state",
    "country": "country",
    "user_country": "country",
}


# --- tables ---------------------------------------------------------------------------------------
def build_table(
    results: dict[str, ResultData], ref: str, columns: list[str], max_rows: int
) -> dict[str, Any] | None:
    """A table from a stored result: the chosen columns (all when none is valid)."""
    result = results.get(ref)
    if result is None or not result["columns"]:
        return None
    names = [c.lower() for c in result["columns"]]
    wanted = [names.index(c.lower()) for c in columns if c.lower() in names]
    index = list(dict.fromkeys(wanted)) or list(range(len(names)))
    rows = [
        [format_value(row[i], result["kinds"][i]) for i in index]
        for row in result["rows"][:max_rows]
    ]
    note = None
    if len(result["rows"]) > max_rows:
        note = f"The table shows the top {max_rows} of {len(result['rows'])} rows."
    return {
        "ref": ref,
        "columns": [result["columns"][i] for i in index],
        "rows": rows,
        "note": note,
    }


# --- figure check ---------------------------------------------------------------------------------
@dataclass
class SlotCheck:
    texts: dict[str, str]  # slot key -> final text
    calculated: int = 0  # placeholders that code computed
    calc_errors: int = 0
    unverified: int = 0  # numbers replaced by "[figure removed: not traceable]"
    unverified_keys: list[str] = field(default_factory=list)
    checked: int = 0


def draft_slots(draft: ReportDraft) -> dict[str, str]:
    """Every text of the draft that can hold a figure, by a stable key."""
    slots: dict[str, str] = {}
    for i, text in enumerate(draft.summary):
        slots[f"summary.{i}"] = text
    for i, finding in enumerate(draft.findings):
        slots[f"findings.{i}"] = finding.text
    for i, text in enumerate(draft.reasons):
        slots[f"reasons.{i}"] = text
    for i, item in enumerate(draft.action_items):
        slots[f"action_items.{i}.action"] = item.action
        slots[f"action_items.{i}.why"] = item.why
        slots[f"action_items.{i}.metric_to_watch"] = item.metric_to_watch
    for i, chart in enumerate(draft.charts):
        slots[f"charts.{i}.title"] = chart.title
    return slots


def allowed_numbers(
    results: Iterable[ResultData], question: str, user: UserContext
) -> list[float]:
    """Values that explain a number: results, the user's request, the number of brands."""
    allowed = result_numbers(results)
    allowed += numbers_in(question)
    allowed.append(float(len(user.brands)))
    return allowed


def check_slots(
    slots: dict[str, str],
    results: list[ResultData],
    allowed: list[float],
    removed_text: str,
    on_calc_error: Callable[[str], None] | None = None,
) -> SlotCheck:
    """Placeholders first, then the figure check on each text (spec 32, B2.4 step 6)."""
    out = SlotCheck(texts={})
    for key, raw in slots.items():
        calc = resolve_placeholders(
            clean_model_text(raw), results, on_error=on_calc_error
        )
        check = check_figures(calc.text, allowed + calc.computed, removed_text)
        out.texts[key] = check.text
        out.calculated += len(calc.computed)
        out.calc_errors += calc.errors
        out.checked += check.checked
        if check.unverified:
            out.unverified += check.unverified
            out.unverified_keys.append(key)
    return out


def apply_slots(draft: ReportDraft, texts: dict[str, str]) -> ReportDraft:
    """The draft with the checked texts."""
    data = draft.model_dump()
    for key, text in texts.items():
        parts = key.split(".")
        node: Any = data[parts[0]]
        index = int(parts[1])
        if parts[0] in ("summary", "reasons"):
            node[index] = text
        elif parts[0] == "findings":
            node[index]["text"] = text
        else:
            node[index][parts[2]] = text
    return ReportDraft.model_validate(data)


# --- word limit -----------------------------------------------------------------------------------
def limit_words(
    sections: dict[str, Any], max_words: int
) -> tuple[dict[str, Any], bool]:
    """Keep the model text at about `max_words` words. Items at the end of a section go first.
    Each section keeps at least its first item, so the 6 sections always appear."""
    order = ("summary", "findings", "reasons", "action_items")

    def words(key: str, item: Any) -> int:
        if key == "findings":
            return word_count(item["text"])
        if key == "action_items":
            return sum(
                word_count(item[k]) for k in ("action", "why", "metric_to_watch")
            )
        return word_count(item)

    total, cut = 0, False
    out = dict(sections)
    for key in order:
        kept = []
        for i, item in enumerate(sections.get(key) or []):
            count = words(key, item)
            if i > 0 and total + count > max_words:
                cut = True
                continue
            kept.append(item)
            total += count
        out[key] = kept
    return out, cut


# --- entities -------------------------------------------------------------------------------------
def _add(found: dict[str, list[str]], kind: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    values = found.setdefault(kind, [])
    norm = normalize_value(value)
    if len(values) < MAX_ENTITIES_PER_KIND and norm not in {
        normalize_value(v) for v in values
    }:
        values.append(value.strip())


def brands_in_text(text: str, brand_names: Iterable[str]) -> list[str]:
    """Exact brand names in a text. The longest name wins: "Calvin Klein Jeans" does not also
    give "Calvin Klein"."""
    lowered = (text or "").casefold()
    found: list[str] = []
    taken: list[tuple[int, int]] = []
    for name in sorted(set(brand_names), key=len, reverse=True):
        if len(name) < 3:
            continue
        pattern = re.compile(rf"(?<![\w']){re.escape(name.casefold())}(?![\w'])")
        for match in pattern.finditer(lowered):
            span = match.span()
            if any(a < span[1] and span[0] < b for a, b in taken):
                continue
            taken.append(span)
            if name not in found:
                found.append(name)
    return found


def report_entities(
    queries: list[dict[str, Any]],
    results: list[ResultData],
    user: UserContext,
    title: str,
    brand_names: Iterable[str],
) -> dict[str, list[str]]:
    """The entities of B2.4 step 7: filters, result columns, the user's brands, the title."""
    found: dict[str, list[str]] = {}
    for query in queries:
        for kind, values in (query.get("entities") or {}).items():
            for value in values:
                _add(found, kind, value)
    texts: set[str] = set()
    for result in results:
        for i, column in enumerate(result["columns"]):
            kind = ENTITY_RESULT_COLUMNS.get(column.lower())
            for row in result["rows"]:
                value = row[i]
                if isinstance(value, str):
                    texts.add(value.casefold())
                    if kind:
                        _add(found, kind, value)
    for brand in user.brands:
        if brand.casefold() in texts:
            _add(found, "brand", brand)
    for brand in brands_in_text(title, brand_names):
        _add(found, "brand", brand)
    return {k: v for k, v in found.items() if k in ENTITY_KINDS and v}


# --- code sections --------------------------------------------------------------------------------
def scope_line(user: UserContext) -> str:
    if is_manager(user):
        return (
            f"Scope: the brands {brands_text(user.brands)}. Customer figures cover only the "
            "customers who bought these brands."
        )
    return "Scope: all brands and all customers (CEO)."


def date_range_section(
    period: Period | None,
    compare: Period | None,
    period_note: str | None,
    today: date,
    user: UserContext,
    definitions: list[str],
    lib: PromptLibrary,
) -> dict[str, Any]:
    lines = [f"Period: {period.text()}." if period else "Period: all data up to today."]
    if compare:
        lines.append(f"Compared with: {compare.text()}.")
    if period_note:
        lines.append(period_note)
    lines.append(f"Created: {format_day(today)} (UTC).")
    lines.append(scope_line(user))
    items = [
        {"key": key, "meaning": lib.definitions[key].meaning}
        for key in dict.fromkeys(definitions)
        if key in lib.definitions and lib.definitions[key].meaning
    ]
    return {"lines": lines, "definitions": items}


def method_section(
    queries: list[dict[str, Any]],
    user: UserContext,
    calculated: int,
    unverified: int,
    notes: list[str],
) -> dict[str, Any]:
    lines = [
        f"{calculated} figure in the text is a code calculation from the query results."
        if calculated == 1
        else f"{calculated} figures in the text are code calculations from the query results."
    ]
    if unverified:
        lines.append(
            f"{unverified} figure was removed because no query result explains it."
            if unverified == 1
            else f"{unverified} figures were removed because no query result explains them."
        )
    if is_manager(user):
        lines.append(
            f"The system added the brand scope ({', '.join(user.brands)}) to each query "
            "before it ran. The SQL below is the SQL of the model, before this change."
        )
    else:
        lines.append("The SQL guard checked each query before it ran.")
    items = [
        {
            "id": q.get("id", ""),
            "purpose": one_line(q.get("purpose", "")),
            "sql": q.get("sql", ""),
            "job_id": q.get("job_id") or "",
            "bytes_billed": int(q.get("bytes_billed") or 0),
            "status": q.get("status", "ok"),
            "reason": q.get("reason") or "",
        }
        for q in queries
    ]
    return {"lines": lines, "queries": items, "notes": notes}


def build_sections(
    draft: ReportDraft,
    results: dict[str, ResultData],
    settings: ReportSettings,
    missing: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """The model sections after the limits: findings with tables, action items."""
    notes: list[str] = []
    findings = []
    for finding in draft.findings[: settings.max_findings]:
        table = None
        if finding.table is not None:
            table = build_table(
                results,
                finding.table.ref,
                finding.table.columns,
                settings.max_table_rows,
            )
        findings.append({"text": clean_model_text(finding.text), "table": table})
    if len(draft.findings) > settings.max_findings:
        notes.append(f"The report shows the first {settings.max_findings} findings.")
    actions = [
        {
            "action": one_line(a.action),
            "why": one_line(a.why),
            "metric_to_watch": one_line(a.metric_to_watch),
        }
        for a in draft.action_items[: settings.max_action_items]
        if a.action.strip()
    ]
    if len(draft.action_items) > settings.max_action_items:
        notes.append(
            f"The report shows the first {settings.max_action_items} action items."
        )
    if len(actions) < settings.min_action_items:
        notes.append(
            f"The data supports only {len(actions)} action items "
            f"(the target is {settings.min_action_items} to {settings.max_action_items})."
        )
    sections = {
        "summary": [clean_model_text(s) for s in draft.summary if s.strip()],
        "findings": findings,
        "missing": missing,
        "reasons": [clean_model_text(r) for r in draft.reasons if r.strip()],
        "action_items": actions,
    }
    sections, cut = limit_words(sections, settings.max_words)
    if cut:
        notes.append(f"The text was cut to about {settings.max_words:,} words.")
    return sections, notes
