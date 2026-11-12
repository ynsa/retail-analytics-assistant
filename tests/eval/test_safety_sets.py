"""The safety sets (spec 22) and the owner's session (spec 21, B2.2a) load and follow the rules."""

from __future__ import annotations

from collections import Counter

import sqlglot
from sqlglot import exp

from opsfleet_agent.evals.cases import ConversationCase, load_set
from opsfleet_agent.evals.safety_checks import KNOWN_CHECKS, PII_COLUMNS, parse_check

CATEGORY_COUNTS = {
    "prompt_injection": 6,
    "data_injection": 4,
    "manipulation": 6,
    "system_prompt": 6,
    "off_topic": 5,
    "benign": 3,
    "pii": 9,
    "cross_brand": 7,
    "write": 7,
    "delete": 8,
}
BRAND_NAMES = ("calvin klein", "carhartt", "levi")
JUDGE_CRITERIA = {"J-REF", "J-ALT", "J-TOPIC", "J-ANS"}


def _conversation_sets() -> list[ConversationCase]:
    cases: list[ConversationCase] = []
    for name in ("safety", "safety_long", "owner_session"):
        cases += load_set(name).cases  # type: ignore[arg-type]
    return cases


def test_safety_set_has_61_cases_in_10_categories():
    cases = load_set("safety").cases
    assert len(cases) == 61
    assert Counter(c.category for c in cases) == CATEGORY_COUNTS  # type: ignore[union-attr]


def test_safety_plan_is_near_the_spec_estimate():
    total = sum(c.expected_calls for c in load_set("safety").cases)
    assert 120 <= total <= 180  # spec 22, B5: about 150 agent calls


def test_every_case_has_a_code_check_and_known_names():
    for case in _conversation_sets():
        names = [parse_check(spec)[0] for t in case.turns for spec in t.checks]
        assert names, f"{case.id} has no code check"
        assert set(names) <= KNOWN_CHECKS
        judged = {j for i in range(len(case.turns)) for j in case.judge_for_turn(i)}
        assert judged <= JUDGE_CRITERIA, case.id


def test_cases_are_ready_and_marked_for_owner_review():
    for case in _conversation_sets():
        assert case.status == "ready"
        assert case.reviewed_by == "claude-draft" and case.reviewed_at


def test_long_conversation_and_owner_session_turns():
    long_case = load_set("safety_long").cases[0]
    assert long_case.id == "LC-01" and len(long_case.turns) == 32
    assert {"F1", "big_orders"} <= set(long_case.references)  # type: ignore[union-attr]
    owner = load_set("owner_session").cases[0]
    assert owner.id == "OS-01" and len(owner.turns) == 5
    assert "overview" in owner.references  # type: ignore[union-attr]


def test_delete_cases_use_the_seed_fixture():
    for case in load_set("safety").cases:
        if case.category == "delete":  # type: ignore[union-attr]
            assert case.fixture == "seed_reports"  # type: ignore[union-attr]


def test_reference_sql_has_no_brand_name_and_no_pii_column():
    for case in _conversation_sets():
        for name, ref in case.references.items():
            text = ref.sql.lower()
            assert not any(b in text for b in BRAND_NAMES), f"{case.id}/{name}"
            assert "@brands" in text and "@all_brands" in text, f"{case.id}/{name}"
            tree = sqlglot.parse_one(ref.sql, read="bigquery")
            columns = {c.name.lower() for c in tree.find_all(exp.Column)}
            assert not columns & PII_COLUMNS, f"{case.id}/{name}"
