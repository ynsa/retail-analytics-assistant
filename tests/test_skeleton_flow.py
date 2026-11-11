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
from tests.conftest import FakeBQ, FakeLLM, frame_result


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
            schema_text="`bigquery-public-data.thelook_ecommerce.orders`(order_id INTEGER)",
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
    llm = FakeLLM([{"sql": SQL, "explanation": "x", "reply": None}])
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": []}))])
    session, _, _ = make_session(llm=llm, bq=bq)
    result = session.ask("Orders in 1990?")
    assert result.error_type == "empty_result"


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
