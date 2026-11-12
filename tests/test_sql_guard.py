"""The SQL guard (spec 30, B2.2 and B4 cases 1-62), with a test schema and a test value cache.

"M" is a manager with the brands `Calvin Klein` and `Levi's`. "C" is the CEO. In BigQuery, `'Levi''s'`
is not a valid string, so the tests write `"Levi's"` or `'Levi\\'s'` (see the spec 30 notes).
"""

from __future__ import annotations

import json

import pytest
import sqlglot
from sqlglot import exp

from opsfleet_agent.guard.scope import is_base_table, verify_scope
from opsfleet_agent.guard.sql_guard import GuardOutcome, GuardResult, SqlGuard
from opsfleet_agent.tracing import init_tracing
from tests.conftest import CEO, MANAGER, T, load_test_schema, load_test_values

PASS, POLICY, PARSE = (
    GuardOutcome.PASS,
    GuardOutcome.POLICY_BLOCK,
    GuardOutcome.PARSE_ERROR,
)


@pytest.fixture(scope="module")
def guards() -> dict[str, SqlGuard]:
    schema, values = load_test_schema(), load_test_values()
    return {"M": SqlGuard(MANAGER, schema, values), "C": SqlGuard(CEO, schema, values)}


def run(guards: dict[str, SqlGuard], role: str, sql: str) -> GuardResult:
    return guards[role].check(sql)


REVENUE_BY_BRAND = (
    f"SELECT p.brand, SUM(oi.sale_price) AS revenue FROM {T('order_items')} oi "
    f"JOIN {T('products')} p ON p.id = oi.product_id GROUP BY p.brand"
)
BRAND_FILTER = (
    f"SELECT SUM(oi.sale_price) AS revenue FROM {T('order_items')} oi "
    f"JOIN {T('products')} p ON p.id = oi.product_id WHERE "
)

