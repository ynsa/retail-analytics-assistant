"""Empty-result checks (spec 30, B2.7): is the data really empty, or is the query wrong?

The checks use the filters of the guard result. Checks 1 and 2 need no query when the value cache
has the values. Code builds each probe query from the parsed SQL, and each probe goes through the
same guard and scope rewrite, so a probe never reads data outside the user's scope. Each probe counts
in the turn budget. At most 5 probe queries run for one empty result; usually 0 to 2.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

from opsfleet_agent.config import Role
from opsfleet_agent.errors import AgentError, LimitExceeded
from opsfleet_agent.guard.filters import FilterLiteral
from opsfleet_agent.guard.sql_guard import GuardResult, SqlGuard
from opsfleet_agent.guard.value_cache import ValueCache, close_names
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.tracing import span

MAX_PROBES = 5
MAX_CONDITIONS = 6
MAX_LISTED_VALUES = 20
_DATE_TYPES = frozenset({"DATE", "DATETIME", "TIMESTAMP"})


class DiagnosisKind(StrEnum):
    ERROR = "error"  # the query is wrong: back to the model (spec 31 loop)
    TRUE_ZERO = "true_zero"  # the data has no rows for these filters
    UNKNOWN = "unknown"  # the checks could not run (no queries left in the turn)


@dataclass(frozen=True)
class EmptyDiagnosis:
    kind: DiagnosisKind
    finding: str  # a short code, for example "value_missing" or "before_data"
    model_feedback: str | None  # only for ERROR
    user_note: str
    queries_run: int


class _OutOfBudget(Exception):
    pass


@dataclass
class _Run:
    budget: TurnBudget
    queries: int = 0


def _date_of(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def _own_conditions(conds: list[exp.Expression], alias: str) -> list[exp.Expression]:
    """The conditions that read only the source `alias`."""
    return [
        c for c in conds if {col.table for col in c.find_all(exp.Column)} == {alias}
    ]


def _arg(node: exp.Expression, name: str) -> Any:
    """sqlglot 30 stores FROM and WITH as `from_` and `with_`; older versions without "_"."""
    return node.args.get(f"{name}_") or node.args.get(name)


def _quote(value: str) -> str:
    return exp.Literal.string(value).sql(dialect="bigquery")


class EmptyResultChecker:
    def __init__(
        self,
        guard: SqlGuard,
        bq: Any,
        values: ValueCache,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.guard = guard
        self.bq = bq
        self.values = values
        self._today = today or (lambda: datetime.now(UTC).date())

    def diagnose(self, guard_result: GuardResult, budget: TurnBudget) -> EmptyDiagnosis:
        run = _Run(budget)
        with span("guard.empty_check", "guard") as sp:
            try:
                diagnosis = self._diagnose(guard_result, run)
            except _OutOfBudget:
                diagnosis = EmptyDiagnosis(
                    DiagnosisKind.UNKNOWN,
                    "budget",
                    None,
                    "The query returned no rows. The checks for the reason did not run, "
                    "because this answer has no queries left.",
                    run.queries,
                )
            sp.set_attribute("diagnosis", diagnosis.kind.value)
            sp.set_attribute("finding", diagnosis.finding)
            sp.set_attribute("queries_run", diagnosis.queries_run)
        return diagnosis

    # --- the checks -------------------------------------------------------------------------------
    def _diagnose(self, result: GuardResult, run: _Run) -> EmptyDiagnosis:
        return (
            self._cached_values(result, run)  # check 1, from the cache
            or self._date_range(result, run)  # check 2
            or self._probe_values(result, run)  # check 1, other columns
            or self._brand_scope(result, run)  # check 3
            or self._filters_and_joins(result, run)  # checks 4 and 5
            or self._true_zero(result, run, "no_rows", "")
        )

    def _cached_values(self, result: GuardResult, run: _Run) -> EmptyDiagnosis | None:
        for f in result.filters:
            if f.op not in ("=", "in") or not self.values.has(f.table, f.column):
                continue
            known = self.values.get(f.table, f.column)
            for value in f.values:
                if value in known:
                    continue
                if len(known) <= MAX_LISTED_VALUES:
                    hint = f"Existing values: {', '.join(known)}."
                else:
                    close = close_names(value, known)
                    hint = f"Close values: {', '.join(close)}." if close else ""
                return EmptyDiagnosis(
                    DiagnosisKind.ERROR,
                    "value_missing",
                    f"The value '{value}' does not exist in {f.table}.{f.column}. {hint}".strip(),
                    f"The value '{value}' does not exist in {f.table}.{f.column}.",
                    run.queries,
                )
        return None

    def _period(self, filters: list[FilterLiteral]) -> tuple[date | None, date | None]:
        """The date interval [low, high) that the exact date filters allow."""
        low: date | None = None
        high: date | None = None

        def tighten(lo: date | None = None, hi: date | None = None) -> None:
            nonlocal low, high
            if lo is not None and (low is None or lo > low):
                low = lo
            if hi is not None and (high is None or hi < high):
                high = hi

        for f in filters:
            if f.column_type not in _DATE_TYPES or not f.exact:
                continue
            if f.op == "year" and f.values[0].lstrip("-").isdigit():
                year = int(f.values[0])
                tighten(date(year, 1, 1), date(year + 1, 1, 1))
                continue
            days = [_date_of(v) for v in f.values]
            if any(d is None for d in days):
                continue
            first = days[0]
            assert first is not None
            if f.op == "=":
                tighten(first, first + timedelta(days=1))
            elif f.op in (">", ">="):
                tighten(lo=first)
            elif f.op == "<":
                tighten(hi=first)
            elif f.op == "<=":
                tighten(hi=first + timedelta(days=1))
            elif f.op == "between" and days[1] is not None:
                tighten(first, days[1] + timedelta(days=1))
        return low, high

    def _range_text(self, result: GuardResult) -> str:
        tables = [t for t in result.tables if t in self.values.date_min]
        first = (
            min((d for t in tables if (d := self.values.first_date(t))), default=None)
            or self.values.first_date()
        )
        return f"The data covers {first} to {self._today()} (UTC)."

    def _date_range(self, result: GuardResult, run: _Run) -> EmptyDiagnosis | None:
        low, high = self._period(result.filters)
        if low is not None and high is not None and low >= high:
            return EmptyDiagnosis(
                DiagnosisKind.ERROR,
                "empty_period",
                "The date filters exclude each other: the period is empty. Check the dates.",
                "The date filters of the query give an empty period.",
                run.queries,
            )
        dated = [f for f in result.filters if f.column_type in _DATE_TYPES and f.exact]
        first = min(
            (d for f in dated if (d := self.values.first_date(f.table))), default=None
        )
        if high is not None and first is not None and high <= first:
            return self._true_zero(
                result, run, "before_data", "The period is before the data starts."
            )
        if low is not None and low > self._today():
            return self._true_zero(
                result, run, "after_today", "The period is after today."
            )
        return None

    def _probe_values(self, result: GuardResult, run: _Run) -> EmptyDiagnosis | None:
        """Check 1 for string columns that the cache does not hold: one COUNTIF probe."""
        checks = [
            (f, v)
            for f in result.filters
            if f.op in ("=", "in")
            and f.column_type == "STRING"
            and not self.values.has(f.table, f.column)
            for v in f.values
        ][:MAX_CONDITIONS]
        if not checks:
            return None
        dataset = self.guard.schema.dataset
        parts = [
            f"(SELECT COUNTIF(t.{f.column} = {_quote(v)}) FROM `{dataset}.{f.table}` AS t) AS v{i}"
            for i, (f, v) in enumerate(checks)
        ]
        frame = self._probe("SELECT " + ", ".join(parts), run)
        if frame is None or frame.empty:
            return None
        for i, (f, v) in enumerate(checks):
            if int(frame.iloc[0][f"v{i}"] or 0) == 0:
                return EmptyDiagnosis(
                    DiagnosisKind.ERROR,
                    "value_missing",
                    f"The value '{v}' does not exist in {f.table}.{f.column} (for this user's "
                    "scope). Check the spelling and the letter case.",
                    f"The value '{v}' does not exist in {f.table}.{f.column}.",
                    run.queries,
                )
        return None

    def _brand_scope(self, result: GuardResult, run: _Run) -> EmptyDiagnosis | None:
        """Check 3 (manager): the user's brands have order items in the period at all."""
        if self.guard.user.role is not Role.MANAGER:
            return None
        low, high = self._period(result.filters)
        conds = []
        if low is not None:
            conds.append(f"oi.created_at >= TIMESTAMP('{low.isoformat()}')")
        if high is not None:
            conds.append(f"oi.created_at < TIMESTAMP('{high.isoformat()}')")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        sql = f"SELECT COUNT(*) AS n FROM `{self.guard.schema.dataset}.order_items` AS oi{where}"
        frame = self._probe(sql, run)
        if frame is None or frame.empty or int(frame.iloc[0]["n"] or 0) > 0:
            return None
        brands = " and ".join(self.guard.user.brands)
        return self._true_zero(
            result,
            run,
            "brand_no_sales",
            f"Your brands ({brands}) have no sales in this period.",
        )

    def _filters_and_joins(
        self, result: GuardResult, run: _Run
    ) -> EmptyDiagnosis | None:
        """Checks 4 and 5 in one probe: which condition empties the result, and the join."""
        if not result.qualified_sql:
            return None
        root = sqlglot.parse_one(result.qualified_sql, read="bigquery")
        if not isinstance(root, exp.Select) or root.args.get("qualify") is not None:
            return None
        from_ = _arg(root, "from")
        if from_ is None:
            return None
        joins = list(root.args.get("joins") or [])
        where = root.args.get("where")
        conds: list[exp.Expression] = []
        if where is not None:
            conds = (
                list(where.this.flatten())
                if isinstance(where.this, exp.And)
                else [where.this]
            )
        check_filters = 0 < len(conds) <= MAX_CONDITIONS
        if not check_filters and not joins:
            return None

        source = " ".join(
            [from_.sql(dialect="bigquery")] + [j.sql(dialect="bigquery") for j in joins]
        )

        def count(source_sql: str, parts: list[exp.Expression]) -> str:
            cond = " AND ".join(p.sql(dialect="bigquery") for p in parts)
            return f"(SELECT COUNT(*) {source_sql}{' WHERE ' + cond if cond else ''})"

        columns: dict[str, str] = {"no_filters": count(source, [])}
        if check_filters and len(conds) > 1:
            for i in range(len(conds)):
                columns[f"drop_{i}"] = count(source, conds[:i] + conds[i + 1 :])
        aliases: list[str] = []
        if joins:
            for i, node in enumerate([from_.this] + [j.this for j in joins]):
                alias = node.alias_or_name
                own = _own_conditions(conds, alias)
                columns[f"table_{i}"] = count(
                    f"FROM {node.sql(dialect='bigquery')}", own
                )
                aliases.append(alias)
        with_ = _arg(root, "with")
        prefix = with_.sql(dialect="bigquery") + " " if with_ is not None else ""
        sql = prefix + "SELECT " + ", ".join(f"{v} AS {k}" for k, v in columns.items())
        frame = self._probe(sql, run)
        if frame is None or frame.empty:
            return None
        row = frame.iloc[0]
        for i, alias in enumerate(aliases):
            if int(row[f"table_{i}"] or 0) == 0:
                return self._filter_finding(
                    result,
                    run,
                    _own_conditions(conds, alias),
                    f"The table '{alias}' has no rows for its filters.",
                )
        if joins and int(row["no_filters"] or 0) == 0:
            return EmptyDiagnosis(
                DiagnosisKind.ERROR,
                "join_key",
                "Each table has rows, but the join has none: check the join key.",
                "The tables of the query do not join.",
                run.queries,
            )
        if not check_filters:
            return None
        if len(conds) == 1:
            emptying = conds if int(row["no_filters"] or 0) > 0 else []
        else:
            emptying = [
                conds[i] for i in range(len(conds)) if int(row[f"drop_{i}"] or 0) > 0
            ]
        if emptying:
            return self._filter_finding(result, run, emptying, "")
        return None

    def _filter_finding(
        self, result: GuardResult, run: _Run, conds: list[exp.Expression], note: str
    ) -> EmptyDiagnosis:
        texts = [c.sql(dialect="bigquery") for c in conds]
        wrong = [
            f
            for f in result.filters
            if f.sql in texts
            and f.column_type in _DATE_TYPES
            and f.op != "year"
            and any(_date_of(v) is None for v in f.values)
        ]
        if wrong:
            return EmptyDiagnosis(
                DiagnosisKind.ERROR,
                "wrong_filter",
                f"The filter {wrong[0].sql} has a value that is not a date in the format "
                "YYYY-MM-DD.",
                "A date filter of the query has a wrong format.",
                run.queries,
            )
        named = "; ".join(texts) if texts else "the combination of the filters"
        return self._true_zero(
            result,
            run,
            "filter_empties",
            (note + " " if note else "") + f"The filter that leaves no rows: {named}.",
            list_filters=False,
        )

    def _true_zero(
        self,
        result: GuardResult,
        run: _Run,
        finding: str,
        reason: str,
        list_filters: bool = True,
    ) -> EmptyDiagnosis:
        used = [f.sql for f in result.filters if f.exact]
        filters = f" Filters: {'; '.join(used)}." if used and list_filters else ""
        note = (
            f"No data for this question. {reason}{filters} {self._range_text(result)}"
        )
        return EmptyDiagnosis(
            DiagnosisKind.TRUE_ZERO, finding, None, " ".join(note.split()), run.queries
        )

    # --- probes -----------------------------------------------------------------------------------
    def _probe(self, sql: str, run: _Run) -> pd.DataFrame | None:
        if run.queries >= MAX_PROBES or run.budget.queries_left() <= 0:
            raise _OutOfBudget
        checked = self.guard.check(sql, purpose="empty_check")
        if not checked.passed:
            return (
                None  # a probe that the guard does not accept is skipped, never forced
            )
        run.queries += 1
        try:
            result = self.bq.query(
                checked.sql,
                run.budget,
                params=checked.params,
                user_id=self.guard.user.id,
                purpose="empty_check",
            )
        except LimitExceeded as exc:
            raise _OutOfBudget from exc
        except AgentError:
            return None
        return result.dataframe
