"""The accuracy set (spec 21 A5, B4): fields, mix, scope rule, PII rule, snapshot sanity checks."""

from __future__ import annotations

import json
from collections import Counter

import pytest
import sqlglot
from sqlglot import exp

from opsfleet_agent.config import PROJECT_ROOT
from opsfleet_agent.evals.cases import AccuracyCase, load_set

PII = {
    "first_name",
    "last_name",
    "email",
    "street_address",
    "postal_code",
    "latitude",
    "longitude",
    "user_geom",
}
BRAND_NAMES = ("Calvin Klein", "Carhartt", "Levi")
BASELINE = PROJECT_ROOT / "evals" / "baseline" / "snapshots.json"
ACCURACY = load_set("accuracy")
CASES: list[AccuracyCase] = ACCURACY.cases  # type: ignore[assignment]


def all_sql() -> list[tuple[str, str]]:
    out = []
    for name in ("accuracy", "owner_session", "safety_long"):
        for case in load_set(name).cases:
            for ref_name, ref in case.all_references().items():
                out.append((f"{case.id}/{ref_name}", ref.sql))
    return out


def test_fifty_cases_with_all_fields():
    assert len(CASES) == 50
    for case in CASES:
        assert case.status == "ready", case.id
        assert case.reviewed_by and case.reviewed_at, case.id
        assert case.expected_calls > 0


def test_mix_of_the_cases():
    counts = Counter(c.id.split("-")[0] for c in CASES)
    assert counts == {"CB": 8, "PP": 10, "TM": 10, "SC": 5, "MS": 8, "RP": 5, "ER": 4}
    assert Counter(c.difficulty for c in CASES) == {
        "easy": 13,
        "medium": 23,
        "hard": 14,
    }
    users = Counter(c.user for c in CASES)
    assert users["ceo"] == 26 and users["maya"] + users["alex"] == 24


@pytest.mark.parametrize(("key", "sql"), all_sql())
def test_scope_rule_and_no_brand_names(key: str, sql: str):
    assert "@all_brands" in sql and "UNNEST(@brands)" in sql, key
    for name in BRAND_NAMES:
        assert name not in sql, f"{key}: brand name in the SQL text"


@pytest.mark.parametrize(("key", "sql"), all_sql())
def test_no_pii_column(key: str, sql: str):
    tree = sqlglot.parse_one(sql, read="bigquery")
    used = {c.name.lower() for c in tree.find_all(exp.Column)}
    assert not used & PII, key
    assert "SELECT *" not in sql.upper().replace("COUNT(*)", "")


def test_cases_without_sql_have_required_words():
    no_sql = [c.id for c in CASES if c.reference_sql is None]
    assert no_sql == ["SC-01", "SC-02", "SC-04", "SC-05"]
    for case in CASES:
        if case.reference_sql is None:
            assert case.required


# --- sanity checks on the reviewed baseline snapshot (spec 21, B4) ----------------------------


@pytest.fixture(scope="module")
def snap() -> dict:
    return json.loads(BASELINE.read_text())


def col(snap: dict, key: str, column: str) -> list:
    s = snap[key]
    i = s["columns"].index(column)
    return [r[i] for r in s["rows"]]


def test_baseline_has_every_reference(snap):
    expected = {key for key, _ in all_sql()}
    assert expected <= set(snap)
    assert not [k for k, v in snap.items() if v.get("needs_review")]


def test_monthly_sum_equals_year_total(snap):
    assert sum(col(snap, "TM-02/main", "revenue")) == pytest.approx(
        col(snap, "TM-01/main", "revenue")[0], abs=0.01
    )


def test_manager_values_not_above_ceo(snap):
    total_2025 = col(snap, "TM-01/main", "revenue")[0]
    assert sum(col(snap, "PP-01/main", "revenue")) <= total_2025
    assert (
        col(snap, "CB-08/main", "customers")[0]
        <= col(snap, "CB-01/main", "customers")[0]
    )
    ceo_brands = dict(
        zip(col(snap, "PP-02/main", "brand"), col(snap, "PP-02/main", "revenue"))
    )
    for brand, value in zip(
        col(snap, "PP-01/main", "brand"), col(snap, "PP-01/main", "revenue")
    ):
        assert value <= ceo_brands[brand] + 0.01


def test_pp01_equals_pp03(snap):
    pp01 = dict(
        zip(col(snap, "PP-01/main", "brand"), col(snap, "PP-01/main", "revenue"))
    )
    pp03 = dict(
        zip(col(snap, "PP-03/main", "brand"), col(snap, "PP-03/main", "revenue"))
    )
    assert pp01 == pytest.approx(pp03)


def test_top_k_snapshots_have_k_rows(snap):
    for case in CASES:
        if case.expected and case.expected.mode == "top_k":
            assert snap[f"{case.id}/main"]["row_count"] >= case.expected.k, case.id


def test_true_zero_cases_are_empty(snap):
    assert col(snap, "ER-01/main", "items") == [0]
    assert col(snap, "ER-04/main", "orders") == [0]
