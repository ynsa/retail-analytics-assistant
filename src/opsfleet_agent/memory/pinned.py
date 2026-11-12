"""Pinned facts: the facts of a conversation that must never get lost (spec 34, B2.2).

Code writes them from structured outputs and from the state. The model never writes them in free
text. They go into each prompt as a short YAML block.

| Fact | Source | Rule |
|---|---|---|
| `user` | `UserContext` | Read-only. At resume, the current `users.yaml` wins. |
| `data_range` | the value cache and today | Refreshed at each turn |
| `format` | `route.format_request` | The latest wins |
| `active_filters` | `write_sql.active_filters` | The latest value of each key wins |
| `standing_filters` | `route.set_filters` | Filters that the user set for the conversation |
| `user_definitions` | `route.set_definitions` | Terms that the user defined, at most 10 |
| `definitions_in_use` | `write_sql.definitions_used` | Union, at most 10 keys |
| `reports` | the report flow, the delete flow | The last 10 reports with their state, and a count |
| `last_results` | `finish` | The result references of the last data turn, at most 4 |
"""

from __future__ import annotations

from datetime import date
from typing import Any

import yaml

MAX_DEFINITIONS = 10
MAX_USER_DEFINITIONS = 10
MAX_STANDING_FILTERS = 5
MAX_FACT_CHARS = 160
MAX_REPORTS = 10
MAX_LAST_RESULTS = 4
BRANDS_CHANGED = "Your brands changed since the last session."
# The order of the facts in the prompt block.
PROMPT_KEYS = (
    "user",
    "data_range",
    "format",
    "standing_filters",
    "user_definitions",
    "active_filters",
    "definitions_in_use",
    "reports",
    "last_results",
    "notes",
)


def user_fact(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "role": user.role.value,
        "brands": list(user.brands) if user.brands else "all brands",
    }


def with_user(
    pinned: dict[str, Any] | None,
    user: Any,
    first_date: date | None,
    today: date,
) -> dict[str, Any]:
    """Refresh `user` and `data_range`. A change of the brands adds the pinned note."""
    out = dict(pinned or {})
    current = user_fact(user)
    before = out.get("user")
    if isinstance(before, dict) and before.get("brands") != current["brands"]:
        notes = [n for n in out.get("notes") or [] if n != BRANDS_CHANGED]
        out["notes"] = [*notes, BRANDS_CHANGED]
    out["user"] = current
    start = first_date.isoformat() if first_date else "unknown"
    out["data_range"] = f"{start}..{today.isoformat()}"
    return out


def _short(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_FACT_CHARS]


def with_route_facts(
    pinned: dict[str, Any] | None, route: dict[str, Any]
) -> dict[str, Any]:
    """The format request, and the filters and terms that the user set for the conversation.
    A new filter on the same field replaces the old one (the latest wins)."""
    out = dict(pinned or {})
    if route.get("format_request"):
        out["format"] = route["format_request"]
    filters = dict(out.get("standing_filters") or {})
    if route.get("clear_filters"):
        filters = {}
    for item in route.get("set_filters") or []:
        field = _short(item.get("field")).lower()
        value = _short(item.get("value"))
        if not field:
            continue
        if value:
            filters.pop(field, None)
            filters[field] = value
        else:
            filters.pop(field, None)
    if filters:
        out["standing_filters"] = dict(list(filters.items())[-MAX_STANDING_FILTERS:])
    else:
        out.pop("standing_filters", None)
    terms = dict(out.get("user_definitions") or {})
    for item in route.get("set_definitions") or []:
        term = _short(item.get("term")).lower()
        meaning = _short(item.get("meaning"))
        if term and meaning:
            terms.pop(term, None)
            terms[term] = meaning
    if terms:
        out["user_definitions"] = dict(list(terms.items())[-MAX_USER_DEFINITIONS:])
    return out


def with_sql_facts(
    pinned: dict[str, Any] | None, plans: list[dict[str, Any]]
) -> dict[str, Any]:
    """`active_filters` (the latest value of each key wins) and `definitions_in_use`."""
    out = dict(pinned or {})
    filters = dict(out.get("active_filters") or {})
    definitions = list(out.get("definitions_in_use") or [])
    for plan in plans:
        for key, value in (plan.get("active_filters") or {}).items():
            if value:
                filters[key] = value
        definitions += [
            k for k in plan.get("definitions_used") or [] if k not in definitions
        ]
    if filters:
        out["active_filters"] = filters
    if definitions:
        out["definitions_in_use"] = definitions[-MAX_DEFINITIONS:]
    return out


def with_reports(
    pinned: dict[str, Any] | None,
    turn_no: int,
    created: tuple[str, str] | None = None,
    deleted: list[str] | None = None,
    restored: list[str] | None = None,
) -> dict[str, Any]:
    """Created, deleted and restored reports of the conversation: the last 10, and a count."""
    out = dict(pinned or {})
    items = [dict(r) for r in out.get("reports") or []]
    total = int(out.get("report_count") or len(items))
    if created:
        report_id, title = created
        items = [r for r in items if r.get("id") != report_id]
        items.append(
            {
                "id": report_id,
                "title": _short(title),
                "turn": turn_no,
                "state": "created",
            }
        )
        total += 1
    for report_id in deleted or []:
        for item in items:
            if item.get("id") == report_id:
                item["state"] = "deleted"
    for report_id in restored or []:
        for item in items:
            if item.get("id") == report_id:
                item["state"] = "created"
    if items:
        out["reports"] = items[-MAX_REPORTS:]
        out["report_count"] = total
    return out


def prompt_block(pinned: dict[str, Any] | None) -> str:
    """The pinned facts as a short YAML block for the prompt."""
    if not pinned:
        return "(none)"
    facts: dict[str, Any] = {}
    for key in PROMPT_KEYS:
        value = pinned.get(key)
        if not value:
            continue
        if key == "reports":
            value = [
                f"{r['id']} ({r.get('state', 'created')}, turn {r.get('turn', '?')}): "
                f"{r.get('title', '')}"
                for r in value
            ]
            count = int(pinned.get("report_count") or len(value))
            if count > len(value):
                value.append(f"and {count - len(value)} older reports")
        if key == "last_results":
            value = [
                f"{r.get('ref')} (turn {r.get('turn', '?')}): {r.get('purpose', '')}"
                for r in value
            ]
        facts[key] = value
    if not facts:
        return "(none)"
    return yaml.safe_dump(
        facts, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100
    ).strip()
