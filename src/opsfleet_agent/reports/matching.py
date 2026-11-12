"""Which reports a delete request means (spec 33, B2.2). All matching is SQL in code.

The model only gives the scope and the name X. Code finds the user's active reports:

- `conversation`: the reports of this conversation.
- `ids`: the named IDs. IDs that are missing, deleted or of another user are "not found".
- `all`: all active reports of the user.
- `entity`: an exact match on the report entities when X is a known name. When X is not known,
  close names stop the request first ("Calvin" is close to "Calvin Klein"). Else a text search.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

from opsfleet_agent.guard.value_cache import ValueCache
from opsfleet_agent.identity import UserContext
from opsfleet_agent.reports.ids import normalize_id
from opsfleet_agent.reports.library import FoundReport, Matcher, ReportLibrary
from opsfleet_agent.reports.text import normalize_value

MatchKind = Literal["matches", "none", "close_names", "unclear", "too_many"]
CLOSE_RATIO = 0.8
MAX_CLOSE_NAMES = 5
_QUOTES = "\"'“”‘’`"
_LEADING = re.compile(r"^(?:client|brand|the)\s+")
_FTS_SPECIAL = re.compile(r"[\"*^():{}\[\]+\-]")
# Value-cache columns that hold entity names (spec 33, B2.2 step 2).
KNOWN_VALUE_COLUMNS = (
    ("products", "brand"),
    ("products", "category"),
    ("products", "department"),
    ("users", "state"),
    ("users", "country"),
)


@dataclass(frozen=True)
class MatchRow:
    report_id: str
    title: str
    created_at: str
    reason: str
    files: tuple[str, ...] = ()


@dataclass
class MatchResult:
    kind: MatchKind
    scope: str | None
    entity: str | None = None
    rows: list[MatchRow] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    close_names: list[str] = field(default_factory=list)
    total: int = 0


def normalize_entity(text: str) -> str:
    """Strip quotes, lower case, trim, single spaces, strip a leading client, brand or the."""
    value = normalize_value(text or "")
    previous = None
    while previous != value:
        previous = value
        value = _LEADING.sub("", value.strip(_QUOTES).strip()).strip()
    return value


def fts_phrase(text: str) -> str | None:
    """A safe FTS5 phrase query: no operators, no quotes. None when nothing is left."""
    words = " ".join(_FTS_SPECIAL.sub(" ", text).split())
    return f'"{words}"' if words else None


def _known(norm: str, user_values: set[str], values: ValueCache) -> bool:
    if norm in user_values:
        return True
    return any(
        norm == normalize_value(v)
        for table, column in KNOWN_VALUE_COLUMNS
        if values.has(table, column)
        for v in values.get(table, column)
    )


def close_names(norm: str, entity_values: list[tuple[str, str, str]]) -> list[str]:
    """Entity values of the user's reports that start with X, or look like X."""
    found: list[str] = []
    for _kind, value, value_norm in entity_values:
        if value_norm == norm or value in found:
            continue
        ratio = difflib.SequenceMatcher(None, norm, value_norm).ratio()
        if value_norm.startswith(norm) or ratio >= CLOSE_RATIO:
            found.append(value)
    return found[:MAX_CLOSE_NAMES]


def _rows(found: list[FoundReport], reason: str | None = None) -> list[MatchRow]:
    rows = []
    for item in found:
        text = reason
        if text is None and item.reason_kind:
            text = f"names {item.reason_kind} {item.reason_value}"
        s = item.summary
        files = tuple(f for f in (s.file_html, s.file_md) if f)
        rows.append(MatchRow(s.report_id, s.title, s.created_at, text or "", files))
    return rows


def match_reports(
    library: ReportLibrary,
    values: ValueCache,
    user: UserContext,
    *,
    scope: str | None,
    entity: str | None,
    ids: list[str],
    conversation_id: str,
    max_reports: int,
) -> MatchResult:
    result = _match(library, values, user, scope, entity, ids, conversation_id)
    result.total = len(result.rows)
    if result.kind == "matches" and not result.rows:
        result.kind = "none"
    if result.kind == "matches" and len(result.rows) > max_reports:
        result.kind = "too_many"
    return result


def _match(
    library: ReportLibrary,
    values: ValueCache,
    user: UserContext,
    scope: str | None,
    entity: str | None,
    ids: list[str],
    conversation_id: str,
) -> MatchResult:
    if scope == "conversation":
        found = library.find(
            user, Matcher("conversation", conversation_id=conversation_id)
        )
        return MatchResult("matches", scope, rows=_rows(found, "this conversation"))
    if scope == "all":
        found = library.find(user, Matcher("all"))
        return MatchResult("matches", scope, rows=_rows(found, "all your reports"))
    if scope == "ids":
        wanted = [(raw, normalize_id(raw, "R")) for raw in ids if raw and raw.strip()]
        if not wanted:
            return MatchResult("unclear", scope)
        canonical = tuple(dict.fromkeys(c for _, c in wanted if c))
        found = library.find(user, Matcher("ids", ids=canonical)) if canonical else []
        got = {f.summary.report_id for f in found}
        missing = [c or raw.strip()[:20] for raw, c in wanted if c not in got]
        return MatchResult(
            "matches",
            scope,
            rows=_rows(found, "named by ID"),
            not_found=list(dict.fromkeys(missing)),
        )
    if scope != "entity" or not entity or not normalize_entity(entity):
        return MatchResult("unclear", scope, entity=entity)

    shown = entity.strip().strip(_QUOTES).strip()
    norm = normalize_entity(entity)
    entity_values = library.entity_values(user)
    user_norms = {v[2] for v in entity_values}
    if _known(norm, user_norms, values):
        found = library.find(user, Matcher("entity", value_norm=norm))
        if found:
            return MatchResult("matches", scope, entity=shown, rows=_rows(found))
        close = close_names(norm, entity_values)
        if close:
            return MatchResult("close_names", scope, entity=shown, close_names=close)
        return MatchResult("none", scope, entity=shown)
    close = close_names(norm, entity_values)
    if close:
        return MatchResult("close_names", scope, entity=shown, close_names=close)
    phrase = fts_phrase(norm)
    found = library.find(user, Matcher("text", phrase=phrase)) if phrase else []
    return MatchResult(
        "matches", scope, entity=shown, rows=_rows(found, f"text contains '{shown}'")
    )
