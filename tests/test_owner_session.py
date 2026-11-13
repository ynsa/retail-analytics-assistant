"""Group OS of spec 21 (B2.2a): the owner's test session of 2026-11-11, with a fake model.

Five turns as `maya`: a greeting, two access questions, "the stats for the last nmonth" and
"last month". The expected behavior: an access answer with the user's brands, no clarifying
question, formatted numbers, the brand scope in the SQL, and a footer with the assumptions.
"""

from __future__ import annotations

import re

import pandas as pd

from tests.conftest import FakeBQ, FakeLLM, frame_result, route_reply, sql_reply

T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
OVERVIEW_SQL = f"""
WITH cur AS (
  SELECT SUM(oi.sale_price) AS revenue, COUNT(DISTINCT oi.order_id) AS orders, COUNT(*) AS items_sold
  FROM {T_ITEMS} oi
  WHERE oi.status NOT IN ('Cancelled', 'Returned')
    AND oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01')
), prev AS (
  SELECT SUM(oi.sale_price) AS revenue, COUNT(DISTINCT oi.order_id) AS orders, COUNT(*) AS items_sold
  FROM {T_ITEMS} oi
  WHERE oi.status NOT IN ('Cancelled', 'Returned')
    AND oi.created_at >= TIMESTAMP('2026-09-01') AND oi.created_at < TIMESTAMP('2026-10-01')
)
SELECT cur.revenue, prev.revenue AS revenue_prev,
  (cur.revenue - prev.revenue) / prev.revenue * 100 AS revenue_change_pct,
  cur.orders, prev.orders AS orders_prev,
  (cur.orders - prev.orders) / prev.orders * 100 AS orders_change_pct,
  cur.items_sold, prev.items_sold AS items_sold_prev,
  (cur.items_sold - prev.items_sold) / prev.items_sold * 100 AS items_sold_change_pct,
  cur.revenue / cur.orders AS average_order_value,
  prev.revenue / prev.orders AS average_order_value_prev,
  (cur.revenue / cur.orders - prev.revenue / prev.orders) / (prev.revenue / prev.orders) * 100
    AS average_order_value_change_pct
FROM cur CROSS JOIN prev
"""
ROW = {
    "revenue": [24813.170041322708],
    "revenue_prev": [22004.51999759674],
    "revenue_change_pct": [12.764126319050],
    "orders": [301],
    "orders_prev": [283],
    "orders_change_pct": [6.3604240282685],
    "items_sold": [355],
    "items_sold_prev": [331],
    "items_sold_change_pct": [7.2507552870090],
    "average_order_value": [82.43578086818176],
    "average_order_value_prev": [77.75448762401674],
    "average_order_value_change_pct": [6.0207],
}
ANSWER = (
    "Your brands had revenue of $24,813.17 in October 2026, up 12.8% from $22,004.52 in "
    "September. Orders rose to 301 (+6.4%), items sold to 355 (+7.3%), and the average order "
    "value to $82.44 (+6.0%).\n\n"
    "| Figure | Oct 2026 | Sep 2026 | Change |\n|---|---|---|---|\n"
    "| Revenue | $24,813.17 | $22,004.52 | +12.8% |\n"
    "| Orders | 301 | 283 | +6.4% |\n"
    "| Items sold | 355 | 331 | +7.3% |\n"
    "| Average order value | $82.44 | $77.75 | +6.0% |\n\n"
    "Do you want other figures, for example the return rate or the top products?"
)
PLAN = {
    "purpose": "Default overview for last month against the month before",
    "period": "2026-10-01..2026-10-31",
    "definitions": [
        "revenue",
        "orders",
        "items_sold",
        "average_order_value",
        "last_month",
    ],
    "assumptions": ["last month = 1–31 Oct 2026", "figures = default overview"],
}


def test_group_os_owner_session(make_session):
    llm = FakeLLM(
        [
            route_reply("chitchat", chitchat_kind="greeting"),
            route_reply("schema", schema_kind="access", follow_up=True),
            route_reply("schema", schema_kind="access", follow_up=True),
            route_reply(),
            sql_reply(OVERVIEW_SQL, **PLAN),
            ANSWER,
            route_reply(follow_up=True),
            sql_reply(OVERVIEW_SQL, **PLAN),
            ANSWER,
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame(ROW)), frame_result(pd.DataFrame(ROW))])
    session, _, _ = make_session("maya", llm=llm, bq=bq)

    os1 = session.ask("hi")
    assert os1.intent == "chitchat" and os1.totals["bq_queries"] == 0
    assert os1.answer.startswith("Hello, Maya.")

    for message in (
        "yes, what projects I have access to?",
        "what companies I can check in the data?",
    ):
        access = session.ask(message)
        assert access.intent == "schema" and access.totals["llm_calls"] == 1
        assert access.totals["bq_queries"] == 0
        assert "Calvin Klein and Carhartt" in access.answer
        assert "brand manager" in access.answer
        assert "6 Jan 2019" in access.answer  # the first date of the data
        assert access.answer.count("\n1. ") + access.answer.count("\n2. ") == 2
        for word in ("Google", "GCP", "project", "key", "bigquery"):
            assert word.lower() not in access.answer.lower()

    os4 = session.ask("give me the stats for the last nmonth")
    assert os4.intent == "analysis" and "?" not in os4.answer.split("\n")[0]
    assert os4.totals["llm_calls"] == 3 and os4.totals["bq_queries"] == 1
    assert "IN UNNEST(@scope_brands)" in os4.executed_sql[0]
    assert bq.params[0][0].values == ["Calvin Klein", "Carhartt"]
    answer_prompt = llm.calls[5]["prompt"]
    assert "$24,813.17" in answer_prompt and "12.8%" in answer_prompt
    assert "24813.17004" not in answer_prompt
    assert not re.search(
        r"\d+\.\d{3,}", os4.answer
    )  # no value with more than 2 decimals
    assert os4.unverified_figures == 0
    assert "Period: 1–31 Oct 2026 · Brands: Calvin Klein, Carhartt" in os4.footer
    assert (
        "Revenue = sale price of items that were not cancelled or returned"
        in os4.footer
    )
    # The user named "last month", so the period is not a default (spec 31, B7, TM-01 fix).
    assert "_Assumed: figures = default overview._" in os4.footer

    os5 = session.ask("last month")
    assert os5.intent == "analysis" and os5.error_type is None
    assert os5.answer.split("\n\n")[0] == os4.answer.split("\n\n")[0]
    total_calls = sum(r.totals["llm_calls"] for r in (os1, access, os4, os5)) + 1
    assert total_calls == 9
    messages = session.graph.get_state(
        {"configurable": {"thread_id": session.conversation_id}}
    ).values["messages"]
    # 5 questions, 5 answers and 2 result references (spec 34), no copies from the subgraph.
    assert len(messages) == 12
    assert [m["kind"] for m in messages].count("result_ref") == 2
