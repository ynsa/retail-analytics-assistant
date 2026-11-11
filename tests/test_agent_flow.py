"""The agent core with a fake Gemini and a fake BigQuery (spec 31, B4 cases 1-16).

The fake model returns the queued replies in order: first the route JSON, then the write_sql JSON
of each attempt, then the answer text. It replaces the walking-skeleton tests of spec 01. The
behavior of spec 30 in the flow (scope, masker, scanner) is tested here too.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest
from google.api_core import exceptions as gexc

from opsfleet_agent.bq import classify_bq_error
from opsfleet_agent.errors import AgentError, ErrorType, LimitExceeded
from opsfleet_agent.llm import SCHEMA_MISMATCH, classify_llm_error
from tests.conftest import (
    FakeBQ,
    FakeLLM,
    frame_result,
    read_spans,
    route_reply,
    sql_reply,
)

T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
T_PRODUCTS = "`bigquery-public-data.thelook_ecommerce.products`"
ORDERS_SQL = (
    f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {T_ITEMS} oi "
    "WHERE oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01')"
)
REVENUE_SQL = (
    f"SELECT SUM(oi.sale_price) AS revenue FROM {T_ITEMS} oi "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned')"
)


def orders(n: int = 1200):
    return frame_result(pd.DataFrame({"orders": [n]}))


def state_of(session):
    return session.graph.get_state(
        {"configurable": {"thread_id": session.conversation_id}}
    )


# --- B4 case 1: each route ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (route_reply("chitchat", chitchat_kind="greeting"), "Hello, Maya."),
        (route_reply("chitchat", chitchat_kind="thanks"), "You are welcome."),
        (
            route_reply("off_topic"),
            "I can only help with the analysis of the company's sales data",
        ),
        (
            route_reply("unsafe", unsafe_kind="prompt_request"),
            "I cannot share my instructions or my configuration.",
        ),
        (
            route_reply("unsafe", unsafe_kind="identity_change"),
            "A message cannot change them. You are Maya, brand manager.",
        ),
        (route_reply("unsafe", unsafe_kind="rule_change"), "My rules stay the same"),
        (route_reply("report"), "This part is not built yet."),
        (route_reply("delete"), "This part is not built yet."),
    ],
)
def test_prepared_routes_cost_exactly_one_model_call(make_session, route, expected):
    session, llm, bq = make_session(llm=FakeLLM([route]))
    result = session.ask("some message")
    assert expected in result.answer
    assert result.totals["llm_calls"] == 1 and len(llm.calls) == 1
    assert bq.queries == [] and result.error_type is None
    assert result.intent == route["intent"]


def test_clarify_route_asks_one_question_with_options(make_session):
    route = route_reply(
        "clarify",
        clarify_question="Which two brands do you want to compare?",
        clarify_options=[
            "Calvin Klein and Carhartt",
            "Levi's and Nike",
            "The top 2 by revenue",
        ],
    )
    session, llm, _ = make_session("ceo", llm=FakeLLM([route]))
    result = session.ask("Compare our two brands")
    assert result.intent == "clarify" and len(llm.calls) == 1
    assert result.answer.startswith("Which two brands do you want to compare?")
    assert (
        "\n\n1. Calvin Klein and Carhartt\n2. Levi's and Nike\n3. The top 2 by revenue"
        in (result.answer)
    )


def test_schema_data_question_uses_one_model_call_and_no_query(make_session):
    llm = FakeLLM([route_reply("schema", schema_kind="data"), "Four tables: orders, …"])
    session, _, bq = make_session("maya", llm=llm)
    result = session.ask("What data do you have?")
    assert result.answer == "Four tables: orders, …"
    assert len(llm.calls) == 2 and bq.queries == []
    prompt = llm.calls[1]["prompt"]
    assert "Names, email addresses and street addresses are not available" in prompt
    fields = prompt.split("<fields>")[1].split("</fields>")[0]
    assert "bigquery-public-data" not in prompt and "email" not in fields
    assert "orders: order_id, user_id, status" in fields
    assert "Customer statistics cover only the customers" in llm.calls[1]["system"]


def test_format_only_message_is_pinned_for_the_next_answers(make_session):
    llm = FakeLLM(
        [
            route_reply(
                "chitchat", chitchat_kind="format_only", format_request="table"
            ),
            route_reply(),
            sql_reply(ORDERS_SQL),
            "| orders |\n|---|\n| 1,200 |",
        ]
    )
    session, _, _ = make_session(llm=llm, bq=FakeBQ([orders()]))
    first = session.ask("Answer with tables from now on")
    assert first.answer == "OK. I will use this format for the next answers."
    session.ask("How many orders last month?")
    assert "## User format\n\nShow figures in Markdown tables" in llm.calls[3]["system"]
    assert state_of(session).values["pinned"]["format"] == "table"


# --- B4 cases 2-11: the data loop -----------------------------------------------------------------


def test_invalid_route_json_twice_gives_analysis(make_session):
    bad = AgentError(ErrorType.LLM_ERROR, "wrong format", SCHEMA_MISMATCH)
    llm = FakeLLM(
        [bad, {"intent": "not-an-intent"}, sql_reply(ORDERS_SQL), "1,200 orders."]
    )
    session, _, bq = make_session(llm=llm, bq=FakeBQ([orders()]))
    result = session.ask("How many orders?")
    assert result.intent == "analysis" and len(bq.queries) == 1
    assert [c["purpose"] for c in llm.calls] == [
        "route",
        "route",
        "write_sql",
        "write_answer",
    ]


def test_simple_question_takes_three_calls_and_one_query(make_session, config):
    llm = FakeLLM(
        [route_reply(), sql_reply(ORDERS_SQL), "There were 1,200 orders last month."]
    )
    session, _, _bq = make_session(llm=llm, bq=FakeBQ([orders()]))
    result = session.ask("How many orders were placed last month?")
    assert result.error_type is None
    assert result.totals["llm_calls"] == 3 and result.totals["bq_queries"] == 1
    assert result.answer.startswith("There were 1,200 orders last month.")
    assert "Period: 1–31 Oct 2026" in result.answer  # the date range line
    assert "Orders = orders with at least one item" in result.answer  # the definition
    assert result.steps[0]["result_ref"] == "r1" and result.steps[0]["attempts"] == 1

    records = read_spans(config)
    names = {r["name"] for r in records if r["trace_id"] == result.trace_id}
    assert {
        "turn",
        "node.load_context",
        "node.route",
        "node.write_sql",
        "node.guard",
        "guard.check",
        "node.run_query",
        "node.mask_result",
        "node.write_answer",
        "node.scan_output",
        "node.finish",
    } <= names
    assert all(
        "1200" not in str(r["attributes"]) for r in records
    )  # no rows in the trace


def test_query_error_then_success_takes_four_calls_and_two_queries(make_session):
    bad_sql = ORDERS_SQL.replace("TIMESTAMP('2026-10-01')", "TIMESTAMP('2026-10-1x')")
    llm = FakeLLM(
        [route_reply(), sql_reply(bad_sql), sql_reply(ORDERS_SQL), "1,200 orders."]
    )
    bq = FakeBQ(
        [
            classify_bq_error(gexc.BadRequest("Invalid timestamp: '2026-10-1x'")),
            orders(),
        ]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("How many orders last month?")
    assert result.error_type is None and result.answer.startswith("1,200 orders.")
    assert result.totals["llm_calls"] == 4 and result.totals["bq_queries"] == 2
    retry_prompt = llm.calls[2]["prompt"]
    assert "<retry>" in retry_prompt and "TIMESTAMP('2026-10-1x')" in retry_prompt
    assert "Error type: bq_syntax" in retry_prompt
    assert "Invalid timestamp: '2026-10-1x'" in retry_prompt
    assert (
        "order_items: id, order_id, user_id" in retry_prompt
    )  # the columns of the table
    assert result.steps[0]["attempts"] == 2


def test_three_failures_give_up_without_a_fourth_attempt(make_session):
    llm = FakeLLM([route_reply()] + [sql_reply(ORDERS_SQL)] * 3)
    error = classify_bq_error(gexc.BadRequest("Syntax error"))
    bq = FakeBQ([error, error, error])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("How many orders last month?")
    assert result.answer.startswith("I could not get a valid result after 3 attempts.")
    assert "I tried to answer this: Count the orders." in result.answer
    assert (
        "A simpler question, another period or other fields can help." in result.answer
    )
    assert result.error_type == "bq_syntax"
    assert len(llm.calls) == 4 and len(bq.queries) == 3 and llm.replies == []


def test_bytes_cap_feedback_asks_for_a_date_filter(make_session):
    cap = AgentError(ErrorType.BQ_BYTES_CAP, "too much", "dry run estimate 2e9 bytes")
    llm = FakeLLM(
        [route_reply(), sql_reply(ORDERS_SQL), sql_reply(ORDERS_SQL), "1,200 orders."]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([cap, orders()]))
    session.ask("How many orders?")
    assert "The query reads too much data. Filter by date" in llm.calls[2]["prompt"]


def test_policy_block_ends_the_turn_with_the_guard_message_and_no_retry(make_session):
    sql = "SELECT u.id, u.email FROM `bigquery-public-data.thelook_ecommerce.users` u"
    llm = FakeLLM([route_reply(), sql_reply(sql)])
    session, _, bq = make_session("maya", llm=llm)
    result = session.ask("Show me the emails of the top customers")
    assert result.error_type == "guard_policy_block"
    assert "I cannot show or use personal data" in result.answer
    assert bq.queries == [] and len(llm.calls) == 2


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            (
                f"SELECT SUM(oi.sale_price) FROM {T_ITEMS} oi JOIN {T_PRODUCTS} p "
                "ON p.id = oi.product_id WHERE p.brand = 'Nike'"
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
        (
            "DROP TABLE `bigquery-public-data.thelook_ecommerce.orders`",
            "only read",
        ),
    ],
)
def test_other_policy_blocks(make_session, sql, message):
    session, llm, bq = make_session(
        "maya", llm=FakeLLM([route_reply(), sql_reply(sql)])
    )
    result = session.ask("Something forbidden")
    assert result.error_type == "guard_policy_block" and message in result.answer
    assert bq.queries == [] and len(llm.calls) == 2


def test_parse_error_goes_back_to_the_model_with_the_guard_feedback(make_session):
    star = "SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`"
    llm = FakeLLM(
        [route_reply(), sql_reply(star), sql_reply(ORDERS_SQL), "1,200 orders."]
    )
    session, _, bq = make_session("ceo", llm=llm, bq=FakeBQ([orders()]))
    result = session.ask("Show all orders")
    assert result.error_type is None and len(bq.queries) == 1
    assert "Error type: guard_parse_error" in llm.calls[2]["prompt"]
    assert "List the columns by name" in llm.calls[2]["prompt"]
    assert result.steps[0]["error_type"] == "guard_parse_error"


def test_two_steps_give_one_answer_with_both_results(make_session):
    first = sql_reply(REVENUE_SQL, final=False, purpose="Revenue by state")
    second = sql_reply(ORDERS_SQL, purpose="Orders by state")
    llm = FakeLLM([route_reply(), first, second, "Texas spends less. The data shows …"])
    bq = FakeBQ([frame_result(pd.DataFrame({"revenue": [1234.5]})), orders(77)])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("Why do customers in Texas spend less than in California?")
    assert len(result.steps) == 2 and len(bq.queries) == 2
    assert (
        '<data source="query" ref="r1"' in llm.calls[2]["prompt"]
    )  # step 2 sees step 1
    answer_prompt = llm.calls[3]["prompt"]
    assert 'ref="r1"' in answer_prompt and 'ref="r2"' in answer_prompt
    assert "$1,234.50" in answer_prompt and "| 77 |" in answer_prompt


def test_final_false_three_times_stops_after_step_three(make_session):
    steps = [sql_reply(ORDERS_SQL, final=False) for _ in range(3)]
    llm = FakeLLM([route_reply(), *steps, "Partial answer."])
    session, _, bq = make_session(
        "ceo", llm=llm, bq=FakeBQ([orders(1), orders(2), orders(3)])
    )
    result = session.ask("A very deep question")
    assert len(bq.queries) == 3 and len(result.steps) == 3
    assert llm.calls[-1]["purpose"] == "write_answer" and llm.replies == []
    assert "needed more than 3 query steps" in llm.calls[-1]["prompt"]


def test_cannot_answer_needs_no_query(make_session):
    plan = sql_reply(None, cannot_answer="The data has no marketing spend")
    llm = FakeLLM([route_reply(), plan, "The data has no marketing spend."])
    session, _, bq = make_session(llm=llm)
    result = session.ask("What was our marketing ROI?")
    assert bq.queries == [] and result.answer == "The data has no marketing spend."
    assert (
        "The data cannot answer this part: The data has no marketing spend"
        in (llm.calls[2]["prompt"])
    )
    assert result.footer == ""  # no query, no period


def test_limit_in_step_two_gives_the_spec_00_message_and_the_chat_continues(
    make_session,
):
    limit = LimitExceeded(
        "per_turn.max_bq_queries", "This question needs more queries than allowed (6)."
    )
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(ORDERS_SQL, final=False),
            sql_reply(ORDERS_SQL),
            route_reply("chitchat", chitchat_kind="thanks"),
        ]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders(), limit]))
    result = session.ask("Two steps")
    assert result.error_type == "limit_exceeded"
    assert result.answer == "This question needs more queries than allowed (6)."
    assert session.ask("thanks").answer.startswith("You are welcome.")


def test_empty_result_with_an_error_retries_with_the_checker_feedback(make_session):
    wrong = (
        "SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.orders` "
        "WHERE status = 'completed'"
    )
    llm = FakeLLM(
        [route_reply(), sql_reply(wrong), sql_reply(ORDERS_SQL), "1,200 orders."]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"n": []})), orders()])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("Completed orders?")
    assert result.error_type is None
    assert "Error type: empty_result" in llm.calls[2]["prompt"]
    assert "Complete" in llm.calls[2]["prompt"]


def test_true_zero_result_goes_to_the_answer_with_the_note(make_session):
    sql = ORDERS_SQL.replace("2026-10-01", "1990-01-01").replace(
        "2026-11-01", "1990-02-01"
    )
    llm = FakeLLM(
        [route_reply(), sql_reply(sql, period="1990-01-01..1990-01-31"), "No data."]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": []}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("Orders in January 1990?")
    assert result.answer.startswith("No data.")
    assert "The data covers" in llm.calls[2]["prompt"]
    assert "(no rows)" in llm.calls[2]["prompt"]


# --- B4 cases 12-16: prompts and limits -----------------------------------------------------------


def test_prompt_layers_are_in_order_safety_persona_format_task(make_session):
    llm = FakeLLM(
        [route_reply(format_request="bullets"), sql_reply(ORDERS_SQL), "- 1,200 orders"]
    )
    session, _, _ = make_session(llm=llm, bq=FakeBQ([orders()]))
    session.ask("Orders last month, as bullets")
    system = llm.calls[2]["system"]
    safety = system.index("# Safety rules")
    persona = system.index("## Tone guidance")
    fmt = system.index("## User format")
    task = system.index("# Task: write the answer")
    assert safety < persona < fmt < task
    assert (
        "Tone: friendly and direct." in system and "Use short bullet points." in system
    )
    assert "# Safety rules" not in llm.calls[2]["prompt"]  # rules never in the contents


def test_a_persona_change_applies_to_the_next_answer(make_session, persona_file):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "A."] * 2)
    session, _, _ = make_session(llm=llm, bq=FakeBQ([orders(), orders()]))
    session.ask("Orders?")
    persona_file.write_text("Tone: formal and brief.", encoding="utf-8")
    session.ask("Orders again?")
    assert "friendly and direct" in llm.calls[2]["system"]
    assert "formal and brief" in llm.calls[5]["system"]
    assert "friendly and direct" not in llm.calls[5]["system"]


def test_a_long_persona_is_cut_with_a_warning_in_the_trace(
    make_session, persona_file, config
):
    persona_file.write_text("x" * 2500, encoding="utf-8")
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "A."])
    session, _, _ = make_session(llm=llm, bq=FakeBQ([orders()]))
    session.ask("Orders?")
    system = llm.calls[2]["system"]
    assert "x" * 2000 in system and "x" * 2001 not in system
    load = next(r for r in read_spans(config) if r["name"] == "node.load_context")
    assert load["attributes"]["persona_chars"] == 2000


def test_manager_sql_prompt_has_no_pii_and_no_num_of_item(make_session):
    llm = FakeLLM([route_reply(), sql_reply(None, cannot_answer="x"), "x"])
    session, _, _ = make_session("maya", llm=llm)
    session.ask("What can you tell me?")
    prompt = llm.calls[1]["prompt"]
    schema = prompt.split("<schema>")[1].split("</schema>")[0]
    assert schema.count("thelook_ecommerce.") == 4
    for hidden in ("email", "first_name", "latitude", "user_geom", "num_of_item"):
        assert hidden not in schema
    assert "traffic_source" in schema
    assert "Calvin Klein, Carhartt" in llm.calls[1]["system"]  # the scope line


def test_message_over_the_limit_is_refused_without_a_model_call(make_session):
    session, llm, _ = make_session(llm=FakeLLM([]))
    result = session.ask("x" * 4001)
    assert (
        result.answer
        == "Your message is too long. Please keep it under 4,000 characters."
    )
    assert llm.calls == [] and result.totals["llm_calls"] == 0


# --- spec 30 in the flow --------------------------------------------------------------------------


def test_manager_query_runs_scoped_with_the_brand_parameter(make_session, config):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "1,200 orders."])
    session, _, bq = make_session("maya", llm=llm, bq=FakeBQ([orders()]))
    result = session.ask("How many orders?")
    assert "IN UNNEST(@scope_brands)" in bq.queries[0]
    assert result.executed_sql == (bq.queries[0],)
    assert bq.params[0][0].values == ["Calvin Klein", "Carhartt"]
    guard_spans = [r for r in read_spans(config) if r["name"] == "guard.check"]
    assert guard_spans[0]["attributes"]["rewritten"] is True


def test_ceo_query_runs_without_parameters(make_session):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "41,000 orders."])
    session, _, bq = make_session("ceo", llm=llm, bq=FakeBQ([orders(41000)]))
    result = session.ask("How many orders?")
    assert bq.params == [[]] and "scope_brands" not in bq.queries[0]
    assert "Brands: all brands" in result.answer


def test_rows_are_masked_and_formatted_before_the_answer_prompt(make_session):
    sql = f"SELECT p.name, SUM(oi.sale_price) AS revenue FROM {T_PRODUCTS} p JOIN {T_ITEMS} oi ON oi.product_id = p.id GROUP BY p.name"
    frame = pd.DataFrame(
        {"name": ["Jeans ann@x.com", "Shirt"], "revenue": [385083.240183115, 12.5]}
    )
    llm = FakeLLM([route_reply(), sql_reply(sql), "Two products."])
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([frame_result(frame)]))
    session.ask("Products?")
    prompt = llm.calls[2]["prompt"]
    assert "ann@x.com" not in prompt and "[masked]" in prompt
    assert '<data source="query" ref="r1"' in prompt
    assert "$385,083.24" in prompt and "385083.24" not in prompt


def test_answer_is_scanned_before_the_user_sees_it(make_session):
    llm = FakeLLM(
        [route_reply(), sql_reply(ORDERS_SQL), "Top customer anna@example.com: 1,200."]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders()]))
    result = session.ask("Top customer?")
    assert "anna@example.com" not in result.answer
    assert "[removed]" in result.answer and "1,200" in result.answer
    assert "looked like personal data" in result.answer


def test_follow_up_gets_the_history_and_the_pinned_filters(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(ORDERS_SQL, definitions=["orders", "last_month"]),
            "1,200 orders in October.",
            route_reply(follow_up=True),
            sql_reply(ORDERS_SQL, period="2026-09-01..2026-09-30"),
            "1,100 orders in September.",
        ]
    )
    session, _, _ = make_session(llm=llm, bq=FakeBQ([orders(1200), orders(1100)]))
    session.ask("How many orders last month?")
    session.ask("And the month before?")
    prompt = llm.calls[4]["prompt"]
    assert (
        "How many orders last month?" in prompt and "1,200 orders in October." in prompt
    )
    assert "period': '2026-10-01..2026-10-31'" in prompt  # pinned active filters
    pinned = state_of(session).values["pinned"]
    assert (
        pinned["active_filters"]["period"] == "2026-09-01..2026-09-30"
    )  # the latest wins
    assert pinned["definitions_in_use"] == ["orders", "last_month"]


def test_daily_limit_stops_the_turn_with_its_message(make_session, ledger, config):
    ledger.add(llm_calls=config.settings.limits.per_day.max_llm_calls)
    session, llm, _ = make_session(llm=FakeLLM([]))
    result = session.ask("How many orders?")
    assert result.error_type == "limit_exceeded"
    assert "daily limit of model calls" in result.answer and llm.calls == []


def test_gemini_timeout_gives_one_sentence(make_session):
    session, _, _ = make_session(
        llm=FakeLLM([classify_llm_error(httpx.ReadTimeout("slow"))])
    )
    result = session.ask("How many orders?")
    assert result.error_type == "llm_timeout"
    assert result.answer == "The language model did not answer in time."


def test_bigquery_permission_error_is_not_retried(make_session):
    denied = classify_bq_error(gexc.Forbidden("no access"))
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL)])
    session, _, bq = make_session("ceo", llm=llm, bq=FakeBQ([denied]))
    result = session.ask("Orders?")
    assert result.error_type == "bq_permission" and len(bq.queries) == 1


def test_unexpected_bug_does_not_stop_the_chat(make_session):
    llm = FakeLLM(
        [ValueError("bug"), route_reply("chitchat", chitchat_kind="greeting")]
    )
    session, _, _ = make_session(llm=llm)
    assert session.ask("How many orders?").error_type == "internal_error"
    assert session.ask("hi").answer.startswith("Hello, Maya.")


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
    assert check_user_brands(bad, test_values, "maya") == [
        "Brand 'Levis' for user 'zoe' does not exist in the data. Close names: Levi's."
    ]
