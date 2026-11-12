"""Snapshots and refresh (spec 21 B2.7, B4): stale rules, shape check, needs_review. No network."""

from __future__ import annotations

import datetime as dt
from typing import Any

from opsfleet_agent.config import load_config
from opsfleet_agent.evals.cases import AccuracyCase
from opsfleet_agent.evals.snapshot import SnapshotStore, refresh

NOW = dt.datetime(2026, 11, 11, 12, tzinfo=dt.UTC)
SQL = "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM t WHERE (@all_brands OR p.brand IN UNNEST(@brands))"


class FakeRunner:
    def __init__(
        self, rows: list[list[Any]], times: dict[str, str] | None = None
    ) -> None:
        self.rows = rows
        self.times = times or {"order_items": "2026-11-11T03:44:00+00:00"}
        self.runs = 0
        self.dry_runs = 0

    def dry_run(self, sql: str, params: dict[str, Any]) -> int:
        self.dry_runs += 1
        return 5_000_000

    def run(self, sql: str, params: dict[str, Any]):
        self.runs += 1
        return ["brand", "revenue"], self.rows, 20_971_520

    def table_times(self) -> dict[str, str]:
        return self.times


def case(**kw: Any) -> AccuracyCase:
    base = {
        "id": "PP-01",
        "user": "maya",
        "turns": ["q"],
        "capability": "product_performance",
        "difficulty": "easy",
        "query_types": ["aggregation"],
        "reference_sql": SQL,
        "expected": {
            "mode": "rows",
            "columns": {
                "brand": {"role": "key"},
                "revenue": {"role": "value", "kind": "money"},
            },
        },
        "scoring": ["execution_match"],
        "expected_calls": 3,
    }
    base.update(kw)
    return AccuracyCase.model_validate(base)


ROWS = [["Calvin Klein", 37217.5], ["Carhartt", 25707.9]]


def test_first_refresh_stores_snapshot_with_params(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    runner = FakeRunner(ROWS)
    report = refresh([case()], users, runner, store, max_bytes=10**8, now=NOW)
    assert [i.action for i in report.items] == ["created"]
    snap = store.current("PP-01")
    assert snap.params == {"all_brands": False, "brands": ["Calvin Klein", "Carhartt"]}
    assert snap.rows == ROWS and snap.created_at.startswith("2026-11-11")
    assert report.bytes_billed == 20_971_520


def test_unchanged_tables_keep_the_snapshot(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    runner = FakeRunner(ROWS)
    refresh([case()], users, runner, store, max_bytes=10**8, now=NOW)
    report = refresh([case()], users, runner, store, max_bytes=10**8, now=NOW)
    assert report.items[0].action == "kept" and runner.runs == 1


def test_table_change_with_same_shape_refreshes(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    refresh([case()], users, FakeRunner(ROWS), store, max_bytes=10**8, now=NOW)
    newer = FakeRunner(
        [["Calvin Klein", 40000.0], ["Carhartt", 26000.0]],
        {"order_items": "2026-11-12T03:44:00+00:00"},
    )
    report = refresh([case()], users, newer, store, max_bytes=10**8, now=NOW)
    assert report.items[0].action == "refreshed"
    assert store.needs_review("PP-01") is None
    assert store.current("PP-01").rows[0][1] == 40000.0


def test_changed_row_count_sets_needs_review(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    refresh([case()], users, FakeRunner(ROWS), store, max_bytes=10**8, now=NOW)
    newer = FakeRunner(
        [["Calvin Klein", 1.0]], {"order_items": "2026-11-12T00:00:00+00:00"}
    )
    report = refresh([case()], users, newer, store, max_bytes=10**8, now=NOW)
    assert report.items[0].action == "needs_review"
    assert "row count" in store.needs_review("PP-01")


def test_empty_result_sets_needs_review(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    refresh([case()], users, FakeRunner(ROWS), store, max_bytes=10**8, now=NOW)
    newer = FakeRunner([], {"order_items": "2026-11-12T00:00:00+00:00"})
    refresh([case()], users, newer, store, max_bytes=10**8, now=NOW)
    assert store.needs_review("PP-01")


def test_relative_period_refreshes_in_a_new_month(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    relative = case(period={"relative": "last_full_month"})
    runner = FakeRunner(ROWS)
    refresh([relative], users, runner, store, max_bytes=10**8, now=NOW)
    refresh([relative], users, runner, store, max_bytes=10**8, now=NOW)
    assert runner.runs == 1
    december = dt.datetime(2026, 12, 2, tzinfo=dt.UTC)
    report = refresh([relative], users, runner, store, max_bytes=10**8, now=december)
    assert report.items[0].reason.startswith("a new month") and runner.runs == 2


def test_dry_only_runs_no_query_and_cap_blocks_large_query(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path)
    runner = FakeRunner(ROWS)
    report = refresh(
        [case()], users, runner, store, max_bytes=10**8, now=NOW, dry_only=True
    )
    assert runner.runs == 0 and report.items[0].bytes_estimated == 5_000_000
    report = refresh([case()], users, runner, store, max_bytes=1000, now=NOW)
    assert report.items[0].action == "error" and runner.runs == 0


def test_export_and_import_baseline(tmp_path):
    users = load_config().users
    store = SnapshotStore(tmp_path / "a")
    refresh([case()], users, FakeRunner(ROWS), store, max_bytes=10**8, now=NOW)
    baseline = tmp_path / "baseline.json"
    assert store.export(["PP-01"], baseline) == 1
    other = SnapshotStore(tmp_path / "b")
    assert other.import_baseline(baseline) == 1
    assert other.current("PP-01").rows == ROWS
    assert other.import_baseline(baseline) == 0  # an existing snapshot stays
