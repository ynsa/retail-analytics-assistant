"""Observability and statistics (spec 35, B4 cases 1-18). Fakes only: no model call, no query."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from opsfleet_agent import tracing
from opsfleet_agent.errors import AgentError, ErrorType, LimitExceeded
from opsfleet_agent.guard.scanner import PATTERNS
from opsfleet_agent.llm import GeminiClient
from opsfleet_agent.observability import stats as stats_module
from opsfleet_agent.observability.redact import (
    ATTRIBUTE_ALLOWLIST,
    Redactor,
    mask_prompt,
    mask_sql,
    mask_text,
)
from opsfleet_agent.observability.retention import prune
from opsfleet_agent.observability.stats import (
    EXPORT_COLUMNS,
    Spread,
    compute_stats,
    nearest_rank,
    stats_text,
)
from opsfleet_agent.observability.trace_view import load_trace, trace_text
from opsfleet_agent.observability.turn_metrics import (
    SpanRecord,
    TurnFacts,
    merge_metrics,
    turn_metrics,
)
from opsfleet_agent.reports.library import ReportLibrary
from tests.conftest import (
    FakeBQ,
    FakeLLM,
    frame_result,
    read_spans,
    route_reply,
    sql_reply,
)
from tests.report_helpers import FIXED_NOW, make_report

T_USERS = "`bigquery-public-data.thelook_ecommerce.users`"
T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
# Glossary filter and the stated period of `sql_reply` (the checks of spec 31, B7 pass).
ORDERS_SQL = (
    f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {T_ITEMS} oi "
    "WHERE oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01') "
    "AND oi.status NOT IN ('Cancelled', 'Returned')"
)
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def orders(n: int = 1200):
    return frame_result(pd.DataFrame({"orders": [n]}))


def log_lines(config) -> list[dict[str, Any]]:
    out = []
    for f in config.settings.data_path("logs").glob("*.jsonl"):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def turn_rows(conn) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM conversation_turns ORDER BY turn_no")
    ]


# --- B4 1-4: mask_sql ----------------------------------------------------------------------------
def test_1_email_literal_is_masked_with_its_length():
    sql = f"SELECT COUNT(*) FROM {T_USERS} u WHERE email = 'anna@example.com'"
    masked = mask_sql(sql)
    assert "email = '<s:16>'" in masked and "anna" not in masked


def test_2_brand_literal_stays():
    sql = "SELECT SUM(oi.sale_price) FROM `x.products` p WHERE p.brand = 'Levi''s'"
    masked = mask_sql(sql)
    assert "Levi" in masked and "<s:" not in masked


@pytest.mark.parametrize(
    "column", ["category", "department", "name", "status", "state", "country"]
)
def test_2_other_business_columns_stay(column):
    masked = mask_sql(f"SELECT 1 FROM t WHERE {column} = 'Jeans' AND sku = 'AB-12'")
    assert "'Jeans'" in masked and "'<s:5>'" in masked


def test_3_sql_that_does_not_parse_masks_all_strings():
    masked = mask_sql(
        "SELEC brand = 'Levi''s' AND email = \"anna@example.com\" 1234567"
    )
    assert masked == "SELEC brand = '<s:7>' AND email = '<s:16>' <n>"


def test_4_number_compared_to_a_pii_column():
    masked = mask_sql(
        f"SELECT COUNT(*) FROM {T_USERS} WHERE postal_code = 94107 AND age > 30"
    )
    assert "postal_code = <n>" in masked and "age > 30" in masked


def test_mask_sql_is_idempotent_and_keeps_dates():
    sql = (
        f"SELECT * FROM {T_USERS} u WHERE u.first_name = 'Anna' AND u.state = 'Texas' "
        "AND user_id = 12345 AND created_at >= '2026-10-01'"
    )
    once = mask_sql(sql)
    assert mask_sql(once) == once
    assert "'<s:4>'" in once and "'Texas'" in once and "user_id = <n>" in once
    assert "'2026-10-01'" in once


# --- B4 5: logs ----------------------------------------------------------------------------------
def test_5_user_message_with_a_phone_number_is_masked_in_the_log(make_session, config):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "1,200 orders."])
    session, _, _ = make_session("maya", llm=llm, bq=FakeBQ([orders()]))
    session.send("Call me at (555) 123-4567 or anna@example.com: how many orders?")
    lines = log_lines(config)
    start = next(line for line in lines if line["message"] == "turn start")
    assert start["fields"]["user_text"].startswith("Call me at [removed] or [removed]")
    assert start["trace_id"] == session.last_trace_id
    text = json.dumps(lines)
    assert "555" not in text and "anna@" not in text


def test_5_log_formatter_masks_message_extra_and_exception(config):
    from opsfleet_agent.resilience.safe_node import init_logging

    init_logging(config.settings.data_path("logs"))
    log = logging.getLogger("opsfleet_agent.test")
    try:
        raise ValueError("bad value anna@example.com")
    except ValueError:
        log.exception(
            "failed for 555-123-4567",
            extra={"sql": "SELECT 1 FROM t WHERE email = 'bob@example.com'"},
        )
    [line] = [x for x in log_lines(config) if x["logger"] == "opsfleet_agent.test"]
    assert line["message"] == "failed for [removed]"
    assert line["fields"]["sql"] == "SELECT 1 FROM t WHERE email = '<s:15>'"
    assert "anna@" not in line["exception"] and "[removed]" in line["exception"]


def test_library_loggers_write_warnings_only(config):
    from opsfleet_agent.resilience.safe_node import init_logging

    init_logging(config.settings.data_path("logs"))
    for name in ("google", "urllib3", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


# --- B4 6-8: spans -------------------------------------------------------------------------------
def test_6_bigquery_error_that_quotes_a_literal_is_masked(config):
    tracing.init_tracing(config.settings.data_path("traces"))
    with (
        pytest.raises(AgentError),
        tracing.span(
            "bq.query",
            "sql",
            sql_masked=mask_sql("SELECT * FROM t WHERE email = 'anna@example.com'"),
        ),
    ):
        raise AgentError(
            ErrorType.BQ_SYNTAX,
            "The query failed.",
            "400 No matching signature for 'anna@example.com' near (555) 123-4567",
        )
    [span] = [s for s in read_spans(config) if s["name"] == "bq.query"]
    detail = span["attributes"]["error_detail"]
    assert "anna@" not in detail and "555" not in detail and "[removed]" in detail
    assert span["attributes"]["error_type"] == "bq_syntax" and span["status"] == "error"


def test_6_raw_sql_in_sql_masked_is_masked_by_the_exporter(config):
    tracing.init_tracing(config.settings.data_path("traces"))
    with tracing.span("bq.query", "sql", sql_masked="SELECT 1 WHERE email = 'a@b.co'"):
        pass
    [span] = read_spans(config)
    assert span["attributes"]["sql_masked"] == "SELECT 1 WHERE email = '<s:6>'"


def test_7_prompt_with_a_data_block_is_a_reference(config, budget):
    tracing.init_tracing(config.settings.data_path("traces"))
    prompt = (
        "Question: how many orders? Call 555-123-4567.\n"
        '<data source="query" ref="res_ab12cd34" rows="3">\n'
        "| brand | revenue |\n| Calvin Klein | 1200 |\n</data>\n"
        '<data source="summary">\nOld summary text.\n</data>'
    )
    usage = SimpleNamespace(prompt_token_count=50, candidates_token_count=5)
    response = SimpleNamespace(
        text="Write to bob@example.com", parsed=None, usage_metadata=usage
    )
    fake = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_: response)
    )
    client = GeminiClient(config.settings.models, api_key=None, client=fake)
    client.generate(
        budget,
        system="secret system text",
        prompt=prompt,
        purpose="write_answer",
        prompt_version="abc12345",
    )
    [span] = [s for s in read_spans(config) if s["kind"] == "llm"]
    masked = span["attributes"]["prompt_masked"]
    assert masked.startswith('<system version="abc12345"/>')
    assert '<data ref="res_ab12cd34" rows="3"/>' in masked
    assert '<data source="summary" chars=' in masked
    assert "Calvin Klein | 1200" not in masked and "555" not in masked
    assert "secret system text" not in json.dumps(span)
    assert span["attributes"]["output_masked"] == "Write to [removed]"


def test_7_capture_prompts_off(config, budget):
    tracing.init_tracing(config.settings.data_path("traces"))
    response = SimpleNamespace(text="ok", parsed=None, usage_metadata=None)
    fake = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_: response)
    )
    client = GeminiClient(
        config.settings.models, api_key=None, client=fake, capture_prompts=False
    )
    client.generate(budget, system="s", prompt="p", purpose="x")
    [span] = [s for s in read_spans(config) if s["kind"] == "llm"]
    assert (
        "prompt_masked" not in span["attributes"]
        and "output_masked" not in span["attributes"]
    )


def test_8_unknown_attribute_is_dropped(config):
    tracing.init_tracing(config.settings.data_path("traces"))
    with tracing.span(
        "node.route", "node", attempt=1, customer_email="x@y.com", rows_json="[1]"
    ):
        pass
    [span] = read_spans(config)
    assert span["attributes"] == {"attempt": 1}


def test_attribute_values_are_cut_at_2000_characters():
    redactor = Redactor()
    out = redactor.attributes(
        "sql", {"sql_masked": "SELECT " + "a, " * 2000 + "b FROM t"}
    )
    assert len(out["sql_masked"]) == 2000 and out["sql_masked"].endswith("…")


def test_each_spec_kind_has_its_attributes():
    assert {"sql_masked", "bytes_billed", "job_id"} <= ATTRIBUTE_ALLOWLIST["sql"]
    assert {"prompt_masked", "output_masked", "fallback_used"} <= ATTRIBUTE_ALLOWLIST[
        "llm"
    ]
    assert {"limit_name", "value", "max"} <= ATTRIBUTE_ALLOWLIST["limit"]


def test_events_are_written_and_masked(config):
    tracing.init_tracing(config.settings.data_path("traces"))
    with tracing.span("llm.route", "llm") as sp:
        sp.add_event("retry", {"attempt": 2, "error_type": "llm_timeout"})
        sp.add_event("note", {"text": "mail anna@example.com"})
    [span] = read_spans(config)
    assert span["events"][0] == {
        "name": "retry",
        "attributes": {"attempt": 2, "error_type": "llm_timeout"},
    }
    assert span["events"][1]["attributes"]["text"] == "mail [removed]"


def test_limit_span_is_written(config, budget):
    tracing.init_tracing(config.settings.data_path("traces"))
    budget.bq_queries = config.settings.limits.per_turn.max_bq_queries
    with pytest.raises(LimitExceeded):
        budget.charge_query()
    [span] = [s for s in read_spans(config) if s["kind"] == "limit"]
    assert span["name"] == "limit.exceeded"
    assert span["attributes"]["limit_name"] == "per_turn.max_bq_queries"
    assert span["attributes"]["error_type"] == "limit_exceeded"


def test_mask_text_and_prompt_are_idempotent():
    text = "Call 555-123-4567"
    assert mask_text(mask_text(text)) == mask_text(text) == "Call [removed]"
    once = mask_prompt('<data ref="res_1" rows="2">x</data>', "v1")
    assert '<data ref="res_1" rows="2"/>' in once


# --- B4 9: one row for each turn status -----------------------------------------------------------
@pytest.mark.parametrize(
    ("replies", "rows", "outcome", "kind", "error_type"),
    [
        (
            [route_reply(), sql_reply(ORDERS_SQL), "1,200 orders."],
            [orders()],
            "answered",
            "question",
            None,
        ),
        ([route_reply("off_topic")], [], "refused", "other", None),
        (
            [route_reply(), sql_reply(f"SELECT email FROM {T_USERS}")],
            [],
            "blocked",
            "question",
            "guard_policy_block",
        ),
        (
            [route_reply(), LimitExceeded("per_turn.max_llm_calls", "Too many steps.")],
            [],
            "limit",
            "question",
            "limit_exceeded",
        ),
        ([route_reply(), KeyboardInterrupt()], [], "cancelled", "other", None),
        (
            [route_reply(), RuntimeError("a bug")],
            [],
            "failed",
            "question",
            "internal_error",
        ),
    ],
    ids=["answered", "refused", "blocked", "limit", "cancelled", "internal_error"],
)
def test_9_each_turn_status_writes_one_row(
    make_session, conn, replies, rows, outcome, kind, error_type
):
    session, _, _ = make_session(
        "maya", llm=FakeLLM(list(replies)), bq=FakeBQ(list(rows))
    )
    result = session.send("How many orders last month?")
    [row] = turn_rows(conn)
    assert row["outcome"] == outcome and row["error_type"] == error_type
    assert row["user_id"] == "maya" and row["trace_id"] == result.trace_id
    assert row["duration_ms"] > 0 and row["answer_chars"] == len(result.answer)
    if outcome != "cancelled":
        assert row["kind"] == kind


def test_9_answered_row_has_the_figures(make_session, conn):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "There were 1,200 orders."])
    session, _, _ = make_session("maya", llm=llm, bq=FakeBQ([orders()]))
    result = session.send("How many orders last month?")
    [row] = turn_rows(conn)
    assert (row["llm_calls"], row["bq_queries"]) == (3, 1)
    assert row["tokens_in"] == result.totals["tokens_in"]
    assert row["bq_bytes_billed"] == result.totals["bytes_billed"]
    assert row["attempts_total"] == 1 and row["retried"] == 0
    tools = json.loads(row["tools_used"])
    assert tools["route"] == 1 and tools["write_sql"] == 1 and tools["run_query"] == 1
    assert row["answer_words"] == len(result.answer.split())
    assert row["prompt_version"] is None  # the fake model writes no llm span
    assert json.loads(row["errors"]) == {}


def test_9_self_correction_counts_the_attempts_and_the_resolved_error(
    make_session, conn
):
    llm = FakeLLM(
        [route_reply(), sql_reply("SELECT FROM WHERE"), sql_reply(ORDERS_SQL), "1,200."]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders()]))
    session.send("How many orders?")
    [row] = turn_rows(conn)
    assert (
        row["outcome"] == "answered"
        and row["retried"] == 1
        and row["attempts_total"] == 2
    )
    assert json.loads(row["errors"]) == {"guard_parse_error": 1}


def test_9_delete_confirmation_is_one_row_with_both_parts(make_session, conn, config):
    session, llm, _ = make_session(llm=FakeLLM(), bq=FakeBQ())
    session.services.clock = lambda: FIXED_NOW
    library = ReportLibrary(session.conn, config.settings, clock=lambda: FIXED_NOW)
    for i in range(2):
        library.create(
            make_report(
                f"R-0000{i + 1}",
                "maya",
                conversation_id=session.conversation_id,
                title=f"Report {i}",
            )
        )
    llm.replies.append(route_reply("delete", delete_request={"scope": "conversation"}))
    shown = session.send("Delete all the reports we made in this conversation")
    [row] = turn_rows(conn)
    assert (
        row["outcome"] == "confirmation_shown"
        and row["confirmation_outcome"] == "shown"
    )
    assert shown.confirmation is not None
    session.send("delete 2")
    [row] = turn_rows(conn)
    assert row["outcome"] == "confirmed" and row["confirmation_outcome"] == "confirmed"
    assert (
        row["kind"] == "delete" and row["llm_calls"] == 1
    )  # the route call of part one
    assert json.loads(row["tools_used"]).get("delete_reports") == 1


def test_turn_metrics_from_spans():
    def sp(name, kind, attrs=None, events=(), status="ok"):
        return SpanRecord(
            "t", name, None, name, kind, status, 5.0, dict(attrs or {}), tuple(events)
        )

    spans = [
        sp("node.write_sql", "node"),
        sp(
            "llm.write_sql",
            "llm",
            {"model": "m1", "prompt_version": "v1", "fallback_used": True},
            [("retry", {}), ("circuit_open", {})],
        ),
        sp(
            "guard.check",
            "guard",
            {
                "outcome": "policy_block",
                "reason_code": "pii_column",
                "error_type": "guard_policy_block",
            },
        ),
        sp("node.guard", "node", {"error_type": "guard_policy_block"}),
        sp("guard.mask", "guard", {"masker_hit": True}),
        sp("guard.scan", "guard", {"hits_total": 2}),
        sp("bq.query", "sql", {"bytes_billed": 10}),
        sp("guard.empty_check", "guard", {"diagnosis": "unknown"}),
        sp("tool.delete_reports", "tool"),
        sp("turn", "turn"),
    ]
    facts = TurnFacts(
        "Not possible.",
        "analysis",
        "error",
        "guard_policy_block",
        {"llm_calls": 2, "fallback_calls": 1, "seconds": 1.5},
    )
    m = turn_metrics(facts, spans, "maya")
    assert m["outcome"] == "blocked" and m["guard_block_reason"] == "pii_column"
    assert m["errors"] == {"guard_policy_block": 1}  # the node span repeats it
    assert m["masker_hits"] == 1 and m["scanner_hits"] == 2
    assert m["retries"] == 1 and m["circuit_open"] == 1 and m["fallback_used"] == 1
    assert m["model"] == "m1" and m["prompt_version"] == "v1"
    assert m["bq_query_bytes"] == [10] and m["empty_result_kind"] == "unknown"
    assert m["tools_used"] == {"delete_reports": 1, "guard": 1, "write_sql": 1}
    assert m["duration_ms"] == 5.0


def test_merge_adds_the_counts_of_both_parts():
    old = {
        "llm_calls": 1,
        "duration_ms": 10,
        "errors": '{"a": 1}',
        "tools_used": '{"route": 1}',
        "bq_query_bytes": "[5]",
        "kind": "delete",
        "retried": 0,
        "fallback_used": 0,
    }
    new = {
        "llm_calls": 0,
        "duration_ms": 3,
        "errors": {"a": 1},
        "tools_used": {"apply_delete": 1},
        "bq_query_bytes": [],
        "kind": "other",
        "retried": 0,
        "fallback_used": 0,
    }
    merged = merge_metrics(old, new)
    assert merged["llm_calls"] == 1 and merged["duration_ms"] == 13
    assert merged["errors"] == {"a": 2} and merged["tools_used"] == {
        "apply_delete": 1,
        "route": 1,
    }
    assert merged["bq_query_bytes"] == [5] and merged["kind"] == "delete"


# --- B4 10-12: statistics -------------------------------------------------------------------------
def insert_turn(conn, i: int, **values: Any) -> None:
    row: dict[str, Any] = {
        "conversation_id": values.pop("conversation_id", "C-AAAAA"),
        "turn_no": i,
        "trace_id": f"{i:08x}" + "0" * 24,
        "started_at": (NOW - timedelta(days=1) + timedelta(minutes=i)).isoformat(),
        "user_text": "secret question text",
        "answer_text": "secret answer text",
        "user_id": "maya",
        "status": "ok",
        "outcome": "answered",
        "errors": "{}",
        "tools_used": "{}",
        "bq_query_bytes": "[]",
    }
    for key, value in values.items():
        row[key] = json.dumps(value) if isinstance(value, dict | list) else value
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO conversation_turns ({columns}) VALUES ({marks})",
        tuple(row.values()),
    )
    conn.commit()


def fixture_40(conn) -> None:
    """40 turns of maya in the last day, plus rows that must not count."""
    special: dict[int, dict[str, Any]] = {
        3: {"retried": 1, "attempts_total": 2, "errors": {"guard_parse_error": 1}},
        5: {"outcome": "true_zero", "empty_result_kind": "true_zero"},
        6: {
            "outcome": "failed",
            "error_type": "bq_timeout",
            "errors": {"bq_timeout": 2},
            "retried": 1,
            "attempts_total": 3,
            "retries": 1,
            "circuit_open": 1,
        },
        7: {
            "outcome": "blocked",
            "error_type": "guard_policy_block",
            "guard_block_reason": "pii_column",
            "errors": {"guard_policy_block": 1},
            "bq_queries": 0,
            "bq_bytes_billed": 0,
            "bq_query_bytes": [],
        },
        8: {
            "outcome": "limit",
            "error_type": "limit_exceeded",
            "errors": {"limit_exceeded": 1},
            "bq_queries": 0,
            "bq_bytes_billed": 0,
            "bq_query_bytes": [],
        },
        9: {"empty_result_kind": "unknown"},
        10: {
            "retried": 1,
            "attempts_total": 3,
            "errors": {"bq_syntax": 1, "guard_parse_error": 1},
        },
        11: {"retries": 2},
        12: {"masker_hits": 1},
        13: {"scanner_hits": 2},
        14: {"fallback_calls": 1, "fallback_used": 1},
    }
    for i in range(1, 41):
        base: dict[str, Any] = {
            "duration_ms": i * 1000,
            "answer_words": i,
            "tokens_in": 100 * i,
            "tokens_out": 10 * i,
        }
        if i <= 25:
            base.update(
                kind="question",
                intent="analysis",
                llm_calls=3,
                attempts_total=1,
                bq_queries=1,
                bq_bytes_billed=i * 1_000_000,
                bq_query_bytes=[i * 1_000_000],
                tools_used={"route": 1, "write_sql": 1},
            )
        elif i <= 30:
            base.update(
                kind="report",
                intent="report",
                llm_calls=4,
                bq_queries=2,
                bq_bytes_billed=2 * i * 1_000_000,
                bq_query_bytes=[i * 1_000_000] * 2,
                tools_used={"route": 1, "plan_report": 1},
            )
        elif i <= 34:
            confirm = {
                31: ("confirmed", "confirmed"),
                32: ("cancelled", "not_exact"),
                33: ("confirmation_shown", "shown"),
                34: ("cancelled", "other_text"),
            }[i]
            base.update(
                kind="delete",
                intent="delete",
                llm_calls=1,
                outcome=confirm[0],
                confirmation_outcome=confirm[1],
                tools_used={"route": 1},
            )
        else:
            base.update(
                kind="other",
                intent="chitchat" if i <= 38 else "off_topic",
                llm_calls=1,
                tools_used={"route": 1},
                outcome="answered" if i <= 38 else "refused",
            )
        base.update(special.get(i, {}))
        insert_turn(conn, i, **base)
    insert_turn(
        conn, 41, user_id="alex", conversation_id="C-BBBBB", duration_ms=999_999
    )
    insert_turn(
        conn, 42, started_at=(NOW - timedelta(days=8)).isoformat(), duration_ms=888_888
    )
    insert_turn(conn, 43, outcome=None, duration_ms=777_777)  # written before spec 35


def test_10_each_figure_of_the_fixture(conn):
    fixture_40(conn)
    s = compute_stats(conn, NOW, 7, "maya")
    assert s.turns == 40
    assert s.by_kind == {"question": 25, "report": 5, "delete": 4, "other": 6}
    assert s.by_outcome == {
        "answered": 30,
        "true_zero": 1,
        "failed": 1,
        "blocked": 1,
        "limit": 1,
        "confirmed": 1,
        "cancelled": 2,
        "confirmation_shown": 1,
        "refused": 2,
    }
    assert s.time_questions == Spread(25, 13000, 24000)
    assert s.time_reports == Spread(5, 28000, None)
    assert s.answer_words == Spread(31, 19, 37)
    assert s.tokens_in == Spread(40, 2000, 3800) and s.tokens_out == Spread(
        40, 200, 380
    )
    assert (s.tokens_in_total, s.tokens_out_total) == (82000, 8200)
    assert s.llm_calls_total == 105 and s.avg_llm_calls == pytest.approx(2.625)
    assert s.routes == {
        "analysis": 25,
        "report": 5,
        "delete": 4,
        "chitchat": 4,
        "off_topic": 2,
    }
    assert s.tools == {"route": 40, "write_sql": 25, "plan_report": 5}
    assert s.final_errors == {
        "bq_timeout": 1,
        "guard_policy_block": 1,
        "limit_exceeded": 1,
    }
    assert s.all_errors == {
        "guard_parse_error": 2,
        "bq_timeout": 2,
        "guard_policy_block": 1,
        "limit_exceeded": 1,
        "bq_syntax": 1,
    }
    assert s.resolved_errors == {"guard_parse_error": 2, "bq_syntax": 1}
    assert (s.retried_turns, s.retried_good) == (3, 2)
    assert s.retried_success == pytest.approx(
        2 / 3
    ) and s.avg_attempts == pytest.approx(8 / 3)
    assert s.guard_blocks == {"pii_column": 1} and (s.masker_hits, s.scanner_hits) == (
        1,
        2,
    )
    assert s.empty_results == {"true_zero": 1, "unknown": 1}
    assert (s.confirmations_shown, s.confirmations_confirmed) == (4, 1)
    assert s.confirmations_cancelled == {"not_exact": 1, "other_text": 1}
    assert s.bq_queries_total == 33 and s.bytes_total == 590_000_000
    assert s.query_bytes == Spread(33, 19_000_000, 30_000_000)
    assert [t[0] for t in s.top_bytes] == [f"{i:08x}" for i in (30, 29, 28, 27, 26)]
    assert [t[2] for t in s.top_bytes] == [
        60_000_000,
        58_000_000,
        56_000_000,
        54_000_000,
        52_000_000,
    ]
    assert (s.retries, s.fallback_calls, s.fallback_turns, s.circuit_opens) == (
        3,
        1,
        1,
        1,
    )
    text = stats_text(s)
    assert "median 13.0 s · p95 24.0 s (nearest rank, n = 25)" in text
    assert "Reports: median 28.0 s · p95: not enough data (n = 5)" in text
    assert "Success: 67%" in text and "Top turns: 0000001e 60.0 MB" in text


def test_10_all_users_includes_alex(conn):
    fixture_40(conn)
    assert compute_stats(conn, NOW, 7, None).turns == 41
    assert compute_stats(conn, NOW, 30, "maya").turns == 41  # the 8-day-old row


def test_11_fewer_than_20_turns_no_p95(conn):
    for i in range(1, 11):
        insert_turn(conn, i, kind="question", intent="analysis", duration_ms=i * 100)
    s = compute_stats(conn, NOW, 7, "maya")
    assert s.time_questions.p95 is None and s.time_questions.median == 500
    assert "p95: not enough data (n = 10)" in stats_text(s)


def test_nearest_rank():
    assert nearest_rank([], 50) is None
    assert nearest_rank([5], 95) == 5
    assert nearest_rank(list(range(1, 101)), 95) == 95
    assert nearest_rank([1, 2, 3, 4], 50) == 2


def test_12_stats_as_a_manager_shows_own_turns_only(make_session, conn):
    session, llm, bq = make_session("maya")
    session.services.clock = lambda: NOW
    fixture_40(conn)
    outcome = session.command("stats", [])
    assert outcome.data.scope == "maya" and outcome.data.turns == 40
    assert llm.calls == [] and bq.queries == []


# --- B4 13-14: /trace ------------------------------------------------------------------------------
def write_trace_fixture(directory: Path) -> str:
    trace = "4f2a9c1e" + "0" * 24
    spans = [
        {
            "trace_id": trace,
            "span_id": "a1",
            "parent_span_id": None,
            "name": "turn",
            "kind": "turn",
            "start": "2026-11-12T10:00:00.000Z",
            "duration_ms": 4210.0,
            "status": "ok",
            "attributes": {"user_id": "maya", "intent": "analysis", "turn_no": 1},
        },
        {
            "trace_id": trace,
            "span_id": "b1",
            "parent_span_id": "a1",
            "name": "node.route",
            "kind": "node",
            "start": "2026-11-12T10:00:00.100Z",
            "duration_ms": 812.0,
            "status": "ok",
            "attributes": {"intent": "analysis"},
        },
        {
            "trace_id": trace,
            "span_id": "c1",
            "parent_span_id": "b1",
            "name": "llm.route",
            "kind": "llm",
            "start": "2026-11-12T10:00:00.110Z",
            "duration_ms": 800.0,
            "status": "ok",
            "attributes": {
                "model": "gemini-3.5-flash-lite",
                "tokens_in": 1830,
                "tokens_out": 210,
            },
            "events": [
                {
                    "name": "retry",
                    "attributes": {"attempt": 2, "error_type": "llm_timeout"},
                }
            ],
        },
        {
            "trace_id": trace,
            "span_id": "b2",
            "parent_span_id": "a1",
            "name": "node.guard",
            "kind": "node",
            "start": "2026-11-12T10:00:01.000Z",
            "duration_ms": 12.0,
            "status": "ok",
            "attributes": {},
        },
        {
            "trace_id": trace,
            "span_id": "c2",
            "parent_span_id": "b2",
            "name": "guard.check",
            "kind": "guard",
            "start": "2026-11-12T10:00:01.001Z",
            "duration_ms": 10.0,
            "status": "ok",
            "attributes": {
                "outcome": "policy_block",
                "reason_code": "pii_column",
                "error_type": "guard_policy_block",
                "sql_masked": "SELECT email FROM users WHERE email = '<s:16>'",
            },
        },
        {
            "trace_id": trace,
            "span_id": "b3",
            "parent_span_id": "a1",
            "name": "bq.query",
            "kind": "sql",
            "start": "2026-11-12T10:00:02.000Z",
            "duration_ms": 2100.0,
            "status": "error",
            "attributes": {
                "sql_masked": "SELECT 1 WHERE email = '<s:16>'",
                "error_type": "bq_timeout",
            },
        },
    ]
    other = dict(spans[0], trace_id="9" * 32, attributes={"user_id": "alex"})
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-11-12.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in [*spans, other])
    )
    (directory / "2026-11-11.jsonl").write_text(
        json.dumps(dict(spans[0], trace_id="1" * 32)) + "\n"
    )
    return trace


EXPECTED_TREE = """Trace 4f2a9c1e (6 steps)
  turn · 4.2 s · intent=analysis · turn_no=1
    node.route · 812 ms
      llm.route · 800 ms · model=gemini-3.5-flash-lite · tokens_in=1830 · tokens_out=210
          event retry: attempt=2, error_type=llm_timeout
    node.guard · 12 ms
      guard.check · 10 ms · outcome=policy_block · reason_code=pii_column · ERROR guard_policy_block
          SQL: SELECT email FROM users WHERE email = '<s:16>'
    bq.query · 2.1 s · ERROR bq_timeout"""


def test_14_trace_view_of_a_fixture(tmp_path):
    trace = write_trace_fixture(tmp_path)
    tree = load_trace(tmp_path, trace[:8], "maya")
    assert tree is not None and tree.span_count == 6
    assert trace_text(tree) == EXPECTED_TREE


def test_13_trace_of_another_user_is_not_found(tmp_path):
    write_trace_fixture(tmp_path)
    assert load_trace(tmp_path, "9" * 8, "maya") is None
    assert load_trace(tmp_path, "4f2a9c1e", "alex") is None
    assert load_trace(tmp_path, "zz", "maya") is None
    assert load_trace(tmp_path, "11111111", "maya") is not None  # an older file


def test_13_trace_command_of_another_user(make_session, config):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "1,200 orders."])
    maya, _, _ = make_session("maya", llm=llm, bq=FakeBQ([orders()]))
    maya.send("How many orders?")
    alex, alex_llm, _ = make_session("alex")
    assert alex.command("trace", [maya.last_trace_id[:8]]).text == "Trace not found."
    own = maya.command("trace", [])
    assert own.kind == "message" and own.data.root.name == "turn"
    assert alex_llm.calls == []


def test_trace_view_is_fast_on_a_large_file(tmp_path):
    import time

    trace = write_trace_fixture(tmp_path)
    filler = (
        json.dumps(
            {
                "trace_id": "a" * 32,
                "span_id": "x",
                "name": "node.x",
                "kind": "node",
                "attributes": {"note": "x" * 200},
            }
        )
        + "\n"
    )
    with (tmp_path / "2026-11-10.jsonl").open("w") as fh:
        fh.write(filler * 100_000)  # about 30 MB
    started = time.monotonic()
    assert load_trace(tmp_path, trace[:8], "maya") is not None
    assert load_trace(tmp_path, "1" * 8, "maya") is not None  # reads the large file too
    assert time.monotonic() - started < 1.0


# --- B4 15: export ---------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_15_export_has_figures_only(conn, tmp_path, fmt):
    fixture_40(conn)
    path, count = stats_module.export(conn, NOW, 7, None, fmt, tmp_path)
    assert path.name == f"stats-2026-11-12.{fmt}" and count == 41
    if fmt == "csv":
        with path.open() as fh:
            reader = csv.DictReader(fh)
            assert tuple(reader.fieldnames or ()) == EXPORT_COLUMNS
            rows = list(reader)
    else:
        rows = json.loads(path.read_text())
        assert isinstance(rows[0]["errors"], dict)
    assert len(rows) == 41
    text = path.read_text()
    assert "user_text" not in text and "answer_text" not in text
    assert "secret question text" not in text and "secret answer text" not in text


# --- B4 16: retention -------------------------------------------------------------------------------
def test_16_prune_with_a_fake_clock(config, conn):
    settings = config.settings
    traces, logs = settings.data_path("traces"), settings.data_path("logs")
    for folder in (traces, logs):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "2026-10-12.jsonl").write_text("{}\n")  # 31 days old
        (folder / "2026-10-14.jsonl").write_text("{}\n")  # 29 days old
    old_log = logs / "agent.log"
    old_log.write_text("old\n")
    stamp = (NOW - timedelta(days=40)).timestamp()
    os.utime(old_log, (stamp, stamp))
    insert_turn(conn, 1, started_at=(NOW - timedelta(days=91)).isoformat())
    insert_turn(conn, 2, started_at=(NOW - timedelta(days=89)).isoformat())
    insert_turn(conn, 3, started_at=(NOW - timedelta(days=366)).isoformat())
    for ref, expires in (
        ("res_old", NOW - timedelta(seconds=1)),
        ("res_new", NOW + timedelta(days=1)),
    ):
        conn.execute(
            "INSERT INTO result_cache (ref, conversation_id, turn_no, owner_id, columns, rows, created_at, expires_at) "
            "VALUES (?, 'C-AAAAA', 1, 'maya', '[]', '[]', ?, ?)",
            (ref, NOW.isoformat(), expires.isoformat()),
        )
    conn.commit()
    result = prune(conn, settings, NOW)
    assert (result.trace_files, result.log_files) == (1, 2)
    assert sorted(p.name for p in traces.iterdir()) == ["2026-10-14.jsonl"]
    rows = {r["turn_no"]: r for r in turn_rows(conn)}
    assert set(rows) == {1, 2}
    assert rows[1]["user_text"] is None and rows[1]["answer_text"] is None
    assert rows[1]["outcome"] == "answered"  # the figures stay
    assert rows[2]["user_text"] == "secret question text"
    refs = [r[0] for r in conn.execute("SELECT ref FROM result_cache")]
    assert refs == ["res_new"]
    assert (result.texts_cleared, result.turns_deleted, result.cache_rows) == (1, 1, 1)


def test_16_resume_view_after_the_texts_are_gone(make_session, conn):
    from opsfleet_agent.memory.conversations import last_turns

    insert_turn(conn, 1, user_text=None, answer_text=None)
    [turn] = last_turns(conn, "C-AAAAA")
    assert turn.user_text == "" and turn.answer_text == ""


# --- B4 17: no other exporter ---------------------------------------------------------------------
def test_17_only_the_local_exporter(config, caplog):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from opsfleet_agent.observability.progress import PROGRESS
    from opsfleet_agent.observability.turn_metrics import TURN_SPANS

    assert config.settings.tracing.otlp_endpoint is None
    provider = tracing.init_tracing(config.settings.data_path("traces"))
    processors = provider._active_span_processor._span_processors
    exporters = [
        p.span_exporter for p in processors if isinstance(p, SimpleSpanProcessor)
    ]
    assert [type(e).__name__ for e in exporters] == ["JsonlSpanExporter"]
    assert set(processors) - {
        *(p for p in processors if isinstance(p, SimpleSpanProcessor))
    } == {TURN_SPANS, PROGRESS}


def test_17_a_set_endpoint_only_warns(config, monkeypatch):
    monkeypatch.setattr(tracing, "_provider", None)
    from opsfleet_agent.resilience.safe_node import init_logging

    init_logging(config.settings.data_path("logs"))
    provider = tracing.init_tracing(
        config.settings.data_path("traces"), otlp_endpoint="http://localhost:4318"
    )
    exporters = [
        p
        for p in provider._active_span_processor._span_processors
        if hasattr(p, "span_exporter")
    ]
    assert len(exporters) == 1
    assert any("no OTLP exporter" in line["message"] for line in log_lines(config))


# --- B4 18: no PII in any trace or log file -------------------------------------------------------
PII_VALUES = (
    "anna@example.com",
    "555-123-4567",
    "(555) 123-4567",
    "1600 Main Street",
    "94107",
)


def test_18_no_pii_in_traces_and_logs_after_a_session(make_session, config):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(
                f"SELECT COUNT(*) AS n FROM {T_USERS} WHERE email = 'anna@example.com' AND postal_code = 94107"
            ),
            route_reply(),
            sql_reply(ORDERS_SQL),
            "There were 1,200 orders. Ask anna@example.com or call (555) 123-4567.",
            route_reply(),
            RuntimeError("bug with 1600 Main Street"),
        ]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders()]))
    session.send("Count users with email anna@example.com")
    session.send("How many orders? I live at 1600 Main Street, call 555-123-4567")
    session.send("And now?")
    files = [
        *config.settings.data_path("traces").glob("*.jsonl"),
        *config.settings.data_path("logs").glob("*.jsonl"),
    ]
    assert len(files) >= 2
    text = "\n".join(f.read_text() for f in files)
    for value in PII_VALUES:
        assert value not in text, value
    for line in text.splitlines():
        for name, pattern, accept in PATTERNS:
            for match in pattern.finditer(line):
                assert not accept(match), (name, match.group(0))
    assert "'<s:16>'" in text  # the blocked query is in the trace, masked


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE email = 'anna@example.com",  # the quote never closes
        "SELECT 1 -- ask anna@example.com\nFROM t",
        "SELEC x /* anna@example.com */ 'ab'",
    ],
)
def test_unparsed_sql_with_open_quotes_or_comments_is_masked(sql):
    masked = Redactor().value("sql_masked", sql)
    assert "anna" not in masked and "example.com" not in masked
