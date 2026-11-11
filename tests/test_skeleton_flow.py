"""The walking skeleton from question to answer, with a fake Gemini and a fake BigQuery (spec 01)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pandas as pd
import pytest
from google.api_core import exceptions as gexc
from langgraph.checkpoint.memory import InMemorySaver

from opsfleet_agent.agent.graph import load_prompts
from opsfleet_agent.agent.state import Services
from opsfleet_agent.bq import classify_bq_error
from opsfleet_agent.config import AppConfig
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.llm import classify_llm_error
from opsfleet_agent.session import AssistantSession
from tests.conftest import (
    FakeBQ,
    FakeLLM,
    frame_result,
    load_test_schema,
    load_test_values,
)


@pytest.fixture
def make_session(
    config: AppConfig, conn
) -> Callable[..., tuple[AssistantSession, FakeLLM, FakeBQ]]:
    def _make(
        user_id: str = "maya", llm: FakeLLM | None = None, bq: FakeBQ | None = None
    ):
        llm = llm or FakeLLM()
        bq = bq or FakeBQ()
        services = Services(
            settings=config.settings,
            llm=llm,
            bq=bq,
            schema=load_test_schema(),
            values=load_test_values(),
            prompts=load_prompts(),
        )
        session = AssistantSession(
            config,
            resolve_user(config.users, user_id),
            services,
            conn=conn,
            checkpointer=InMemorySaver(),
        )
        return session, llm, bq

    return _make


SQL = "SELECT COUNT(*) AS orders FROM `bigquery-public-data.thelook_ecommerce.orders`"


def spans(config):
    out = []
    for f in config.settings.data_path("traces").glob("*.jsonl"):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_simple_question_gives_an_answer_and_a_trace(make_session, config):
    llm = FakeLLM(
        [
            {"sql": SQL, "explanation": "Counts all orders.", "reply": None},
            "There were 41,000 orders.",
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [41000]}))])
    session, _, _ = make_session(llm=llm, bq=bq)
    result = session.ask("How many orders were placed in 2024?")
    assert result.answer == "There were 41,000 orders."
    assert result.error_type is None
    assert result.totals["llm_calls"] == 2 and result.totals["bq_queries"] == 1
    assert (
        "| orders |" in llm.calls[1]["prompt"]
    )  # the answer prompt got the result table

    records = spans(config)
    turn = next(r for r in records if r["kind"] == "turn")
    assert turn["trace_id"] == result.trace_id
    names = {r["name"] for r in records if r["trace_id"] == result.trace_id}
    assert {
        "turn",
        "node.write_sql",
        "node.guard",
        "guard.check",
        "node.run_query",
        "node.write_answer",
    } <= names


def test_write_request_is_blocked_before_bigquery(make_session):
    llm = FakeLLM(
        [
            {
                "sql": "DROP TABLE `bigquery-public-data.thelook_ecommerce.orders`",
                "explanation": "x",
                "reply": None,
            }
        ]
    )
    session, _, bq = make_session(llm=llm)
    result = session.ask("Delete the orders table")
    assert "only read" in result.answer
    assert result.error_type == "guard_policy_block"
    assert bq.queries == []


def test_model_refusal_needs_no_query(make_session):
    llm = FakeLLM(
        [
            {
                "sql": None,
                "explanation": "",
                "reply": "I can only answer questions about the sales data.",
            }
        ]
    )
    session, _, bq = make_session(llm=llm)
    result = session.ask("Write me a poem")
    assert "sales data" in result.answer and bq.queries == []


def test_bigquery_error_gives_one_sentence_and_chat_continues(make_session):
    llm = FakeLLM(
        [
            {"sql": SQL, "explanation": "x", "reply": None},
            {"sql": SQL, "explanation": "x", "reply": None},
            "Fine now.",
        ]
    )
    bq = FakeBQ(
        [
            classify_bq_error(gexc.BadRequest("Unrecognized name: x")),
            frame_result(pd.DataFrame({"orders": [1]})),
        ]
    )
    session, _, _ = make_session(llm=llm, bq=bq)
    first = session.ask("Question one")
    assert first.error_type == "bq_syntax" and first.answer == "The query has an error."
    second = session.ask("Question two")
    assert second.answer == "Fine now."


def test_gemini_timeout_gives_one_sentence(make_session):
    llm = FakeLLM([classify_llm_error(httpx.ReadTimeout("slow"))])
    session, _, _ = make_session(llm=llm)
    result = session.ask("How many orders?")
    assert result.error_type == "llm_timeout"
    assert result.answer == "The language model did not answer in time."


def test_empty_result_is_reported(make_session):
    llm = FakeLLM(
        [{"sql": SQL, "explanation": "x", "reply": None}, "No data for this question."]
    )
    probe = frame_result(pd.DataFrame({"n": [3]}))  # check 3: maya's brands have items
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": []})), probe])
    session, _, _ = make_session(llm=llm, bq=bq)
    result = session.ask("Orders in 1990?")
    assert result.error_type == "empty_result"
    assert result.answer == "No data for this question."
    assert "The data covers" in llm.calls[1]["prompt"]


def test_unexpected_bug_does_not_stop_the_chat(make_session):
    llm = FakeLLM([ValueError("bug")])
    session, _, _ = make_session(llm=llm)
    result = session.ask("How many orders?")
    assert result.error_type == "internal_error"


def test_follow_up_question_gets_the_history(make_session):
    llm = FakeLLM(
        [
            {"sql": SQL, "explanation": "x", "reply": None},
            "41,000 orders.",
            {"sql": SQL, "explanation": "x", "reply": None},
            "In 2023: 30,000.",
        ]
    )
    bq = FakeBQ(
        [
            frame_result(pd.DataFrame({"orders": [41000]})),
            frame_result(pd.DataFrame({"orders": [30000]})),
        ]
    )
    session, _, _ = make_session(llm=llm, bq=bq)
    session.ask("How many orders in 2024?")
    session.ask("And in 2023?")
    second_sql_prompt = llm.calls[2]["prompt"]
    assert (
        "How many orders in 2024?" in second_sql_prompt
        and "41,000 orders." in second_sql_prompt
    )


def test_daily_limit_stops_the_turn_with_its_message(make_session, ledger, config):
    ledger.add(llm_calls=config.settings.limits.per_day.max_llm_calls)
    session, llm, _ = make_session(llm=FakeLLM([]))
    result = session.ask("How many orders?")
    assert result.error_type == "limit_exceeded"
    assert "daily limit of model calls" in result.answer
    assert llm.calls == []


# --- spec 30 in the flow -----------------------------------------------------------------------

T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"


def test_manager_query_runs_scoped_with_the_brand_parameter(make_session, config):
    llm = FakeLLM([{"sql": SQL, "explanation": "x", "reply": None}, "1,200 orders."])
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    session, _, _ = make_session("maya", llm=llm, bq=bq)
    session.ask("How many orders?")
    assert "IN UNNEST(@scope_brands)" in bq.queries[0]
    assert bq.params[0][0].values == ["Calvin Klein", "Carhartt"]
    assert "Calvin Klein, Carhartt" in llm.calls[0]["prompt"]  # the scope line
    assert "Calvin Klein, Carhartt" in llm.calls[1]["prompt"]
    guard_spans = [r for r in spans(config) if r["name"] == "guard.check"]
    assert guard_spans[0]["attributes"]["rewritten"] is True


def test_ceo_query_runs_without_parameters(make_session):
    llm = FakeLLM([{"sql": SQL, "explanation": "x", "reply": None}, "41,000 orders."])
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [41000]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    session.ask("How many orders?")
    assert bq.params == [[]] and "scope_brands" not in bq.queries[0]


def test_model_schema_has_no_pii_and_no_num_of_item_for_a_manager(make_session):
    llm = FakeLLM([{"sql": None, "explanation": "", "reply": "OK"}])
    session, _, _ = make_session("maya", llm=llm)
    session.ask("What data do you have?")
    schema = "\n".join(
        line
        for line in llm.calls[0]["prompt"].splitlines()
        if "thelook_ecommerce." in line
    )
    assert schema.count("thelook_ecommerce.") == 4
    for hidden in ("email", "first_name", "latitude", "user_geom", "num_of_item"):
        assert hidden not in schema
    assert "traffic_source" in schema


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            "SELECT u.id, u.email FROM `bigquery-public-data.thelook_ecommerce.users` u",
            "I cannot show or use personal data",
        ),
        (
            (
                f"SELECT SUM(oi.sale_price) FROM {T_ITEMS} oi JOIN "
                "`bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id "
                "WHERE p.brand = 'Nike'"
            ),
            "Nike is outside your brands. Your brands are Calvin Klein and Carhartt.",
        ),
        (
            (
                "SELECT u.id FROM `bigquery-public-data.thelook_ecommerce.users` u "
                "WHERE u.state = 'California'"
            ),
            "I cannot show a single customer together with",
        ),
    ],
)
def test_policy_block_ends_the_turn_without_retry_and_query(make_session, sql, message):
    llm = FakeLLM([{"sql": sql, "explanation": "x", "reply": None}])
    session, _, bq = make_session("maya", llm=llm)
    result = session.ask("Show me something forbidden")
    assert result.error_type == "guard_policy_block"
    assert message in result.answer
    assert bq.queries == [] and len(llm.calls) == 1


def test_parse_error_keeps_the_feedback_for_spec_31(make_session):
    sql = "SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`"
    llm = FakeLLM([{"sql": sql, "explanation": "x", "reply": None}])
    session, _, bq = make_session("ceo", llm=llm)
    result = session.ask("Show all orders")
    assert result.error_type == "guard_parse_error" and bq.queries == []
    state = session.graph.get_state(
        {"configurable": {"thread_id": session.conversation_id}}
    )
    assert "List the columns by name" in state.values["turn"]["model_feedback"]


def test_rows_are_masked_before_the_answer_prompt(make_session):
    sql = (
        "SELECT p.name, COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.products` p "
        "GROUP BY p.name"
    )
    frame = pd.DataFrame({"name": ["Jeans ann@x.com", "Shirt"], "n": [3, 4]})
    llm = FakeLLM([{"sql": sql, "explanation": "x", "reply": None}, "Two products."])
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([frame_result(frame)]))
    session.ask("Products?")
    prompt = llm.calls[1]["prompt"]
    assert "ann@x.com" not in prompt and "[masked]" in prompt
    assert '<data source="query" ref="r1">' in prompt


def test_answer_is_scanned_before_the_user_sees_it(make_session):
    llm = FakeLLM(
        [
            {"sql": SQL, "explanation": "x", "reply": None},
            "Top customer anna@example.com spent $1,200.50.",
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("Top customer?")
    assert "anna@example.com" not in result.answer
    assert "[removed]" in result.answer and "$1,200.50" in result.answer
    assert "looked like personal data" in result.answer


def test_model_reply_without_sql_is_also_scanned(make_session):
    llm = FakeLLM([{"sql": None, "explanation": "", "reply": "Mail me at a@b.com"}])
    session, _, _ = make_session("ceo", llm=llm)
    assert "a@b.com" not in session.ask("hi").answer


def test_empty_result_with_a_wrong_value_tells_the_error_and_needs_no_answer_call(
    make_session,
):
    sql = (
        "SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.orders` "
        "WHERE status = 'completed'"
    )
    llm = FakeLLM([{"sql": sql, "explanation": "x", "reply": None}])
    bq = FakeBQ([frame_result(pd.DataFrame({"n": []}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("Completed orders?")
    assert result.error_type == "empty_result" and len(llm.calls) == 1
    assert "'completed' does not exist" in result.answer
    state = session.graph.get_state(
        {"configurable": {"thread_id": session.conversation_id}}
    )
    assert "Complete" in state.values["turn"]["model_feedback"]


def test_wrong_brand_of_the_user_stops_the_start(config, test_values):
    from opsfleet_agent.errors import ConfigError
    from opsfleet_agent.session import check_user_brands

    users = config.users.model_copy(
        update={
            "users": [
                *config.users.users,
                config.users.users[2].model_copy(
                    update={"id": "zoe", "brands": ["Levis"]}
                ),
            ]
        }
    )
    bad = config.model_copy(update={"users": users})
    with pytest.raises(ConfigError, match="Close names: Levi's"):
        check_user_brands(bad, test_values, "zoe")
    warnings = check_user_brands(bad, test_values, "maya")
    assert warnings == [
        "Brand 'Levis' for user 'zoe' does not exist in the data. Close names: Levi's."
    ]
