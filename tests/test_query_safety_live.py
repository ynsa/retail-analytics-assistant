"""Live tests of spec 30 (B4): 0 Gemini calls. About 5 queries and 200 MB billed in total, plus one
query of about 40 MB when the value cache is missing.

Run by hand: uv run pytest -m live tests/test_query_safety_live.py
"""

from __future__ import annotations

import pytest

from opsfleet_agent.config import load_config
from opsfleet_agent.guard.empty_result import DiagnosisKind, EmptyResultChecker
from opsfleet_agent.guard.sql_guard import SqlGuard
from opsfleet_agent.guard.value_cache import validate_brands
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.session import build_services
from opsfleet_agent.store.db import connect

pytestmark = pytest.mark.live

REVENUE_BY_BRAND = (
    "SELECT p.brand, SUM(oi.sale_price) AS revenue "
    "FROM `bigquery-public-data.thelook_ecommerce.order_items` oi "
    "JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id "
    "WHERE oi.created_at >= TIMESTAMP('2025-10-01') AND oi.created_at < TIMESTAMP('2025-11-01') "
    "GROUP BY p.brand"
)


@pytest.fixture(scope="module")
def live():
    config = load_config()
    services = build_services(config)
    conn = connect(config.settings.data_path("app.db"))
    ledger = DailyLedger(conn, config.settings.limits)
    yield config, services, ledger
    conn.close()


def _budget(config, ledger) -> TurnBudget:
    return TurnBudget(config.settings.limits, ledger)


def test_brand_scope_manager_sees_only_own_brands(live):
    config, services, ledger = live
    brands = {}
    for user_id in ("maya", "ceo"):
        user = resolve_user(config.users, user_id)
        checked = SqlGuard(user, services.schema, services.values).check(
            REVENUE_BY_BRAND
        )
        assert checked.passed
        result = services.bq.query(
            checked.sql,
            _budget(config, ledger),
            params=checked.params,
            user_id=user_id,
            purpose="live_test",
        )
        brands[user_id] = set(result.dataframe["brand"])
    maya = resolve_user(config.users, "maya")
    assert brands["maya"] and brands["maya"] <= set(maya.brands)
    assert len(brands["ceo"]) > len(brands["maya"])


def test_brands_of_users_yaml_exist_in_the_data(live):
    config, services, _ = live
    assert validate_brands(config.users, services.values) == []
    assert "Levi's" in services.values.brands


def test_empty_result_with_a_real_probe(live):
    config, services, ledger = live
    maya = resolve_user(config.users, "maya")
    guard = SqlGuard(maya, services.schema, services.values)
    checked = guard.check(
        "SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.order_items` oi "
        "WHERE oi.sale_price > 100000"
    )
    checker = EmptyResultChecker(guard, services.bq, services.values)
    diagnosis = checker.diagnose(checked, _budget(config, ledger))
    assert diagnosis.kind is DiagnosisKind.TRUE_ZERO
    assert diagnosis.finding == "filter_empties" and 1 <= diagnosis.queries_run <= 2
    assert "sale_price > 100000" in diagnosis.user_note