CASES: list[tuple[int, str, str, GuardOutcome, str | None]] = [
    # Read-only and statements
    (1, "M", f"SELECT COUNT(*) FROM {T('orders')}", PASS, None),
    (2, "C", f"DROP TABLE {T('users')}", POLICY, "not_select"),
    (3, "C", f"DELETE FROM {T('orders')} WHERE TRUE", POLICY, "not_select"),
    (
        4,
        "C",
        f"INSERT INTO {T('orders')} SELECT * FROM {T('orders')}",
        POLICY,
        "not_select",
    ),
    (
        5,
        "C",
        (
            f"MERGE {T('orders')} t USING {T('orders')} s ON t.order_id = s.order_id "
            "WHEN MATCHED THEN DELETE"
        ),
        POLICY,
        "not_select",
    ),
    (6, "C", "CREATE TEMP TABLE x AS SELECT 1", POLICY, "not_select"),
    (7, "C", f"SELECT 1; DROP TABLE {T('users')}", POLICY, "not_select"),
    (8, "C", "SELECT 1; SELECT 2", PARSE, "multi_statement"),
    (
        9,
        "C",
        "EXECUTE IMMEDIATE CONCAT('SELECT email FROM ', 'users')",
        POLICY,
        "not_select",
    ),
    (10, "C", "DECLARE t STRING DEFAULT 'users'; SELECT 1", POLICY, "not_select"),
    (
        11,
        "C",
        f"EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT order_id FROM {T('orders')}",
        POLICY,
        "not_select",
    ),
    (12, "C", f"SELEC COUNT(*) FROM {T('orders')}", PARSE, "syntax"),
    # Tables
    (13, "M", "SELECT COUNT(*) FROM orders", PASS, None),
    (
        14,
        "M",
        "SELECT COUNT(*) FROM `bigquery-public-data`.thelook_ecommerce.ORDERS",
        PASS,
        None,
    ),
    (15, "C", f"SELECT * FROM {T('events')}", POLICY, "table_not_allowed"),
    (
        16,
        "C",
        "SELECT COUNT(*) FROM thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS",
        POLICY,
        "table_not_allowed",
    ),
    (
        17,
        "C",
        "SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.*`",
        POLICY,
        "table_not_allowed",
    ),
    (
        18,
        "C",
        "SELECT COUNT(*) FROM other_project.ds.orders",
        POLICY,
        "table_not_allowed",
    ),
    (
        19,
        "C",
        "SELECT COUNT(*) FROM EXTERNAL_QUERY('x', 'SELECT 1')",
        POLICY,
        "forbidden_construct",
    ),
    (20, "C", "SELECT SESSION_USER()", POLICY, "forbidden_construct"),
    (21, "C", "SELECT @@project_id", POLICY, "forbidden_construct"),
    (
        22,
        "C",
        f"SELECT COUNT(*) FROM {T('orders')} WHERE status = @s",
        POLICY,
        "forbidden_construct",
    ),
    (
        23,
        "C",
        f"SELECT COUNT(*) FROM {T('orders')} FOR SYSTEM_TIME AS OF '2024-01-01'",
        POLICY,
        "forbidden_construct",
    ),
    # Brand scope
    (24, "M", REVENUE_BY_BRAND, PASS, None),
    (25, "C", REVENUE_BY_BRAND, PASS, None),
    (26, "M", BRAND_FILTER + "p.brand = 'Nike'", POLICY, "brand_out_of_scope"),
    (
        27,
        "M",
        BRAND_FILTER + "p.brand IN (\"Levi's\", 'Calvin Klein Jeans')",
        POLICY,
        "brand_out_of_scope",
    ),
    (28, "M", BRAND_FILTER + "p.brand = 'levi\\'s'", PARSE, "brand_name_case"),
    (29, "M", BRAND_FILTER + "p.brand = 'Levi\\'s'", PASS, None),
    (
        30,
        "M",
        (
            f"SELECT COUNT(*) AS n FROM {T('order_items')} oi WHERE EXISTS (SELECT 1 FROM "
            f"{T('products')} p2 WHERE p2.id = oi.product_id AND p2.brand = 'Calvin Klein')"
        ),
        PASS,
        None,
    ),
    (
        31,
        "M",
        f"WITH x AS (SELECT * FROM {T('products')}) SELECT brand FROM x",
        PARSE,
        "star_select",
    ),
    (
        32,
        "M",
        f"WITH x AS (SELECT id, brand FROM {T('products')}) SELECT brand FROM x",
        PASS,
        None,
    ),
    (
        33,
        "M",
        "WITH products AS (SELECT 1 AS id) SELECT id FROM products",
        PARSE,
        "cte_shadows_table",
    ),
    (
        34,
        "M",
        f"SELECT SUM(num_of_item) FROM {T('orders')}",
        PARSE,
        "column_not_in_scope",
    ),
    (35, "C", f"SELECT SUM(num_of_item) FROM {T('orders')}", PASS, None),
    (
        36,
        "M",
        (
            f"SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, COUNT(*) AS signups "
            f"FROM {T('users')} GROUP BY month"
        ),
        PASS,
        None,
    ),
    (
        37,
        "M",
        (
            f"SELECT brand FROM {T('products')} WHERE category = 'Jeans' "
            f"UNION ALL SELECT brand FROM {T('products')} WHERE category = 'Tops & Tees'"
        ),
        PASS,
        None,
    ),
    (
        38,
        "M",
        f"SELECT COUNT(*) FROM UNNEST(ARRAY(SELECT brand FROM {T('products')}))",
        PARSE,
        "unnest_subquery",
    ),
    (
        39,
        "M",
        (
            "SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-01-01','2024-03-01', "
            "INTERVAL 1 MONTH)) d"
        ),
        PASS,
        None,
    ),
    # PII and demographic fields
    (42, "C", f"SELECT email FROM {T('users')}", POLICY, "pii_column"),
    (43, "C", f"SELECT u.email AS e FROM {T('users')} u", POLICY, "pii_column"),
    (44, "C", f"SELECT `EMAIL` FROM {T('users')}", POLICY, "pii_column"),
    (
        45,
        "C",
        f"SELECT COUNT(*) FROM {T('users')} WHERE email LIKE '%gmail%'",
        POLICY,
        "pii_column",
    ),
    (
        46,
        "C",
        f"SELECT COUNT(*) FROM {T('users')} u JOIN {T('orders')} o ON u.email = CAST(o.user_id AS STRING)",
        POLICY,
        "pii_column",
    ),
    (47, "C", f"SELECT * FROM {T('users')}", PARSE, "star_select"),
    (48, "C", f"SELECT u.* FROM {T('users')} u", PARSE, "star_select"),
    (49, "C", f"SELECT TO_JSON_STRING(u) FROM {T('users')} u", POLICY, "row_value"),
    (
        50,
        "C",
        f"SELECT JSON_VALUE(TO_JSON_STRING(u), '$.email') FROM {T('users')} u",
        POLICY,
        "pii_column",
    ),
    (51, "C", f"SELECT ST_ASTEXT(user_geom) FROM {T('users')}", POLICY, "pii_column"),
    (
        52,
        "C",
        f"SELECT ROUND(latitude, 1) AS lat FROM {T('users')}",
        POLICY,
        "pii_column",
    ),
    (
        53,
        "C",
        f"SELECT first_name FROM (SELECT * FROM {T('users')})",
        PARSE,
        "star_select",
    ),
    (
        54,
        "M",
        f"SELECT user_id, SUM(sale_price) FROM {T('order_items')} GROUP BY user_id",
        PASS,
        None,
    ),
    (
        55,
        "C",
        f"SELECT u.id, u.state FROM {T('users')} u",
        POLICY,
        "demographic_pairing",
    ),
    (
        56,
        "C",
        (
            f"SELECT oi.user_id, SUM(sale_price) FROM {T('order_items')} oi JOIN {T('users')} u "
            "ON u.id = oi.user_id WHERE u.state = 'Texas' GROUP BY 1"
        ),
        POLICY,
        "demographic_pairing",
    ),
    (
        57,
        "C",
        f"SELECT state, COUNT(DISTINCT u.id) FROM {T('users')} u GROUP BY state",
        PASS,
        None,
    ),
    (
        58,
        "C",
        f"SELECT state, ARRAY_AGG(u.id) FROM {T('users')} u GROUP BY state",
        POLICY,
        "demographic_pairing",
    ),
    (
        59,
        "C",
        f"SELECT CAST(user_id AS STRING) AS c, o.gender FROM {T('orders')} o",
        POLICY,
        "demographic_pairing",
    ),
    (
        60,
        "M",
        (
            f"WITH c AS (SELECT oi.user_id, u.state, SUM(oi.sale_price) AS spend "
            f"FROM {T('order_items')} oi JOIN {T('users')} u ON u.id = oi.user_id GROUP BY 1, 2) "
            "SELECT state, AVG(spend) AS avg_spend FROM c GROUP BY state"
        ),
        PASS,
        None,
    ),
    (
        61,
        "M",
        (
            f"WITH c AS (SELECT oi.user_id, u.state FROM {T('order_items')} oi "
            f"JOIN {T('users')} u ON u.id = oi.user_id) SELECT user_id FROM c WHERE state = 'CA'"
        ),
        POLICY,
        "demographic_pairing",
    ),
    (
        62,
        "C",
        (
            f"SELECT FARM_FINGERPRINT(CAST(user_id AS STRING)) AS h, age FROM {T('order_items')} oi "
            f"JOIN {T('users')} u ON u.id = oi.user_id"
        ),
        POLICY,
        "demographic_pairing",
    ),
]


