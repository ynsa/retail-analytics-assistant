"""Snapshots of the reference results and their refresh (spec 21, B2.7).

A snapshot is the result of one reference SQL for one user, with the date and the change times of
the tables. The refresh runs a reference SQL again only when the tables changed, the SQL changed,
or a relative period ("last month") moved. A new result with another shape sets `needs_review`.

Cost rules: a free dry run before each query, `maximum_bytes_billed` on each query, and only the
columns that the reference SQL selects.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from opsfleet_agent.evals.cases import Case, Expected, Reference, scope_params


@dataclass
class Snapshot:
    case_id: str
    ref: str
    user: str
    params: dict[str, Any]
    sql_sha256: str
    created_at: str
    table_modified: dict[str, str]
    bytes_billed: int
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    period_key: str | None = None  # the month of a relative period, e.g. "2026-11"

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]

    def is_empty(self) -> bool:
        if not self.rows:
            return True
        return all(v is None or v == 0 for row in self.rows for v in row)


@dataclass
class IndexEntry:
    path: str
    created_at: str
    needs_review: bool = False
    reason: str | None = None


@dataclass
class RefreshItem:
    case_id: str
    ref: str
    user: str
    action: str  # kept | refreshed | created | needs_review | error
    reason: str
    bytes_estimated: int = 0
    bytes_billed: int = 0


@dataclass
class RefreshReport:
    items: list[RefreshItem] = field(default_factory=list)

    @property
    def bytes_billed(self) -> int:
        return sum(i.bytes_billed for i in self.items)

    @property
    def bytes_estimated(self) -> int:
        return sum(i.bytes_estimated for i in self.items)


class QueryRunner(Protocol):
    def dry_run(self, sql: str, params: dict[str, Any]) -> int: ...

    def run(
        self, sql: str, params: dict[str, Any]
    ) -> tuple[list[str], list[list[Any]], int]: ...

    def table_times(self) -> dict[str, str]: ...


def sql_hash(sql: str, params: dict[str, Any]) -> str:
    text = sql.strip() + "\n" + json.dumps(params, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(value: Any) -> Any:
    """A JSON value: dates as ISO text, decimals as float, NaN as None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # numpy scalar
        return plain(value.item())
    return value


def current_period_key(now: dt.datetime) -> str:
    return now.strftime("%Y-%m")


# ------------------------------------------------------------------------------------------------
# Shape check
# ------------------------------------------------------------------------------------------------


def _key_tuples(snap: Snapshot, expected: Expected) -> set[tuple[Any, ...]]:
    keys = expected.key_columns()
    if not keys:
        return set()
    rows = snap.as_dicts()
    if expected.mode == "top_k" and expected.k:
        rows = top_k_rows(rows, expected)
    return {tuple(str(r.get(k)) for k in keys) for r in rows}


def top_k_rows(rows: list[dict[str, Any]], expected: Expected) -> list[dict[str, Any]]:
    """The reference rows in the top k, with ties at the border (rows are in rank order)."""
    k = expected.k or len(rows)
    if len(rows) <= k:
        return rows
    value_col = expected.value_columns()[0]
    border = rows[k - 1].get(value_col)
    return rows[:k] + [r for r in rows[k:] if r.get(value_col) == border]


def shape_changed(old: Snapshot, new: Snapshot, expected: Expected) -> str | None:
    """The reason of a shape change, or None. A change of a value is not a change of shape."""
    if old.columns != new.columns:
        return "columns changed"
    if old.is_empty() != new.is_empty():
        return "empty result" if new.is_empty() else "result is not empty any more"
    if expected.mode != "top_k" and old.row_count != new.row_count:
        return f"row count {old.row_count} -> {new.row_count}"
    if expected.key_columns() and _key_tuples(old, expected) != _key_tuples(
        new, expected
    ):
        return "keys changed"
    return None


# ------------------------------------------------------------------------------------------------
# Store
# ------------------------------------------------------------------------------------------------


