"""Statistics of the turns (spec 35, B2.4).

`compute_stats()` reads the metric columns of `conversation_turns` for a period, for one user or
for all users. The percentiles use the nearest-rank method: the value at rank ceil(p/100 * n) of
the sorted values. A p95 needs at least 20 values. Turns written before spec 35 have no figures
(`outcome` is NULL), so they do not count.

Definitions:

- Time: `duration_ms` of the turn span, for questions and for reports separately.
- Answer length: `answer_words` of the turns that ended `answered` or `true_zero`.
- Tokens: for each turn. Model calls: the average of the logical calls for each turn.
- Self-correction: turns with a query step that needed more than 1 attempt. Success: the share of
  them that ended `answered` or `true_zero`.
- Errors resolved: errors in turns that ended `answered` or `true_zero` (a retry or a corrected
  query fixed them).
- Confirmations: `shown` counts each delete list. Cancelled counts by reason.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

MIN_P95 = 20
GOOD = frozenset({"answered", "true_zero"})
EXPORT_COLUMNS = (
    "started_at",
    "user_id",
    "conversation_id",
    "turn_no",
    "trace_id",
    "kind",
    "intent",
    "outcome",
    "status",
    "error_type",
    "duration_ms",
    "answer_words",
    "answer_chars",
    "llm_calls",
    "llm_attempts",
    "tokens_in",
    "tokens_out",
    "bq_queries",
    "bq_bytes_billed",
    "attempts_total",
    "retried",
    "errors",
    "guard_block_reason",
    "empty_result_kind",
    "confirmation_outcome",
    "tools_used",
    "model",
    "fallback_used",
    "fallback_calls",
    "retries",
    "circuit_open",
    "masker_hits",
    "scanner_hits",
    "prompt_version",
)
_SELECT = ", ".join((*EXPORT_COLUMNS, "bq_query_bytes"))


def nearest_rank(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Spread:
    """The median and the p95 (nearest rank) of a list of values."""

    n: int
    median: float | None
    p95: float | None  # None with fewer than 20 values

    @classmethod
    def of(cls, values: Iterable[float]) -> Spread:
        items = [float(v) for v in values]
        return cls(
            n=len(items),
            median=nearest_rank(items, 50),
            p95=nearest_rank(items, 95) if len(items) >= MIN_P95 else None,
        )

    def text(self, unit: str = "", scale: float = 1.0, digits: int = 1) -> str:
        if self.median is None:
            return "no data"

        def fmt(value: float) -> str:
            return f"{value / scale:,.{digits}f}{unit}"

        p95 = (
            f"p95 {fmt(self.p95)} (nearest rank, n = {self.n})"
            if self.p95 is not None
            else f"p95: not enough data (n = {self.n})"
        )
        return f"median {fmt(self.median)} · {p95}"


@dataclass(frozen=True)
class Stats:
    scope: str  # the user ID, or "all users"
    days: int
    since: str
    turns: int
    by_kind: dict[str, int] = field(default_factory=dict)
    by_outcome: dict[str, int] = field(default_factory=dict)
    time_questions: Spread = Spread(0, None, None)
    time_reports: Spread = Spread(0, None, None)
    answer_words: Spread = Spread(0, None, None)
    tokens_in: Spread = Spread(0, None, None)
    tokens_out: Spread = Spread(0, None, None)
    tokens_in_total: int = 0
    tokens_out_total: int = 0
    llm_calls_total: int = 0
    avg_llm_calls: float = 0.0
    routes: dict[str, int] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    final_errors: dict[str, int] = field(default_factory=dict)
    all_errors: dict[str, int] = field(default_factory=dict)
    resolved_errors: dict[str, int] = field(default_factory=dict)
    retried_turns: int = 0
    retried_good: int = 0
    avg_attempts: float = 0.0
    guard_blocks: dict[str, int] = field(default_factory=dict)
    masker_hits: int = 0
    scanner_hits: int = 0
    empty_results: dict[str, int] = field(default_factory=dict)
    confirmations_shown: int = 0
    confirmations_confirmed: int = 0
    confirmations_cancelled: dict[str, int] = field(default_factory=dict)
    bq_queries_total: int = 0
    bytes_total: int = 0
    query_bytes: Spread = Spread(0, None, None)
    top_bytes: tuple[tuple[str, str, int], ...] = ()  # (trace 8 chars, start, bytes)
    retries: int = 0
    fallback_calls: int = 0
    fallback_turns: int = 0
    circuit_opens: int = 0

    @property
    def retried_success(self) -> float | None:
        return self.retried_good / self.retried_turns if self.retried_turns else None

    def groups(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """The figures as text rows in the groups of B2.4. The CLI and the UI show these rows."""
        mb = 1e6
        success = self.retried_success
        return [
            (
                "Volume",
                [
                    ("Turns", f"{self.turns:,}"),
                    ("By kind", _counts(self.by_kind)),
                    ("By status", _counts(self.by_outcome)),
                ],
            ),
            (
                "Time",
                [
                    ("Questions", self.time_questions.text(" s", 1000)),
                    ("Reports", self.time_reports.text(" s", 1000)),
                ],
            ),
            ("Answer length", [("Words", self.answer_words.text("", digits=0))]),
            (
                "Tokens",
                [
                    ("In, each turn", self.tokens_in.text("", digits=0)),
                    ("Out, each turn", self.tokens_out.text("", digits=0)),
                    (
                        "Total",
                        f"{self.tokens_in_total:,} in · {self.tokens_out_total:,} out",
                    ),
                    (
                        "Model calls",
                        f"{self.llm_calls_total:,} · {self.avg_llm_calls:.1f} each turn",
                    ),
                ],
            ),
            (
                "Routes and tools",
                [
                    ("Routes", _counts(self.routes)),
                    ("Nodes and tools", _counts(self.tools)),
                ],
            ),
            (
                "Failures",
                [
                    ("Turns by error type", _counts(self.final_errors)),
                    ("All errors by type", _counts(self.all_errors)),
                    ("Resolved by retries", _counts(self.resolved_errors)),
                ],
            ),
            (
                "Self-correction",
                [
                    ("Turns with a retry", f"{self.retried_turns:,}"),
                    (
                        "Success",
                        "no data" if success is None else f"{success:.0%}",
                    ),
                    ("Average attempts", f"{self.avg_attempts:.1f}"),
                ],
            ),
            (
                "Guard",
                [
                    ("Policy blocks", _counts(self.guard_blocks)),
                    ("Masker hits", f"{self.masker_hits:,}"),
                    ("Scanner hits", f"{self.scanner_hits:,}"),
                ],
            ),
            ("Empty results", [("By kind", _counts(self.empty_results))]),
            (
                "Confirmations",
                [
                    ("Shown", f"{self.confirmations_shown:,}"),
                    ("Confirmed", f"{self.confirmations_confirmed:,}"),
                    ("Cancelled", _counts(self.confirmations_cancelled)),
                ],
            ),
            (
                "BigQuery",
                [
                    ("Queries", f"{self.bq_queries_total:,}"),
                    ("Bytes billed", f"{self.bytes_total / mb:,.1f} MB"),
                    ("Each query", self.query_bytes.text(" MB", mb)),
                    (
                        "Top turns",
                        ", ".join(
                            f"{trace} {value / mb:,.1f} MB"
                            for trace, _, value in self.top_bytes
                        )
                        or "none",
                    ),
                ],
            ),
            (
                "Resilience",
                [
                    ("Retries", f"{self.retries:,}"),
                    (
                        "Fallback model",
                        f"{self.fallback_calls:,} calls in {self.fallback_turns:,} turns",
                    ),
                    ("Circuit opens", f"{self.circuit_opens:,}"),
                ],
            ),
        ]


def _counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{name} {count:,}" for name, count in ordered)


def load_rows(
    conn: sqlite3.Connection, since: datetime, user_id: str | None
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_SELECT} FROM conversation_turns "
        "WHERE started_at >= ? AND outcome IS NOT NULL"
    )
    params: list[Any] = [since.isoformat()]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    rows = conn.execute(sql + " ORDER BY started_at, turn_no", params).fetchall()
    return [dict(r) for r in rows]


def compute_stats(
    conn: sqlite3.Connection, now: datetime, days: int, user_id: str | None
) -> Stats:
    """The statistics of the last `days` days. `user_id = None` gives all users."""
    since = now - timedelta(days=days)
    rows = load_rows(conn, since, user_id)
    return stats_of(rows, user_id or "all users", days, since)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def stats_of(
    rows: list[dict[str, Any]], scope: str, days: int, since: datetime
) -> Stats:
    def where(**match: Any) -> list[dict[str, Any]]:
        return [r for r in rows if all(r.get(k) == v for k, v in match.items())]

    good = [r for r in rows if r["outcome"] in GOOD]
    all_errors: Counter[str] = Counter()
    resolved: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    query_bytes: list[int] = []
    for r in rows:
        errors = _json(r.get("errors"), {})
        all_errors.update(errors)
        if r["outcome"] in GOOD:
            resolved.update(errors)
        tools.update(_json(r.get("tools_used"), {}))
        query_bytes += [int(b) for b in _json(r.get("bq_query_bytes"), [])]
    retried = [r for r in rows if r.get("retried")]
    confirm = Counter(
        r["confirmation_outcome"] for r in rows if r.get("confirmation_outcome")
    )
    top = sorted(
        (r for r in rows if r["bq_bytes_billed"]),
        key=lambda r: -int(r["bq_bytes_billed"]),
    )[:5]
    n = len(rows)
    return Stats(
        scope=scope,
        days=days,
        since=since.date().isoformat(),
        turns=n,
        by_kind=dict(Counter(r["kind"] for r in rows if r.get("kind"))),
        by_outcome=dict(Counter(r["outcome"] for r in rows)),
        time_questions=Spread.of(r["duration_ms"] for r in where(kind="question")),
        time_reports=Spread.of(r["duration_ms"] for r in where(kind="report")),
        answer_words=Spread.of(r["answer_words"] for r in good),
        tokens_in=Spread.of(r["tokens_in"] for r in rows),
        tokens_out=Spread.of(r["tokens_out"] for r in rows),
        tokens_in_total=sum(int(r["tokens_in"]) for r in rows),
        tokens_out_total=sum(int(r["tokens_out"]) for r in rows),
        llm_calls_total=sum(int(r["llm_calls"]) for r in rows),
        avg_llm_calls=sum(int(r["llm_calls"]) for r in rows) / n if n else 0.0,
        routes=dict(Counter(r["intent"] for r in rows if r.get("intent"))),
        tools=dict(tools),
        final_errors=dict(
            Counter(r["error_type"] for r in rows if r.get("error_type"))
        ),
        all_errors=dict(all_errors),
        resolved_errors=dict(resolved),
        retried_turns=len(retried),
        retried_good=sum(1 for r in retried if r["outcome"] in GOOD),
        avg_attempts=(
            sum(int(r["attempts_total"]) for r in retried) / len(retried)
            if retried
            else 0.0
        ),
        guard_blocks=dict(
            Counter(
                r["guard_block_reason"] for r in rows if r.get("guard_block_reason")
            )
        ),
        masker_hits=sum(int(r["masker_hits"]) for r in rows),
        scanner_hits=sum(int(r["scanner_hits"]) for r in rows),
        empty_results=dict(
            Counter(r["empty_result_kind"] for r in rows if r.get("empty_result_kind"))
        ),
        confirmations_shown=sum(confirm.values()),
        confirmations_confirmed=confirm.get("confirmed", 0),
        confirmations_cancelled={
            k: v for k, v in confirm.items() if k not in ("shown", "confirmed")
        },
        bq_queries_total=sum(int(r["bq_queries"]) for r in rows),
        bytes_total=sum(int(r["bq_bytes_billed"]) for r in rows),
        query_bytes=Spread.of(query_bytes),
        top_bytes=tuple(
            (
                str(r["trace_id"] or "")[:8],
                str(r["started_at"]),
                int(r["bq_bytes_billed"]),
            )
            for r in top
        ),
        retries=sum(int(r["retries"]) for r in rows),
        fallback_calls=sum(int(r["fallback_calls"]) for r in rows),
        fallback_turns=sum(1 for r in rows if r.get("fallback_used")),
        circuit_opens=sum(int(r["circuit_open"]) for r in rows),
    )


# --- export ---------------------------------------------------------------------------------------
def export(
    conn: sqlite3.Connection,
    now: datetime,
    days: int,
    user_id: str | None,
    fmt: str,
    out_dir: Path,
) -> tuple[Path, int]:
    """One row for each turn, figures only: no question and no answer text (A2.5)."""
    rows = load_rows(conn, now - timedelta(days=days), user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"stats-{now:%Y-%m-%d}.{fmt}"
    records = [{k: r[k] for k in EXPORT_COLUMNS} for r in rows]
    if fmt == "json":
        for record in records:
            for key in ("errors", "tools_used"):
                record[key] = _json(record[key], {})
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    elif fmt == "csv":
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(records)
    else:
        raise ValueError(f"unknown export format: {fmt}")
    return path, len(records)


def stats_text(stats: Stats) -> str:
    """The statistics as plain text (for the UI, the logs and a terminal without Rich)."""
    lines = [stats_title(stats)]
    for group, rows in stats.groups():
        lines.append(f"{group}:")
        lines += [f"  {label}: {value}" for label, value in rows]
    return "\n".join(lines)


def stats_title(stats: Stats) -> str:
    who = "all users" if stats.scope == "all users" else f"user {stats.scope}"
    return (
        f"Statistics of {who}, last {stats.days} {'day' if stats.days == 1 else 'days'} "
        f"(since {stats.since}): "
        f"{stats.turns:,} turns"
    )
