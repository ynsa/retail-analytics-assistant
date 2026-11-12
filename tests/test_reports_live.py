"""Live test of spec 32 (marker `live`). Run by hand:

    uv run pytest -m live tests/test_reports_live.py -s

One report for the manager `maya`, with an HTML file. Planned: 3 to 5 Gemini calls (route,
plan_report, write_report, at most one correction or one figure rewrite) and at most 4 queries
(about 100-200 MB billed). The free-tier Gemini key has rate limits.
"""

from __future__ import annotations

import re

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


def test_report_with_html_file_live():
    print(
        "Planned: 3 to 5 Gemini calls and at most 4 queries (about 100-200 MB billed)."
    )
    config = load_config()
    session = AssistantSession(
        config, resolve_user(config.users, "maya"), build_services(config)
    )
    result = session.ask(
        "Create a report for Q1 2026 with action items for Q2, as an HTML file"
    )
    t = result.totals
    print(
        f"calls={t['llm_calls']} queries={t['bq_queries']} bytes={t['bytes_billed']} "
        f"seconds={t['seconds']}\n{result.answer}"
    )
    assert result.report_id and result.report_files
    assert t["llm_calls"] <= 8 and t["bq_queries"] <= 6
    html = config.settings.project_path(
        __import__("pathlib").Path(result.report_files[0])
    )
    text = html.read_text(encoding="utf-8")
    positions = [text.index(f"<h2>{name}</h2>") for name in SECTIONS]
    assert positions == sorted(positions)
    assert "<script" not in text and not re.search(r"https?:", text)
