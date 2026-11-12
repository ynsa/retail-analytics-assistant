"""The report flow with a fake Gemini and a fake BigQuery (spec 32, B4 cases 1-7, 15, 15a, 16)."""

from __future__ import annotations

import pytest

from opsfleet_agent.errors import ErrorType
from tests.conftest import FakeBQ, FakeLLM, read_spans, route_reply, sql_reply
from tests.report_helpers import (
    BRAND_SQL,
    CATEGORY_SQL,
    FIXED_NOW,
    MONTHLY_SQL,
    bq_error,
    brand_frame,
    category_frame,
    draft_reply,
    monthly_frame,
    plain_draft,
    plan_reply,
    report_llm,
    report_route,
    row_of,
)

SECTIONS = (
    "## Summary",
    "## Findings",
    "## Reasons",
    "## Action items",
    "## Date range and definitions",
    "## Method and SQL appendix",
)


def start(make_session, llm, bq, user="maya"):
    session, llm, bq = make_session(user_id=user, llm=llm, bq=bq)
    session.services.clock = lambda: FIXED_NOW
    return session, llm, bq


def two_query_session(make_session, llm=None):
    return start(
        make_session, llm or report_llm(), FakeBQ([monthly_frame(), brand_frame()])
    )


# --- case 1: a normal report ----------------------------------------------------------------------
def test_normal_report_has_six_sections_a_row_and_three_model_calls(make_session):
    session, llm, bq = two_query_session(make_session)
    result = session.ask("Create a report for Q1 with insights and action items for Q2")
    assert result.error_type is None and result.report_id
    assert [c["purpose"] for c in llm.calls] == ["route", "plan_report", "write_report"]
    assert result.totals["llm_calls"] == 3 and len(bq.queries) == 2
    row = row_of(session.conn, result.report_id)
    body = row["body_markdown"]
    positions = [body.index(h) for h in SECTIONS]
    assert positions == sorted(positions)
    assert (
        row["owner_id"] == "maya" and row["conversation_id"] == session.conversation_id
    )
    assert (row["period_start"], row["period_end"]) == ("2026-01-01", "2026-03-31")
    assert row["scope_brands"] == ["Calvin Klein", "Carhartt"]
    assert [q["id"] for q in row["sql"]] == ["q1", "q2"]
    assert row["sql"][0]["job_id"] == "job-1" and row["sql"][0]["bytes_billed"] > 0
    assert (
        row["sql"][0]["sql"] == MONTHLY_SQL
    )  # the SQL of the model, not the scoped SQL
    assert row["model"] and row["prompt_version"] and row["trace_id"]
    assert row["file_html"] is None and row["file_md"] is None
    assert "revenue" in row["definitions"]
    assert f"Saved as {result.report_id}." in result.answer
    assert "Restock Carhartt outerwear" in result.answer  # the action items in the chat
    # each planned query ran with the brand scope of the guard
    assert all("@scope_brands" in q for q in bq.queries)


def test_report_in_the_chat_has_no_file_without_a_request(make_session, config):
    session, *_ = two_query_session(make_session)
    session.ask("Create a report for Q1")
    assert not config.settings.reports_dir().exists()


def test_report_with_html_request_writes_the_file(make_session, config):
    llm = report_llm(route=report_route(file_format="html"))
    session, *_ = two_query_session(make_session, llm)
    result = session.ask("Create a report for Q1 as an HTML file")
    path = config.settings.reports_dir() / "maya" / f"{result.report_id}.html"
    assert (
        path.exists()
        and result.report_files
        and result.report_files[0].endswith(".html")
    )
    assert (
        row_of(session.conn, result.report_id)["file_html"]
        == f"reports/maya/{result.report_id}.html"
    )
    assert "File:" in result.answer


def test_report_with_markdown_request_writes_mermaid(make_session, config):
    llm = report_llm(route=report_route(file_format="md"))
    session, *_ = two_query_session(make_session, llm)
    result = session.ask("Create a report for Q1 as a Markdown file")
    text = (
        config.settings.reports_dir() / "maya" / f"{result.report_id}.md"
    ).read_text()
    assert "```mermaid\nxychart-beta" in text and "| month | revenue |" in text


# --- case 2: at most 4 planned queries --------------------------------------------------------------
def test_plan_with_six_queries_runs_the_first_four(make_session):
    plan = plan_reply([(f"Part {i}", MONTHLY_SQL) for i in range(1, 7)])
    session, _llm, bq = start(
        make_session,
        report_llm(plan=plan, draft=plain_draft()),
        FakeBQ([monthly_frame() for _ in range(4)]),
    )
    result = session.ask("Create a big report")
    assert len(bq.queries) == 4 and result.report_id
    assert [q["id"] for q in row_of(session.conn, result.report_id)["sql"]] == [
        "q1",
        "q2",
        "q3",
        "q4",
    ]
    plan_spans = [
        s for s in read_spans(session.config) if s["name"] == "node.plan_report"
    ]
    assert plan_spans and plan_spans[0]["attributes"]["planned_queries"] == 6