class SnapshotStore:
    """Files: `<root>/<case_id>/<user>-<UTC date>[-<ref>].json` and `<root>/index.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def index(self) -> dict[str, IndexEntry]:
        if not self.index_path.exists():
            return {}
        raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        return {k: IndexEntry(**v) for k, v in raw.items()}

    def _write_index(self, index: dict[str, IndexEntry]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in sorted(index.items())}
        self.index_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def key(case_id: str, ref: str) -> str:
        return f"{case_id}/{ref}"

    def current(self, case_id: str, ref: str = "main") -> Snapshot | None:
        entry = self.index().get(self.key(case_id, ref))
        if entry is None:
            return None
        path = self.root / entry.path
        if not path.exists():
            return None
        return Snapshot(**json.loads(path.read_text(encoding="utf-8")))

    def needs_review(self, case_id: str) -> str | None:
        for key, entry in self.index().items():
            if key.split("/")[0] == case_id and entry.needs_review:
                return entry.reason or "needs_review"
        return None

    def save(self, snap: Snapshot, needs_review: str | None = None) -> Path:
        day = snap.created_at[:10]
        suffix = "" if snap.ref == "main" else f"-{snap.ref}"
        rel = Path(snap.case_id) / f"{snap.user}-{day}{suffix}.json"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(snap), indent=2, default=str) + "\n", encoding="utf-8"
        )
        index = self.index()
        index[self.key(snap.case_id, snap.ref)] = IndexEntry(
            path=str(rel),
            created_at=snap.created_at,
            needs_review=needs_review is not None,
            reason=needs_review,
        )
        self._write_index(index)
        return path

    def clear_review(self, case_id: str) -> None:
        index = self.index()
        for key, entry in index.items():
            if key.split("/")[0] == case_id:
                entry.needs_review = False
                entry.reason = None
        self._write_index(index)

    # --- baseline in git ------------------------------------------------------------------------
    def export(self, case_ids: list[str], path: Path) -> int:
        """Write the current snapshots of the cases to one JSON file (the reviewed baseline).

        The file keeps the snapshots of other cases, so several sets can share it."""
        out: dict[str, Any] = {}
        if path.exists():
            out = {
                k: v
                for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                if k.split("/")[0] not in case_ids
            }
        for key, entry in sorted(self.index().items()):
            if key.split("/")[0] not in case_ids:
                continue
            snap = self.current(*key.split("/"))
            if snap is not None:
                out[key] = {**asdict(snap), "needs_review": entry.needs_review}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(sorted(out.items())), indent=1, default=str) + "\n",
            encoding="utf-8",
        )
        return sum(1 for k in out if k.split("/")[0] in case_ids)

    def import_baseline(self, path: Path) -> int:
        """Copy the snapshots of a baseline file into the store, if the store has none yet."""
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        for key, raw in data.items():
            case_id, ref = key.split("/")
            if self.current(case_id, ref) is not None:
                continue
            raw = dict(raw)
            flag = raw.pop("needs_review", False)
            self.save(Snapshot(**raw), "from baseline" if flag else None)
            count += 1
        return count


# ------------------------------------------------------------------------------------------------
# Refresh
# ------------------------------------------------------------------------------------------------


def _relative(case: Case) -> bool:
    period = getattr(case, "period", None) or {}
    return "relative" in period


def stale_reason(
    snap: Snapshot | None,
    sql_sha: str,
    table_times: dict[str, str],
    relative: bool,
    now: dt.datetime,
) -> str | None:
    if snap is None:
        return "no snapshot"
    if snap.sql_sha256 != sql_sha:
        return "the SQL or the parameters changed"
    for table, modified in table_times.items():
        if modified > snap.table_modified.get(table, ""):
            return f"table {table} changed"
    if relative and snap.period_key != current_period_key(now):
        return "a new month started (relative period)"
    return None


def ref_params(case: Case, ref: Reference, users: Any) -> tuple[str, dict[str, Any]]:
    user = ref.user or case.user
    params = {**scope_params(user, users), **ref.params}
    return user, params


def refresh(
    cases: list[Case],
    users: Any,
    runner: QueryRunner,
    store: SnapshotStore,
    *,
    max_bytes: int,
    now: dt.datetime | None = None,
    force: bool = False,
    dry_only: bool = False,
    on_bytes: Any = None,
) -> RefreshReport:
    """Refresh the stale snapshots of the cases. `dry_only` makes only the free dry runs (the plan)."""
    now = now or dt.datetime.now(dt.UTC)
    times = runner.table_times()
    report = RefreshReport()
    for case in cases:
        for ref_name, ref in case.all_references().items():
            user, params = ref_params(case, ref, users)
            sha = sql_hash(ref.sql, params)
            old = store.current(case.id, ref_name)
            reason = (
                "forced"
                if force
                else stale_reason(old, sha, times, _relative(case), now)
            )
            if reason is None:
                report.items.append(
                    RefreshItem(case.id, ref_name, user, "kept", "up to date")
                )
                continue
            try:
                estimate = runner.dry_run(ref.sql, params)
            except Exception as exc:  # noqa: BLE001 - one bad SQL must not stop the refresh
                report.items.append(
                    RefreshItem(
                        case.id, ref_name, user, "error", f"dry run: {exc}"[:300]
                    )
                )
                continue
            item = RefreshItem(
                case.id, ref_name, user, "planned", reason, bytes_estimated=estimate
            )
            report.items.append(item)
            if dry_only:
                continue
            if estimate > max_bytes:
                item.action, item.reason = (
                    "error",
                    f"dry run {estimate} bytes > cap {max_bytes}",
                )
                continue
            try:
                columns, rows, billed = runner.run(ref.sql, params)
            except Exception as exc:  # noqa: BLE001
                item.action, item.reason = "error", f"query: {exc}"[:300]
                continue
            if on_bytes is not None:
                on_bytes(billed)
            item.bytes_billed = billed
            new = Snapshot(
                case_id=case.id,
                ref=ref_name,
                user=user,
                params=params,
                sql_sha256=sha,
                created_at=now.isoformat(timespec="seconds"),
                table_modified=times,
                bytes_billed=billed,
                columns=columns,
                rows=[[plain(v) for v in row] for row in rows],
                row_count=len(rows),
                period_key=current_period_key(now) if _relative(case) else None,
            )
            change = None
            if old is not None and old.sql_sha256 == sha:
                change = shape_changed(old, new, ref.expected)
            if change is None and new.is_empty() and old is None:
                change = None  # a true zero is a valid first snapshot (ER cases)
            store.save(new, change)
            item.action = (
                "needs_review"
                if change
                else ("created" if old is None else "refreshed")
            )
            if change:
                item.reason = change
    return report


# ------------------------------------------------------------------------------------------------
# BigQuery runner
# ------------------------------------------------------------------------------------------------


class BigQueryReferenceRunner:
    """Runs reference SQL on BigQuery: a free dry run first, then a capped query."""

    def __init__(
        self,
        client: Any,
        dataset: str,
        tables: list[str],
        max_bytes: int,
        location: str = "US",
    ) -> None:
        self._client = client
        self._dataset = dataset
        self._tables = tables
        self._max_bytes = max_bytes
        self._location = location

    @staticmethod
    def _params(params: dict[str, Any]) -> list[Any]:
        from google.cloud import bigquery

        out: list[Any] = []
        for name, value in params.items():
            if isinstance(value, list):
                out.append(
                    bigquery.ArrayQueryParameter(
                        name, "STRING", [str(v) for v in value]
                    )
                )
            elif isinstance(value, bool):
                out.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
            elif isinstance(value, int):
                out.append(bigquery.ScalarQueryParameter(name, "INT64", value))
            elif isinstance(value, float):
                out.append(bigquery.ScalarQueryParameter(name, "FLOAT64", value))
            else:
                out.append(bigquery.ScalarQueryParameter(name, "STRING", str(value)))
        return out

    def _config(self, params: dict[str, Any], dry: bool) -> Any:
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(
            dry_run=dry,
            use_query_cache=not dry,
            maximum_bytes_billed=self._max_bytes,
            query_parameters=self._params(params),
            labels={"app": "opsfleet-agent", "purpose": "eval_snapshot"},
        )

    def dry_run(self, sql: str, params: dict[str, Any]) -> int:
        job = self._client.query(
            sql, job_config=self._config(params, dry=True), location=self._location
        )
        return int(job.total_bytes_processed or 0)

    def run(
        self, sql: str, params: dict[str, Any]
    ) -> tuple[list[str], list[list[Any]], int]:
        job = self._client.query(
            sql, job_config=self._config(params, dry=False), location=self._location
        )
        result = job.result(timeout=120)
        columns = [f.name for f in result.schema]
        rows = [list(row.values()) for row in result]
        return columns, rows, int(job.total_bytes_billed or 0)

    def table_times(self) -> dict[str, str]:
        """The last change time of each table. Reading table metadata is free."""
        times: dict[str, str] = {}
        for table in self._tables:
            meta = self._client.get_table(f"{self._dataset}.{table}")
            times[table] = meta.modified.isoformat() if meta.modified else ""
        return times
