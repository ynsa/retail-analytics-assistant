"""Live test of specs 32 and 33 in one session (marker `live`). Run by hand:

    uv run pytest -m live tests/test_delete_live.py -s

As `maya`: a report for Q1 2026 with an HTML file, then "Delete all the reports we made in
this conversation", the answer `delete 1`, and `/undo`. Planned: 4 to 6 Gemini calls (route,
plan_report, write_report, at most one correction or figure rewrite, and one route call for the
delete) and at most 4 queries (about 100-200 MB billed). The confirmation and the undo make no
model call and no query.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from opsfleet_agent.config import load_config
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.session import AssistantSession, build_services

pytestmark = pytest.mark.live
SECTIONS = (
    "Summary",
    "Findings",
    "Reasons",
    "Action items",
    "Date range and definitions",
    "Method and SQL appendix",
)


class _Headings(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h2: list[str] = []
        self._in_h2 = False
        self.svgs = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self._in_h2 = tag == "h2"
        self.svgs += tag == "svg"

    def handle_endtag(self, tag: str) -> None:
        self._in_h2 = False

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self.h2.append(data.strip())


def _show(label: str, result) -> None:
    t = result.totals
    print(
        f"\n--- {label}: intent={result.intent} calls={t['llm_calls']} "
        f"queries={t['bq_queries']} bytes={t['bytes_billed']} seconds={t['seconds']}\n"
        f"{result.answer}"
    )


def test_report_delete_and_undo_live():
    print(
        "Planned: 4 to 6 Gemini calls and at most 4 queries (about 100-200 MB billed)."
    )
    config = load_config()
    session = AssistantSession(
        config, resolve_user(config.users, "maya"), build_services(config)
    )

    report = session.send(
        "Create a report for Q1 2026 with action items for Q2, as an HTML file"
    )
    _show("report", report)
    assert report.report_id and report.report_files
    html = config.settings.project_path(Path(report.report_files[0])).read_text("utf-8")
    parser = _Headings()
    parser.feed(html)
    print(f"HTML: {len(html):,} bytes, sections {parser.h2}, charts {parser.svgs}")
    assert parser.h2 == list(SECTIONS)
    assert "<script" not in html and not re.search(r"https?:", html)

    shown = session.send("Delete all the reports we made in this conversation")
    _show("delete request", shown)
    assert shown.confirmation is not None and shown.confirmation.count == 1
    assert shown.confirmation.rows[0]["report_id"] == report.report_id

    done = session.send(shown.confirmation.expected_text)
    _show("confirmation", done)
    assert done.delete_outcome == "deleted" and done.totals["llm_calls"] == 0
    assert not config.settings.project_path(Path(report.report_files[0])).exists()

    undo = session.command("undo", [])
    print(f"\n--- /undo\n{undo.text}")
    assert undo.kind == "message" and undo.text.startswith("Restored 1 report")
    assert config.settings.project_path(Path(report.report_files[0])).exists()