@pytest.mark.parametrize(
    ("case", "role", "sql", "outcome", "reason"),
    CASES,
    ids=[f"case{c[0]}" for c in CASES],
)
def test_spec_cases(guards, case, role, sql, outcome, reason):
    result = run(guards, role, sql)
    assert (result.outcome, result.reason_code) == (outcome, reason), (
        result.model_feedback or result.user_message
    )
    if outcome is POLICY:
        assert result.user_message and result.sql is None
    if outcome is PARSE:
        assert result.model_feedback and result.sql is None


# --- extra bypass attempts -------------------------------------------------------------------------

EXTRA: list[tuple[str, str, GuardOutcome, str | None]] = [
    # PII through aliases, subqueries, CTEs, struct access and functions
    (
        "C",
        f"WITH x AS (SELECT id, last_name AS ln FROM {T('users')}) SELECT ln FROM x",
        POLICY,
        "pii_column",
    ),
    (
        "C",
        f"SELECT COUNT(*) FROM {T('users')} u WHERE u.postal_code = '10001'",
        POLICY,
        "pii_column",
    ),
    ("C", f"SELECT u.`Street_Address` FROM {T('users')} u", POLICY, "pii_column"),
    (
        "C",
        (
            f"SELECT COUNT(*) FROM {T('orders')} WHERE user_id IN (SELECT id FROM {T('users')} "
            "WHERE first_name = 'Anna')"
        ),
        POLICY,
        "pii_column",
    ),
    (
        "C",
        f"SELECT JSON_EXTRACT_SCALAR(TO_JSON_STRING(t), \"$['email']\") FROM {T('users')} t",
        POLICY,
        "pii_column",
    ),
    ("C", f"SELECT FORMAT('%T', u) FROM {T('users')} u", POLICY, "row_value"),
    ("C", f"SELECT s FROM (SELECT id FROM {T('users')}) s", POLICY, "row_value"),
    # Tables: other projects and datasets, quoting, wildcard suffix
    (
        "C",
        "SELECT COUNT(*) FROM `other-project.thelook_ecommerce.orders`",
        POLICY,
        "table_not_allowed",
    ),
    (
        "C",
        "SELECT COUNT(*) FROM `bigquery-public-data.other.orders`",
        POLICY,
        "table_not_allowed",
    ),
    ("C", f"SELECT COUNT(*) FROM {T('inventory_items')}", POLICY, "table_not_allowed"),
    (
        "C",
        "SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.order*`",
        POLICY,
        "table_not_allowed",
    ),
    ("C", "SELECT COUNT(*) FROM thelook_ecommerce.orders", PASS, None),
    ("C", "SELECT 1 AS x", PASS, None),
    # Forbidden functions and parameters
    ("C", "SELECT AI.GENERATE('hello')", POLICY, "forbidden_construct"),
    ("C", "SELECT KEYS.NEW_KEYSET('AEAD_AES_GCM_256')", POLICY, "forbidden_construct"),
    (
        "C",
        "SELECT * FROM ML.PREDICT(MODEL `p.d.m`, (SELECT 1 AS x))",
        POLICY,
        "forbidden_construct",
    ),
    (
        "C",
        f"SELECT COUNT(*) FROM {T('orders')} WHERE status = ?",
        POLICY,
        "forbidden_construct",
    ),
    # Statements
    (
        "C",
        f"UPDATE {T('products')} SET retail_price = 0 WHERE TRUE",
        POLICY,
        "not_select",
    ),
    ("C", f"TRUNCATE TABLE {T('orders')}", POLICY, "not_select"),
    ("C", "CALL proc()", POLICY, "not_select"),
    ("C", "BEGIN SELECT 1; END", POLICY, "not_select"),
    ("C", "SET x = 1", POLICY, "not_select"),
    ("C", "", PARSE, "syntax"),
    ("C", "SELECT 1 " + " " * 20_001, PARSE, "too_long"),
    # Columns
    ("C", f"SELECT revenue_x FROM {T('order_items')}", PARSE, "unknown_column"),
    # Brand literals
    ("M", BRAND_FILTER + "p.brand = 'calvin  klein'", PARSE, "brand_name_case"),
    ("M", BRAND_FILTER + "p.brand IN UNNEST(['Nike'])", POLICY, "brand_out_of_scope"),
    ("M", BRAND_FILTER + "'Nike' = p.brand", POLICY, "brand_out_of_scope"),
    (
        "M",
        f"WITH b AS (SELECT brand FROM {T('products')}) SELECT COUNT(*) FROM b WHERE brand = 'Nike'",
        POLICY,
        "brand_out_of_scope",
    ),
    ("M", BRAND_FILTER + 'p.brand = "Levi\'s"', PASS, None),
    ("C", BRAND_FILTER + "p.brand = 'Nike'", PASS, None),
    # Demographic pairing: more shapes
    (
        "C",
        f"SELECT o.user_id FROM {T('orders')} o WHERE o.gender = 'F'",
        POLICY,
        "demographic_pairing",
    ),
    (
        "C",
        f"SELECT user_id FROM {T('order_items')} UNION ALL SELECT id FROM {T('users')} WHERE country = 'Brasil'",
        POLICY,
        "demographic_pairing",
    ),
    (
        "C",
        (
            f"SELECT state, SUM(sale_price) AS s FROM {T('order_items')} oi JOIN {T('users')} u "
            "ON u.id = oi.user_id GROUP BY oi.user_id, state"
        ),
        POLICY,
        "demographic_pairing",
    ),
    (
        "C",
        (
            f"SELECT (SELECT MAX(u2.id) FROM {T('users')} u2 WHERE u2.state = u.state) AS m "
            f"FROM {T('users')} u"
        ),
        POLICY,
        "demographic_pairing",
    ),
    (
        "C",
        (
            f"SELECT traffic_source, COUNT(*) AS n, APPROX_COUNT_DISTINCT(id) AS c FROM {T('users')} "
            "GROUP BY traffic_source"
        ),
        PASS,
        None,
    ),
    (
        "C",
        f"SELECT age, COUNT(*) OVER (PARTITION BY id) AS n FROM {T('users')}",
        PASS,
        None,
    ),
]


