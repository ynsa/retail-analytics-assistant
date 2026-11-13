"""Fakes and builders for the report tests (spec 32) and the delete tests (spec 33)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.reports.models import Report
from tests.conftest import FakeLLM, frame_result, route_reply

T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
T_PRODUCTS = "`bigquery-public-data.thelook_ecommerce.products`"
FIXED_NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

MONTHLY_SQL = (
    "SELECT FORMAT_TIMESTAMP('%Y-%m', oi.created_at) AS month, SUM(oi.sale_price) AS revenue "
    f"FROM {T_ITEMS} oi WHERE oi.status NOT IN ('Cancelled', 'Returned') "
    "AND oi.created_at >= TIMESTAMP('2026-01-01') AND oi.created_at < TIMESTAMP('2026-04-01') "
    "GROUP BY month ORDER BY month"
)
BRAND_SQL = (
    f"SELECT p.brand, SUM(oi.sale_price) AS revenue FROM {T_ITEMS} oi "
    f"JOIN {T_PRODUCTS} p ON p.id = oi.product_id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') "
    "AND oi.created_at >= TIMESTAMP('2026-01-01') AND oi.created_at < TIMESTAMP('2026-04-01') "
    "GROUP BY p.brand"
)
CATEGORY_SQL = (
    f"SELECT p.category, SUM(oi.sale_price) AS revenue FROM {T_ITEMS} oi "
    f"JOIN {T_PRODUCTS} p ON p.id = oi.product_id "
    "WHERE p.brand = 'Carhartt' AND oi.status NOT IN ('Cancelled', 'Returned') "
    "GROUP BY p.category"
)


def monthly_frame():
    return frame_result(
        pd.DataFrame(
            {
                "month": ["2026-01", "2026-02", "2026-03"],
                "revenue": [1200.5, 1300.25, 900.0],
            }
        )
    )


def brand_frame():
    return frame_result(
        pd.DataFrame(
            {"brand": ["Calvin Klein", "Carhartt"], "revenue": [2000.5, 1400.25]}
        )
    )


def category_frame():
    return frame_result(
        pd.DataFrame(
            {"category": ["Outerwear & Coats", "Pants"], "revenue": [900.0, 500.25]}
        )
    )


def bq_error(kind: ErrorType = ErrorType.BQ_SYNTAX) -> AgentError:
    return AgentError(
        kind, "The query has an error.", "Syntax error: unexpected keyword"
    )


def report_route(action: str = "create", **fields: Any) -> dict[str, Any]:
    return route_reply("report", report_request={"action": action, **fields})


def plan_reply(
    queries: list[tuple[str, str]] | None = None,
    *,
    title: str = "Q1 2026 sales review: Calvin Klein and Carhartt",
    start: str = "2026-01-01",
    end: str = "2026-03-31",
    compare: tuple[str, str] | None = ("2025-10-01", "2025-12-31"),
) -> dict[str, Any]:
    queries = (
        queries
        if queries is not None
        else [
            ("Revenue by month", MONTHLY_SQL),
            ("Revenue by brand", BRAND_SQL),
        ]
    )
    return {
        "title": title,
        "period": {"start": start, "end": end},
        "compare_period": {"start": compare[0], "end": compare[1]} if compare else None,
        "queries": [
            {
                "id": f"q{i}",
                "purpose": purpose,
                "sql": sql,
                "definitions_used": ["revenue"],
            }
            for i, (purpose, sql) in enumerate(queries, start=1)
        ],
        "definitions_used": ["revenue", "quarter"],
    }


def draft_reply(**overrides: Any) -> dict[str, Any]:
    draft = {
        "summary": [
            "Your brands had revenue of {{calc:sum(r1.revenue)}} in Q1 2026.",
            "February was the strongest month with $1,300.25.",
        ],
        "findings": [
            {
                "text": "Revenue was $1,200.50 in January and $900.00 in March.",
                "table": {"ref": "r1", "columns": ["month", "revenue"]},
            },
            {
                "text": "Calvin Klein had $2,000.50 and Carhartt had $1,400.25.",
                "table": {"ref": "r2", "columns": []},
            },
        ],
        "reasons": ["The data shows a drop in March for both brands."],
        "action_items": [
            {
                "action": "Restock Carhartt outerwear",
                "why": "March fell to $900.00.",
                "metric_to_watch": "Monthly revenue",
            },
            {
                "action": "Repeat the February promotion",
                "why": "February had $1,300.25.",
                "metric_to_watch": "Orders",
            },
            {
                "action": "Check the March drop",
                "why": "The data shows a fall.",
                "metric_to_watch": "Return rate",
            },
        ],
        "charts": [
            {
                "ref": "r1",
                "type": "line",
                "x": "month",
                "y": ["revenue"],
                "title": "Revenue by month",
            }
        ],
        "definitions_used": ["revenue"],
    }
    draft.update(overrides)
    return draft


def plain_draft(**overrides: Any) -> dict[str, Any]:
    """A draft without figures: it fits any results, so no rewrite request follows."""
    draft = draft_reply(
        summary=["Revenue changed across the period."],
        findings=[
            {
                "text": "The first result shows the trend.",
                "table": {"ref": "r1", "columns": []},
            }
        ],
        action_items=[
            {
                "action": "Restock outerwear",
                "why": "Demand rose.",
                "metric_to_watch": "Revenue",
            },
            {
                "action": "Review prices",
                "why": "Margins vary.",
                "metric_to_watch": "Margin",
            },
            {
                "action": "Check returns",
                "why": "Returns vary.",
                "metric_to_watch": "Return rate",
            },
        ],
        charts=[],
    )
    draft.update(overrides)
    return draft


def report_llm(
    *extra: Any,
    plan: dict | None = None,
    draft: dict | None = None,
    route: dict | None = None,
) -> FakeLLM:
    """route, plan, (extra replies, for example corrections), write."""
    return FakeLLM(
        [route or report_route(), plan or plan_reply(), *extra, draft or draft_reply()]
    )


def make_report(
    report_id: str,
    owner_id: str = "maya",
    *,
    conversation_id: str = "conv-1",
    title: str = "Q1 report",
    body: str = "Body text.",
    entities: dict[str, list[str]] | None = None,
    created_at: str = "2026-11-11T10:00:00+00:00",
    charts: list[dict[str, Any]] | None = None,
    sections: dict[str, Any] | None = None,
) -> Report:
    return Report(
        report_id=report_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
        title=title,
        question="Create a report",
        created_at=created_at,
        period_start="2026-01-01",
        period_end="2026-03-31",
        scope_brands=["Calvin Klein", "Carhartt"],
        sql=[],
        body_markdown=body,
        sections=sections or {"summary": ["S."], "period_text": "1 Jan – 31 Mar 2026"},
        charts=charts or [],
        definitions=[],
        entities=entities or {},
    )


def row_of(conn, report_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    for key in ("sections", "charts", "entities", "sql", "definitions", "scope_brands"):
        data[key] = json.loads(data[key])
    return data
