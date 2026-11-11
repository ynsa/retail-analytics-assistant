"""Value cache and the brand check of users.yaml (spec 30, B2.8 and B4 cases 77-79)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from opsfleet_agent.config import UsersConfig
from opsfleet_agent.guard.value_cache import (
    ValueCache,
    build_query,
    load_value_cache,
    validate_brands,
)
from tests.conftest import DATASET, FakeBQ, frame_result


def users(*entries: dict) -> UsersConfig:
    return UsersConfig.model_validate({"users": list(entries)})


CEO = {"id": "ceo", "name": "CEO", "role": "ceo"}


def test_case_77_brand_without_apostrophe_names_the_real_brand(test_values):
    problems = validate_brands(
        users(CEO, {"id": "alex", "name": "A", "role": "manager", "brands": ["Levis"]}),
        test_values,
    )
    assert [p.message() for p in problems] == [
        "Brand 'Levis' for user 'alex' does not exist in the data. Close names: Levi's."
    ]


def test_case_78_typographic_apostrophe_is_a_different_brand(test_values):
    [problem] = validate_brands(
        users({"id": "alex", "name": "A", "role": "manager", "brands": ["Levi’s"]}),
        test_values,
    )
    assert problem.close_names[0] == "Levi's"


def test_case_79_problems_name_their_user(test_values):
    config = users(
        {"id": "maya", "name": "M", "role": "manager", "brands": ["Calvin Klein"]},
        {"id": "alex", "name": "A", "role": "manager", "brands": ["levi's"]},
    )
    problems = validate_brands(config, test_values)
    assert [(p.user_id, p.brand) for p in problems] == [("alex", "levi's")]


def test_exact_names_pass_and_close_variants_are_different_brands(test_values):
    ok = users(
        {
            "id": "m",
            "name": "M",
            "role": "manager",
            "brands": ["Calvin Klein", "O'Neill"],
        }
    )
    assert validate_brands(ok, test_values) == []
    bad = users(
        {"id": "m", "name": "M", "role": "manager", "brands": ["Calvin Klein Jean"]}
    )
    [problem] = validate_brands(bad, test_values)
    assert problem.close_names[0] == "Calvin Klein Jeans"


def _cache_frame() -> pd.DataFrame:
    rows = {
        "orders": {
            "status": ["Complete", "Cancelled"],
            "gender": ["M", "F"],
            "min_created_at": "2019-01-06",
        },
        "order_items": {"status": ["Complete"], "min_created_at": "2019-01-06"},
        "products": {
            "department": ["Men"],
            "category": ["Jeans"],
            "brand": ["Levi's", "Carhartt"],
        },
        "users": {
            "gender": ["F"],
            "traffic_source": ["Search"],
            "country": ["Brasil"],
            "min_created_at": "2019-01-01",
        },
    }
    return pd.DataFrame(
        {"tbl": list(rows), "data": [json.dumps(v) for v in rows.values()]}
    )


def test_build_query_reads_only_the_small_columns():
    sql = build_query(DATASET)
    assert sql.count("UNION ALL") == 3
    for column in (
        "email",
        "first_name",
        "street_address",
        "latitude",
        "num_of_item",
        "name",
    ):
        assert f"({column} " not in sql and f" {column})" not in sql
    assert "MIN(created_at)" in sql


def test_load_builds_once_then_reads_the_file(tmp_path, budget):
    path = tmp_path / "value_cache.json"
    bq = FakeBQ([frame_result(_cache_frame())])
    cache = load_value_cache(path, bq, budget, DATASET, today=date(2026, 11, 11))
    assert cache.brands == ("Carhartt", "Levi's")
    assert cache.date_min == {
        "orders": "2019-01-06",
        "order_items": "2019-01-06",
        "users": "2019-01-01",
    }
    assert bq.purposes == ["value_cache"]
    again = load_value_cache(
        path, FakeBQ([]), budget, DATASET, today=date(2026, 11, 12)
    )
    assert again == cache
    assert "Levi's" in path.read_text(encoding="utf-8")


def test_cache_is_built_again_after_7_days_or_on_request(tmp_path, budget):
    path = tmp_path / "value_cache.json"
    load_value_cache(
        path,
        FakeBQ([frame_result(_cache_frame())]),
        budget,
        DATASET,
        today=date(2026, 11, 1),
    )
    stale = FakeBQ([frame_result(_cache_frame())])
    load_value_cache(path, stale, budget, DATASET, today=date(2026, 11, 8))
    assert len(stale.queries) == 1
    forced = FakeBQ([frame_result(_cache_frame())])
    load_value_cache(
        path, forced, budget, DATASET, refresh=True, today=date(2026, 11, 8)
    )
    assert len(forced.queries) == 1


def test_round_trip_keeps_values(test_values):
    assert ValueCache.from_json(test_values.to_json()) == test_values
    assert test_values.first_date("orders") == date(2019, 1, 6)
    assert test_values.first_date() == date(2019, 1, 1)