@pytest.mark.parametrize(("role", "sql", "outcome", "reason"), EXTRA)
def test_bypass_attempts(guards, role, sql, outcome, reason):
    result = run(guards, role, sql)
    assert (result.outcome, result.reason_code) == (outcome, reason), (
        result.model_feedback or result.user_message
    )


# --- the rewrite (cases 24, 25, 29, 36, 37, 40, 41) ------------------------------------------------


def _base_tables(sql: str) -> list[exp.Table]:
    tree = sqlglot.parse_one(sql, read="bigquery")
    return [t for t in tree.find_all(exp.Table) if is_base_table(t)]


def test_case_24_manager_scope_rewrites_both_tables_with_one_parameter(guards):
    result = run(guards, "M", REVENUE_BY_BRAND)
    assert result.rewritten and result.tables == {"order_items", "products"}
    assert result.sql.count("IN UNNEST(@scope_brands)") == 2
    assert ") AS oi" in result.sql and ") AS p" in result.sql
    [param] = result.params
    assert param.name == "scope_brands" and param.array_type == "STRING"
    assert param.values == ["Calvin Klein", "Levi's"]


def test_case_25_ceo_has_no_brand_filter_and_no_parameter(guards):
    result = run(guards, "C", REVENUE_BY_BRAND)
    assert result.params == [] and "scope_brands" not in result.sql
    assert not result.rewritten


