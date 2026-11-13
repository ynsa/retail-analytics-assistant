"""Report files and the library (spec 32, B4 cases 11, 12, 14, 17-20)."""

from __future__ import annotations

import re

import pytest

from opsfleet_agent.errors import AgentError, ReportNotFound
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.reports.files import report_file
from opsfleet_agent.reports.library import Matcher, ReportLibrary
from opsfleet_agent.reports.render_html import render_html
from opsfleet_agent.reports.render_md import render_md_file
from opsfleet_agent.reports.text import clean_model_text
from tests.conftest import FakeBQ
from tests.report_helpers import (
    FIXED_NOW,
    brand_frame,
    draft_reply,
    make_report,
    monthly_frame,
    report_llm,
    report_route,
    row_of,
)

LINE_CHART = {
    "ref": "r1",
    "type": "line",
    "requested_type": "line",
    "title": "Revenue by month",
    "x": "month",
    "y": ["revenue"],
    "kinds": {"revenue": "money"},
    "labels": ["2026-01", "2026-02"],
    "series": {"revenue": [1200.5, 1300.25]},
}


@pytest.fixture
def library(config, conn) -> ReportLibrary:
    return ReportLibrary(conn, config.settings, clock=lambda: FIXED_NOW)


@pytest.fixture
def maya(config):
    return resolve_user(config.users, "maya")


@pytest.fixture
def alex(config):
    return resolve_user(config.users, "alex")


# --- case 11: the HTML file ------------------------------------------------------------------------
def test_html_has_no_script_no_url_and_the_csp(library):
    report = make_report(
        "R-7K2PQ",
        title='Q1 & Q2 "plan" <b>review</b>',
        charts=[LINE_CHART],
        sections={
            "summary": ["Sales < plan & costs > budget."],
            "findings": [{"text": "A finding.", "table": None}],
            "period_text": "1 Jan – 31 Mar 2026",
        },
    )
    html = render_html(report)
    assert "<script" not in html.lower() and "javascript" not in html.lower()
    assert not re.search(r"https?:|ftp:|//|url\((?!#)|@import", html)
    assert (
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:\">" in html
    )
    assert "<title>Q1 &amp; Q2 &#34;plan&#34; &lt;b&gt;review&lt;/b&gt;</title>" in html
    assert "Sales &lt; plan &amp; costs &gt; budget." in html
    assert html.count("<svg") == 1 and "[[chart" not in html
    assert render_html(report) == html  # the same report gives the same file


# --- case 12: links and images --------------------------------------------------------------------
def test_links_images_and_html_are_stripped():
    text = 'See [the shop](https://evil.example/x) ![pixel](https://evil.example/p.png) <img src="https://evil.example/i.png"> now https://evil.example/raw.'
    assert clean_model_text(text) == "See the shop now"


def test_links_are_stripped_from_the_body_and_the_files(make_session, config):
    draft = draft_reply(
        reasons=[
            (
                "The data shows a drop, see [the dashboard](https://evil.example/d) "
                '![x](https://evil.example/p.png) <img src="https://evil.example/i.png">.'
            )
        ]
    )
    session, *_ = make_session(
        llm=report_llm(draft=draft, route=report_route(file_format="html")),
        bq=FakeBQ([monthly_frame(), brand_frame()]),
    )
    result = session.ask("Create a report for Q1 as HTML")
    row = row_of(session.conn, result.report_id)
    assert (
        "evil" not in row["body_markdown"]
        and "see the dashboard" in row["body_markdown"]
    )
    session.command("export", [result.report_id, "--format", "md"])
    for ext in ("html", "md"):
        text = (
            config.settings.reports_dir() / "maya" / f"{result.report_id}.{ext}"
        ).read_text()
        assert "evil" not in text and "<img" not in text


def test_markdown_file_has_mermaid_and_a_table(library):
    text = render_md_file(make_report("R-7K2PQ", charts=[LINE_CHART]))
    assert '```mermaid\nxychart-beta\n    title "Revenue by month"' in text
    assert "| month | revenue |\n|---|---|\n| 2026-01 | $1,200.50 |" in text


# --- case 17: ownership ---------------------------------------------------------------------------
def test_other_users_report_is_not_found_with_the_same_text(library, maya, alex):
    library.create(make_report("R-AAAAA", owner_id="alex"))
    with pytest.raises(ReportNotFound) as other:
        library.get(maya, "R-AAAAA")
    with pytest.raises(ReportNotFound) as missing:
        library.get(maya, "R-ZZZZZ")
    assert other.value.user_message == "Report R-AAAAA not found."
    assert missing.value.user_message == "Report R-ZZZZZ not found."
    with pytest.raises(ReportNotFound):
        library.export(maya, "R-AAAAA", "html")
    assert library.list(maya).total == 0 and library.find(maya, Matcher("all")) == []
    assert library.get(alex, "r-aaaaa").report_id == "R-AAAAA"  # case does not matter


def test_open_and_export_commands_of_another_user(make_session, library):
    library.create(make_report("R-AAAAA", owner_id="alex"))
    session, *_ = make_session()
    for name, args in (
        ("open", ["R-AAAAA"]),
        ("export", ["R-AAAAA", "--format", "md"]),
    ):
        out = session.command(name, args)
        assert out.kind == "error" and out.text == "Report R-AAAAA not found."
    assert session.command("open", ["R-QQQQQ"]).text == "Report R-QQQQQ not found."


