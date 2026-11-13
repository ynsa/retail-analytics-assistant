"""Live tests of spec 31 (marker `live`). Run by hand, one test at a time:

    uv run pytest -m live tests/test_agent_live.py -k owner -s

- `test_owner_session_live` (spec 21, group OS): 5 turns as maya. About 9 Gemini calls and
  2 queries (about 40-80 MB billed).
- `test_capabilities_live` (spec 31, B4): one question for each capability of the brief. About
  15-20 Gemini calls and 5-7 queries (about 100-300 MB billed).

Each test prints the planned calls before the start. The free-tier Gemini key has rate limits.
"""

from __future__ import annotations

import re

import pytest

from opsfleet_agent.config import load_config
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.session import AssistantSession, build_services

pytestmark = pytest.mark.live
RAW_NUMBER = re.compile(r"\d+\.\d{3,}")


def _session(user_id: str) -> AssistantSession:
    config = load_config()
    return AssistantSession(
        config, resolve_user(config.users, user_id), build_services(config)
    )


def _show(label: str, result) -> None:
    t = result.totals
    print(
        f"\n--- {label}: intent={result.intent} calls={t['llm_calls']} "
        f"queries={t['bq_queries']} bytes={t['bytes_billed']}\n{result.answer}"
    )


def test_owner_session_live():
    print("Planned: about 9 Gemini calls and 2 queries.")
    session = _session("maya")
    os1 = session.ask("hi")
    _show("OS-01", os1)
    assert os1.intent == "chitchat" and os1.totals["bq_queries"] == 0

    for label, message in (
        ("OS-02", "yes, what projects I have access to?"),
        ("OS-03", "what companies I can check in the data?"),
    ):
        access = session.ask(message)
        _show(label, access)
        assert access.intent == "schema" and access.totals["bq_queries"] == 0
        assert "Calvin Klein" in access.answer and "Carhartt" in access.answer
        assert (
            "google" not in access.answer.lower()
            and "project" not in access.answer.lower()
        )

    os4 = session.ask("give me the stats for the last nmonth")
    _show("OS-04", os4)
    assert os4.intent == "analysis" and os4.error_type is None
    assert os4.executed_sql and all("@scope_brands" in sql for sql in os4.executed_sql)
    assert not RAW_NUMBER.search(os4.answer)
    assert "Period:" in os4.footer and "Calvin Klein, Carhartt" in os4.footer

    os5 = session.ask("last month")
    _show("OS-05", os5)
    assert os5.intent == "analysis" and os5.error_type is None
    assert not RAW_NUMBER.search(os5.answer)


CAPABILITIES = (
    ("customer behavior", "How many customers bought more than once last quarter?"),
    ("product performance", "Which 5 categories had the highest revenue last month?"),
    ("time-based metric", "What was the revenue for each month of Q3 2026?"),
    ("schema", "What does the status of an order item mean?"),
    (
        "multi-step",
        "Why are customers in Texas spending less than customers in California?",
    ),
)


def test_capabilities_live():
    print("Planned: about 15-20 Gemini calls and 5-7 queries.")
    session = _session("ceo")
    for label, question in CAPABILITIES:
        result = session.ask(question)
        _show(label, result)
        assert result.error_type is None, result.answer
        assert not RAW_NUMBER.search(result.answer)
        if label == "schema":
            assert result.totals["bq_queries"] == 0
        else:
            assert result.footer and result.totals["llm_calls"] <= 8