def test_case_13_short_name_gets_the_full_name(guards):
    result = run(guards, "M", "SELECT COUNT(*) FROM orders")
    assert "bigquery-public-data" in result.sql and ") AS orders" in result.sql


def test_case_29_brand_literal_stays_and_scope_still_applies(guards):
    result = run(guards, "M", BRAND_FILTER + "p.brand = 'Levi\\'s'")
    assert result.passed and "IN UNNEST(@scope_brands)" in result.sql
    assert "p.brand = 'Levi\\'s'" in result.sql


def test_case_30_correlated_subquery_table_is_rewritten(guards):
    sql = next(c[2] for c in CASES if c[0] == 30)
    result = run(guards, "M", sql)
    assert ") AS p2" in result.sql
    assert verify_scope(result.sql, MANAGER, guards["M"].templates)


def test_case_32_cte_body_is_rewritten(guards):
    result = run(guards, "M", next(c[2] for c in CASES if c[0] == 32))
    assert result.sql.startswith("WITH x AS (SELECT id, brand FROM (SELECT")


def test_case_36_users_only_with_scoped_order_items(guards):
    result = run(guards, "M", next(c[2] for c in CASES if c[0] == 36))
    assert "x.id IN (SELECT i.user_id" in result.sql
    assert "email" not in result.sql and "first_name" not in result.sql