def test_commands_list_open_export(make_session, library, config):
    library.create(
        make_report(
            "R-BBBBB",
            title="Q1 review",
            body="# Q1 review\n\nThe body.",
            charts=[LINE_CHART],
        )
    )
    session, llm, bq = make_session()
    listing = session.command("reports", [])
    assert (
        "| R-BBBBB | Q1 review | 1 Jan – 31 Mar 2026 | 2026-11-11 |  |" in listing.text
    )
    assert session.command("open", ["r-bbbbb"]).text.startswith("# Q1 review")
    out = session.command("export", ["R-BBBBB", "--format", "md"])
    assert out.kind == "message" and out.path and out.path.endswith("R-BBBBB.md")
    assert (config.settings.reports_dir() / "maya" / "R-BBBBB.md").exists()
    assert "MD" in session.command("reports", []).text
    assert (
        session.command("export", ["R-BBBBB", "--format", "pdf"]).text
        == "Usage: /export <id> --format html|md"
    )
    assert llm.calls == [] and bq.queries == []


def test_export_twice_replaces_the_file(library, maya, config):
    library.create(make_report("R-CCCCC"))
    first = library.export(maya, "R-CCCCC", "md")
    second = library.export(maya, "R-CCCCC", "md")
    assert first == second and len(list(first.parent.iterdir())) == 1


# --- case 18: owner ID ----------------------------------------------------------------------------
@pytest.mark.parametrize("owner", ["../x", "..", "Maya", "a/b", ""])
def test_unsafe_owner_id_is_rejected_before_any_write(config, owner):
    base = config.settings.reports_dir()
    with pytest.raises(AgentError) as err:
        report_file(base, owner, "R-7K2PQ", "html")
    assert err.value.user_message == "The report file could not be written."
    assert not base.exists()


def test_unsafe_owner_in_the_library_writes_nothing(library, config):
    library.create(make_report("R-DDDDD", owner_id="../x"))
    from opsfleet_agent.config import Role
    from opsfleet_agent.identity import UserContext

    user = UserContext("../x", "X", Role.MANAGER, ("Carhartt",))
    with pytest.raises(AgentError):
        library.export(user, "R-DDDDD", "html")
    assert not config.settings.reports_dir().exists()
    assert not (config.settings.data_path() / "x").exists()


@pytest.mark.parametrize("report_id", ["R-../X", "R-7K2PQ/../../a", "X-7K2PQ"])
def test_unsafe_report_id_is_rejected(config, report_id):
    with pytest.raises(AgentError):
        report_file(config.settings.reports_dir(), "maya", report_id, "md")


# --- case 19: deleted reports ---------------------------------------------------------------------
def test_deleted_report_is_not_in_list_get_or_find(library, maya, conn):
    library.create(make_report("R-EEEEE", entities={"brand": ["Carhartt"]}))
    conn.execute(
        "UPDATE reports SET deleted_at = '2026-11-11', delete_batch_id = 'D-AAAAA'"
    )
    conn.commit()
    assert library.list(maya).reports == []
    with pytest.raises(ReportNotFound):
        library.get(maya, "R-EEEEE")
    for matcher in (
        Matcher("all"),
        Matcher("conversation", conversation_id="conv-1"),
        Matcher("ids", ids=("R-EEEEE",)),
        Matcher("entity", value_norm="carhartt"),
        Matcher("text", phrase='"report"'),
    ):
        assert library.find(maya, matcher) == []


# --- case 20: 60 reports --------------------------------------------------------------------------
def test_list_shows_the_newest_50_and_the_total(library, maya, make_session):
    for i in range(60):
        library.create(
            make_report(
                f"R-{i:05d}",
                created_at=f"2026-11-{1 + i // 10:02d}T10:{i % 60:02d}:00+00:00",
            )
        )
    listing = library.list(maya)
    assert len(listing.reports) == 50 and listing.total == 60
    assert listing.reports[0].report_id == "R-00059"
    session, *_ = make_session()
    assert "Showing the newest 50 of 60 reports." in session.command("reports", []).text


def test_library_row_has_all_fields(make_session):
    session, *_ = make_session(
        llm=report_llm(), bq=FakeBQ([monthly_frame(), brand_frame()])
    )
    result = session.ask("Create a report for Q1")
    row = row_of(session.conn, result.report_id)
    expected = {
        "report_id",
        "owner_id",
        "conversation_id",
        "title",
        "question",
        "created_at",
        "period_start",
        "period_end",
        "scope_brands",
        "sql",
        "body_markdown",
        "sections",
        "charts",
        "definitions",
        "entities",
        "calculated_figures",
        "unverified_figures",
        "file_html",
        "file_md",
        "model",
        "prompt_version",
        "trace_id",
        "deleted_at",
        "delete_batch_id",
    }
    assert set(row) == expected
    assert row["charts"][0]["series"]["revenue"] == [1200.5, 1300.25, 900.0]
    assert row["entities"]["brand"] == ["Calvin Klein", "Carhartt"]


def test_fts_follows_the_reports_table(library, maya, conn):
    library.create(
        make_report("R-FFFFF", title="Levi's returns", body="Returns of Levi's jeans.")
    )
    assert [
        f.summary.report_id
        for f in library.find(maya, Matcher("text", phrase='"levi\'s returns"'))
    ] == ["R-FFFFF"]
    conn.execute("DELETE FROM reports WHERE report_id = 'R-FFFFF'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM reports_fts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM report_entities").fetchone()[0] == 0
