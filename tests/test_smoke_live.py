"""Live smoke test (spec 01): 2 Gemini calls and 1 small BigQuery query (about 10 MB billed).

Run by hand: uv run pytest -m live
"""

import re

import pytest

from opsfleet_agent.config import load_config
from opsfleet_agent.identity import resolve_user
from opsfleet_agent.session import AssistantSession, build_services


@pytest.mark.live
def test_one_question_end_to_end():
    config = load_config()
    session = AssistantSession(
        config, resolve_user(config.users, "ceo"), build_services(config)
    )
    result = session.ask("How many orders were placed in 2024?")
    assert result.error_type is None, result.answer
    assert re.search(r"\d", result.answer)
    assert result.totals["llm_calls"] == 2 and result.totals["bq_queries"] == 1