def test_case_37_each_branch_of_a_union_is_rewritten(guards):
    result = run(guards, "M", next(c[2] for c in CASES if c[0] == 37))
    assert result.sql.count("IN UNNEST(@scope_brands)") == 2


def test_case_40_every_base_table_is_inside_a_scope_subquery(guards):
    result = run(guards, "M", REVENUE_BY_BRAND)
    assert verify_scope(result.sql, MANAGER, guards["M"].templates)
    bodies = guards["M"].templates.bodies
    for table in _base_tables(result.sql):
        ancestors = []
        node = table.parent
        while node is not None:
            ancestors.append(node)
            node = node.parent
        assert any(
            isinstance(a, exp.Subquery) and a.this.sql(dialect="bigquery") in bodies
            for a in ancestors
        ), table.sql()


def test_case_40_unscoped_sql_fails_the_check(guards):
    assert not verify_scope(REVENUE_BY_BRAND, MANAGER, guards["M"].templates)


def test_case_41_apostrophe_brand_only_in_the_parameter(guards):
    result = run(guards, "M", REVENUE_BY_BRAND)
    assert "Levi" not in result.sql and "Calvin" not in result.sql
    assert "Levi's" in result.params[0].values


def test_ceo_users_table_has_no_pii_columns(guards):
    result = run(
        guards, "C", f"SELECT state, COUNT(*) AS n FROM {T('users')} GROUP BY state"
    )
    assert result.rewritten and "FROM (SELECT x.id, x.age" in result.sql
    assert result.params == []


def test_manager_orders_have_no_num_of_item(guards):
    result = run(guards, "M", f"SELECT COUNT(*) AS n FROM {T('orders')}")
    assert "num_of_item" not in result.sql


def test_manager_without_tables_needs_no_parameter(guards):
    result = run(guards, "M", "SELECT 1 AS one")
    assert result.passed and result.params == []


def test_feedback_for_unknown_column_lists_columns_without_pii(guards):
    result = run(guards, "C", f"SELECT nope FROM {T('users')}")
    assert "nope" in result.model_feedback and "state" in result.model_feedback
    assert "email" not in result.model_feedback


def test_case_28_feedback_names_the_exact_brand(guards):
    result = run(guards, "M", BRAND_FILTER + "p.brand = 'levi\\'s'")
    assert "Levi's" in result.model_feedback


def test_typographic_apostrophe_gets_the_exact_name(guards):
    result = run(guards, "M", BRAND_FILTER + "p.brand = 'Levi’s'")
    assert result.reason_code == "brand_name_case" and "Levi's" in result.model_feedback


def test_case_34_feedback_says_count_from_order_items(guards):
    result = run(guards, "M", f"SELECT SUM(num_of_item) FROM {T('orders')}")
    assert "order_items" in result.model_feedback


def test_pass_result_has_filters_output_columns_and_customer_level(guards):
    result = run(
        guards,
        "M",
        f"SELECT user_id, SUM(sale_price) AS spend FROM {T('order_items')} "
        "WHERE status = 'Complete' AND created_at >= TIMESTAMP('2024-01-01') GROUP BY user_id",
    )
    assert result.customer_level and result.output_columns == ["user_id", "spend"]
    ops = {(f.table, f.column, f.op, f.values) for f in result.filters}
    assert ("order_items", "status", "=", ("Complete",)) in ops
    assert ("order_items", "created_at", ">=", ("2024-01-01",)) in ops
    assert result.source_sql.startswith("SELECT user_id")