# --- case 3: the period ends after today ------------------------------------------------------------
def test_period_end_after_today_moves_to_today(make_session):
    plan = plan_reply(start="2026-10-01", end="2026-12-31")
    session, *_ = start(
        make_session, report_llm(plan=plan), FakeBQ([monthly_frame(), brand_frame()])
    )
    result = session.ask("Create a report for Q4 2026")
    row = row_of(session.conn, result.report_id)
    assert row["period_end"] == "2026-11-11"
    assert "The planned period ended after today" in row["body_markdown"]


# --- case 4: one query fails 3 times ---------------------------------------------------------------
def test_one_failed_query_marks_the_missing_part(make_session):
    llm = report_llm(
        sql_reply(BRAND_SQL, purpose="Revenue by brand"),
        sql_reply(BRAND_SQL, purpose="Revenue by brand"),
        draft=plain_draft(),
    )
    bq = FakeBQ([monthly_frame(), bq_error(), bq_error(), bq_error()])
    session, llm, bq = start(make_session, llm, bq)
    result = session.ask("Create a report for Q1")
    assert result.report_id and result.error_type is None
    assert [c["purpose"] for c in llm.calls] == [
        "route",
        "plan_report",
        "write_sql",
        "write_sql",
        "write_report",
    ]
    row = row_of(session.conn, result.report_id)
    assert row["sections"]["missing"] == [
        "Revenue by brand: no result. The query failed after 3 attempts."
    ]
    assert "Missing part:" in row["body_markdown"] and "Missing part:" in result.answer
    assert row["sql"][1]["status"] == "missing"
    write_prompt = llm.calls[-1]["prompt"]
    assert (
        "Revenue by brand: no result" in write_prompt
    )  # the model knows the missing part


def test_a_policy_block_marks_the_part_missing(make_session):
    blocked = "SELECT u.email FROM `bigquery-public-data.thelook_ecommerce.users` u"
    plan = plan_reply([("Revenue by month", MONTHLY_SQL), ("Emails", blocked)])
    session, _llm, bq = start(
        make_session,
        report_llm(plan=plan, draft=plain_draft()),
        FakeBQ([monthly_frame()]),
    )
    result = session.ask("Create a report with the emails")
    row = row_of(session.conn, result.report_id)
    assert len(bq.queries) == 1
    assert "did not pass the data access rules" in row["sections"]["missing"][0]


# --- case 5: all queries fail ---------------------------------------------------------------------
def test_all_queries_fail_gives_no_report(make_session):
    plan = plan_reply([("Revenue by month", MONTHLY_SQL)])
    llm = FakeLLM(
        [report_route(), plan, sql_reply(MONTHLY_SQL), sql_reply(MONTHLY_SQL)]
    )
    session, llm, _bq = start(make_session, llm, FakeBQ([bq_error()] * 3))
    result = session.ask("Create a report for Q1")
    assert result.report_id is None and "nothing was saved" in result.answer
    assert session.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert "write_report" not in [c["purpose"] for c in llm.calls]


# --- case 6: the time limit ------------------------------------------------------------------------
class SlowLLM(FakeLLM):
    """Moves the fake clock past 150 s before the call with the given purpose."""

    def __init__(self, replies, clock, at):
        super().__init__(replies)
        self.clock, self.at = clock, at

    def generate(self, budget, **kwargs):
        if kwargs.get("purpose") == self.at:
            self.clock[0] = 151.0
        return super().generate(budget, **kwargs)


@pytest.mark.parametrize("at", ["write_report", "plan_report"])
def test_report_over_150_seconds_saves_nothing(make_session, config, at):
    clock = [0.0]
    llm = SlowLLM(report_llm(route=report_route(file_format="html")).replies, clock, at)
    session, llm, _bq = start(
        make_session, llm, FakeBQ([monthly_frame(), brand_frame()])
    )
    session.monotonic = lambda: clock[0]
    result = session.ask("Create a report for Q1 as HTML")
    assert result.answer.startswith("The report took longer than 150 s.")
    assert "shorter period or fewer topics" in result.answer
    assert result.error_type == ErrorType.LIMIT_EXCEEDED.value
    assert session.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert not config.settings.reports_dir().exists()


def test_question_keeps_the_60_second_limit(make_session):
    clock = [0.0]
    llm = SlowLLM([route_reply(), sql_reply(MONTHLY_SQL)], clock, "write_sql")
    session, *_ = start(make_session, llm, FakeBQ())
    session.monotonic = lambda: clock[0]
    result = session.ask("How many orders?")
    assert result.answer.startswith("The answer took longer than 60 s.")


