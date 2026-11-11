import pytest

from opsfleet_agent.guard.minimal import GuardOutcome, check_read_only

DATASET = "bigquery-public-data.thelook_ecommerce"
TABLES = ["orders", "order_items", "products", "users"]


def check(sql: str):
    return check_read_only(sql, DATASET, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT COUNT(*) FROM `{DATASET}.orders` WHERE EXTRACT(YEAR FROM created_at) = 2024",
        (
            "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM thelook_ecommerce.order_items oi "
            "JOIN thelook_ecommerce.products p ON p.id = oi.product_id GROUP BY 1"
        ),
        f"WITH x AS (SELECT user_id FROM `{DATASET}.order_items`) SELECT COUNT(DISTINCT user_id) FROM x",
        f"SELECT state, COUNT(*) FROM `{DATASET}.users` GROUP BY state",
        f"SELECT COUNT(*) AS n FROM `{DATASET}.users`",
        f"SELECT status FROM `{DATASET}.orders` UNION ALL SELECT status FROM `{DATASET}.order_items`",
    ],
)
def test_read_only_queries_pass(sql):
    assert check(sql).outcome is GuardOutcome.PASS


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DROP TABLE orders", "write_statement"),
        ("DELETE FROM orders WHERE TRUE", "write_statement"),
        ("INSERT INTO orders (order_id) VALUES (1)", "write_statement"),
        ("UPDATE orders SET status = 'x' WHERE TRUE", "write_statement"),
        ("CREATE TABLE my.copy AS SELECT * FROM orders", "write_statement"),
        (
            "MERGE orders t USING orders s ON TRUE WHEN MATCHED THEN DELETE",
            "write_statement",
        ),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        (f"SELECT * FROM `{DATASET}.events`", "table_not_allowed"),
        (f"SELECT * FROM `{DATASET}.INFORMATION_SCHEMA.TABLES`", "table_not_allowed"),
        (f"SELECT * FROM `{DATASET}.*`", "table_not_allowed"),
        (
            "SELECT * FROM `other-project.thelook_ecommerce.orders`",
            "dataset_not_allowed",
        ),
        ("SELECT * FROM `bigquery-public-data.other.orders`", "dataset_not_allowed"),
        ("SELECT 1 AS x", "no_table"),
        (f"SELECT email FROM `{DATASET}.users`", "pii_column"),
        (
            f"SELECT u.first_name, COUNT(*) FROM `{DATASET}.users` u GROUP BY 1",
            "pii_column",
        ),
        (f"SELECT * FROM `{DATASET}.users`", "pii_star"),
        (f"SELECT u.* FROM `{DATASET}.users` u", "pii_star"),
    ],
)
def test_unsafe_queries_are_blocked(sql, reason):
    result = check(sql)
    assert result.outcome is GuardOutcome.POLICY_BLOCK
    assert result.reason_code == reason
    assert result.user_message


@pytest.mark.parametrize("sql", ["SELEC wrong", "", "not sql at all ("])
def test_invalid_text_is_not_a_pass(sql):
    assert check(sql).outcome is not GuardOutcome.PASS