def test_each_decision_writes_one_guard_span_without_values(
    tmp_path, test_schema, test_values
):
    init_tracing(tmp_path)
    guard = SqlGuard(MANAGER, test_schema, test_values)
    guard.check(REVENUE_BY_BRAND)
    guard.check(f"SELECT email FROM {T('users')}")
    records = [
        json.loads(line)
        for f in tmp_path.glob("*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    spans = [r for r in records if r["kind"] == "guard"]
    assert len(spans) == 2
    first, second = (s["attributes"] for s in spans)
    assert first["outcome"] == "pass" and first["rewritten"] is True
    assert first["tables"] == ["order_items", "products"]
    assert first["masker_hit"] is False
    assert second["outcome"] == "policy_block" and second["reason_code"] == "pii_column"
    assert second["error_type"] == "guard_policy_block"
    text = json.dumps(records)
    # Spec 35: the guard span has the masked source SQL, never the user's scope values.
    assert "Calvin" not in text and "scope_brands" not in text
    assert first["sql_masked"].startswith("SELECT p.brand")


def test_guard_bug_never_passes(guards, monkeypatch):
    guard = guards["C"]
    monkeypatch.setattr(guard, "_star", lambda tree: 1 / 0)
    result = guard.check(f"SELECT COUNT(*) FROM {T('orders')}")
    assert result.outcome is PARSE and result.reason_code == "unverifiable"
    assert result.internal_error


# The read-only queries of the spec 01 minimal guard (removed by spec 30) still pass.
@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT COUNT(*) FROM {T('orders')} WHERE EXTRACT(YEAR FROM created_at) = 2024",
        (
            "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM thelook_ecommerce.order_items oi "
            "JOIN thelook_ecommerce.products p ON p.id = oi.product_id GROUP BY 1"
        ),
        f"WITH x AS (SELECT user_id FROM {T('order_items')}) SELECT COUNT(DISTINCT user_id) FROM x",
        f"SELECT state, COUNT(*) FROM {T('users')} GROUP BY state",
        f"SELECT COUNT(*) AS n FROM {T('users')}",
        f"SELECT status FROM {T('orders')} UNION ALL SELECT status FROM {T('order_items')}",
    ],
)
@pytest.mark.parametrize("role", ["M", "C"])
def test_spec_01_read_only_queries_still_pass(guards, role, sql):
    assert run(guards, role, sql).passed


# --- Spec 50 round 1: a brand name with an apostrophe (PP-06, TM-05, CB-08) ----------------------
@pytest.mark.parametrize("literal", ["'Levi's'", "'Levi''s'"])
def test_apostrophe_in_single_quotes_gets_feedback_that_names_it(guards, literal):
    sql = (
        f"SELECT SUM(oi.sale_price) AS revenue FROM {T('order_items')} oi "
        f"JOIN {T('products')} p ON p.id = oi.product_id WHERE p.brand = {literal}"
    )
    result = run(guards, "M", sql)
    assert result.outcome is PARSE and result.reason_code == "syntax"
    assert "apostrophe" in result.model_feedback
    assert '"Levi\'s"' in result.model_feedback


def test_double_quoted_brand_with_an_apostrophe_passes(guards):
    sql = (
        f"SELECT SUM(oi.sale_price) AS revenue FROM {T('order_items')} oi "
        f'JOIN {T("products")} p ON p.id = oi.product_id WHERE p.brand = "Levi\'s"'
    )
    assert run(guards, "M", sql).outcome is PASS


def test_other_syntax_errors_get_no_apostrophe_hint(guards):
    result = run(guards, "C", f"SELECT FROM WHERE {T('orders')}")
    assert result.outcome is PARSE and "apostrophe" not in (result.model_feedback or "")


def test_churn_pattern_of_the_glossary_passes_the_guard(guards):
    """Spec 50 round 1 (TM-10, MS-03): the SQL pattern of `monthly_churn_rate` must pass the
    guard for both roles when the model fills in the dates."""
    from opsfleet_agent.agent.context import PromptLibrary

    text = PromptLibrary().text("definitions")
    pattern = text.split("```sql", 1)[1].split("```", 1)[0]
    sql = (
        pattern.replace("<end>", "2026-07-01")
        .replace("<first month>", "2026-01-01")
        .replace("<last month>", "2026-06-01")
    )
    for role in ("M", "C"):
        result = run(guards, role, sql)
        assert result.outcome is PASS, (role, result.reason_code, result.model_feedback)