# --- case 7: save the last answer -----------------------------------------------------------------
def test_save_last_answer_runs_no_query(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(
                MONTHLY_SQL,
                purpose="Revenue by month",
                period="2026-01-01..2026-03-31",
                definitions=["revenue"],
            ),
            "Revenue was $1,200.50 in January.",
            report_route("save_last"),
            draft_reply(
                title="Q1 revenue by month",
                findings=[
                    {
                        "text": "January had $1,200.50.",
                        "table": {"ref": "r1", "columns": ["month", "revenue"]},
                    }
                ],
            ),
        ]
    )
    session, llm, bq = start(make_session, llm, FakeBQ([monthly_frame()]))
    session.ask("What was the revenue by month in Q1 2026?")
    result = session.ask("Save this as a report")
    assert result.totals["bq_queries"] == 0 and result.totals["llm_calls"] == 2
    assert len(bq.queries) == 1
    row = row_of(session.conn, result.report_id)
    assert row["title"] == "Q1 revenue by month"
    assert (row["period_start"], row["period_end"]) == ("2026-01-01", "2026-03-31")
    assert row["sql"][0]["sql"] == MONTHLY_SQL


def test_save_last_without_an_answer_explains(make_session):
    session, _llm, _bq = start(
        make_session, FakeLLM([report_route("save_last")]), FakeBQ()
    )
    result = session.ask("Save this as a report")
    assert "no earlier answer with data" in result.answer and result.report_id is None


# --- cases 15 and 15a: the figure check ----------------------------------------------------------
def test_figure_check_counts_one_calculation(make_session):
    session, *_ = two_query_session(make_session)
    result = session.ask("Create a report for Q1")
    row = row_of(session.conn, result.report_id)
    assert row["calculated_figures"] == 1 and row["unverified_figures"] == 0
    assert "$3,400.75" in row["body_markdown"]  # the sum that code computed
    assert "1 figure in the text is a code calculation" in row["body_markdown"]
    assert result.unverified_figures == 0


def bad_draft():
    return draft_reply(
        summary=["Revenue grew by 17.3% in Q1 2026.", "February had $1,300.25."]
    )


def test_untraceable_figure_gets_one_rewrite_then_is_removed(make_session):
    llm = report_llm(draft=bad_draft())
    llm.replies.append({"sentences": ["Revenue grew by 17.3% in Q1 2026."]})
    session, llm, _ = two_query_session(make_session, llm)
    result = session.ask("Create a report for Q1")
    assert [c["purpose"] for c in llm.calls][-1] == "fix_figures"
    assert "Revenue grew by 17.3%" in llm.calls[-1]["prompt"]
    row = row_of(session.conn, result.report_id)
    assert row["unverified_figures"] == 1 and result.unverified_figures == 1
    assert (
        "Revenue grew by [figure removed: not traceable] in Q1 2026."
        in row["body_markdown"]
    )
    assert "17.3" not in row["body_markdown"]


def test_rewrite_with_a_placeholder_keeps_the_sentence(make_session):
    llm = report_llm(draft=bad_draft())
    fix = "Revenue changed by {{calc:pct_change(r1.revenue@2026-03, r1.revenue@2026-01)}} from January to March."
    llm.replies.append({"sentences": [fix]})
    session, llm, _ = two_query_session(make_session, llm)
    result = session.ask("Create a report for Q1")
    row = row_of(session.conn, result.report_id)
    assert row["unverified_figures"] == 0 and row["calculated_figures"] == 1
    assert "-25.0%" in row["body_markdown"]


# --- case 16: entities ----------------------------------------------------------------------------
def test_entities_from_filters_results_and_title(make_session):
    plan = plan_reply(
        [("Revenue by brand", BRAND_SQL), ("Carhartt categories", CATEGORY_SQL)],
        title="Calvin Klein Jeans and Carhartt review",
    )
    session, *_ = start(
        make_session,
        report_llm(plan=plan, draft=plain_draft()),
        FakeBQ([brand_frame(), category_frame()]),
    )
    result = session.ask("Create a report on my brands")
    entities = {
        (r["kind"], r["value"])
        for r in session.conn.execute(
            "SELECT kind, value FROM report_entities WHERE report_id = ?",
            (result.report_id,),
        )
    }
    assert entities == {
        ("brand", "Carhartt"),  # a filter literal and a result value
        ("brand", "Calvin Klein"),  # a result value of the column `brand`
        ("brand", "Calvin Klein Jeans"),  # an exact brand name in the title
        ("category", "Outerwear & Coats"),
        ("category", "Pants"),
    }
    norms = [
        r[0] for r in session.conn.execute("SELECT value_norm FROM report_entities")
    ]
    assert "outerwear & coats" in norms


# --- the scanner ---------------------------------------------------------------------
def test_email_in_model_text_is_removed_from_body_and_files(make_session, config):
    draft = draft_reply(reasons=["Contact jane.doe@example.com about the drop."])
    llm = report_llm(draft=draft, route=report_route(file_format="md"))
    session, *_ = two_query_session(make_session, llm)
    result = session.ask("Create a report for Q1 as Markdown")
    row = row_of(session.conn, result.report_id)
    assert (
        "jane.doe" not in row["body_markdown"] and "[removed]" in row["body_markdown"]
    )
    text = (
        config.settings.reports_dir() / "maya" / f"{result.report_id}.md"
    ).read_text()
    assert "jane.doe" not in text and "[removed]" in text
    session.command("export", [result.report_id, "--format", "html"])
    html = (
        config.settings.reports_dir() / "maya" / f"{result.report_id}.html"
    ).read_text()
    assert "jane.doe" not in html and "[removed]" in html
