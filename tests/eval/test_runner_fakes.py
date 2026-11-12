"""The runner with a fake agent, a fake judge and a fake BigQuery (spec 23 B4). No network.

A full run, the "yes" rule, a pause at the cap, a resume, the daily limit, the rate limit, a data
change during the run, the human labels and the comparison.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from opsfleet_agent.evals.orchestrate import EvalEnv, execute_run, make_report
from opsfleet_agent.evals.records import TurnRecord
from opsfleet_agent.evals.review import review
from opsfleet_agent.evals.snapshot import SnapshotStore
from opsfleet_agent.evals.storage import CallStats, RunStore

from .fakes import FakeAdapter, FakeJudgeLLM, FakeReferenceRunner, perfect_turn

NOW = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.UTC)
SMALL = ["TM-01", "PP-01"]  # 6 planned agent calls: no "yes" necessary
BIG = ["TM-01", "PP-01", "CB-01", "CB-02", "PP-03"]  # 15 planned calls


class Env:
    """An EvalEnv with fakes, plus handles for the tests."""

    def __init__(
        self, config, ledger, tmp_path, answers: list[str] | None = None
    ) -> None:
        fast = config.settings.eval.model_copy(update={"call_interval_s": 0})
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"eval": fast})}
        )
        self.runner = FakeReferenceRunner()
        self.judge_llm = FakeJudgeLLM()
        self.adapters: list[FakeAdapter] = []
        self.answers = answers or []
        self.logs: list[str] = []
        self.sleeps: list[float] = []
        self.adapter_kw: dict[str, Any] = {}
        self.env = EvalEnv(
            config=config,
            ledger=ledger,
            runs=RunStore(tmp_path / "runs"),
            snapshots=SnapshotStore(tmp_path / "snapshots"),
            stats=CallStats(tmp_path / "call_stats.json"),
            reference_runner=lambda: self.runner,
            adapter_factory=self._adapter,
            judge_llm_factory=lambda: self.judge_llm,
            ask=lambda prompt: self.answers.pop(0) if self.answers else "",
            log=self.logs.append,
            sleep=self.sleeps.append,
            now=lambda: NOW,
        )

    def _adapter(self, cap, model, throttle):
        adapter = FakeAdapter(cap, self.env.ledger, model=model, **self.adapter_kw)
        self.adapters.append(adapter)
        return adapter

    def run(self, **kw: Any):
        return execute_run(self.env, **kw)

    def lines(self, run_id: str) -> list[dict[str, Any]]:
        return self.env.runs.results(run_id)


@pytest.fixture
def e(config, ledger, tmp_path) -> Env:
    return Env(config, ledger, tmp_path)


def test_full_run_writes_results_report_and_index(e: Env):
    out = e.run(set_name="smoke")
    assert out.status == "done", out.message
    lines = e.lines(out.run_id)
    assert {x["case_id"] for x in lines} == {
        "TM-01",
        "PP-01",
        "SC-02",
        "OT-01",
        "SP-04",
    }
    assert all(x["passed"] for x in lines), [
        x["scorers"] for x in lines if not x["passed"]
    ]
    meta = e.env.runs.meta(out.run_id)
    assert meta["approval"].startswith("auto") and meta["judge_model"] is None
    assert meta["actual"]["agent_calls"] == 10 and meta["status"] == "done"
    assert set(meta["hashes"]) == {"set", "prompts", "rubrics", "settings"}
    report = out.report_path.read_text()
    for part in ("## Summary", "## Safety gate", "## Failed cases", "## Skipped cases"):
        assert part in report
    assert "Gate: **pass**" in report
    assert e.env.runs.index()[0]["run_id"] == out.run_id
    assert e.env.runs.record(out.run_id, "TM-01").turns[0].queries


def test_run_above_ten_calls_needs_yes(e: Env):
    e.answers = ["no"]
    out = e.run(set_name="accuracy", case_ids=BIG)
    assert out.status == "not_started" and out.run_id is None
    assert not e.adapters and e.env.runs.all_run_ids() == []
    assert 'Type "yes"' not in " ".join(
        e.logs
    )  # the question goes to `ask`, not the log
    e.answers = ["yes"]
    out = e.run(set_name="accuracy", case_ids=BIG)
    assert out.status == "done"
    assert e.env.runs.meta(out.run_id)["approval"] == "typed yes"


def test_yes_flag_and_model_option(e: Env):
    out = e.run(set_name="accuracy", case_ids=BIG, yes=True, model="gemini-3.8-flash")
    meta = e.env.runs.meta(out.run_id)
    assert meta["agent_model"] == "gemini-3.8-flash" and "-flash-" in out.run_id
    assert meta["approval"].startswith("--yes")
    assert e.adapters[0].model == "gemini-3.8-flash"


def test_pause_at_cap_and_resume_without_second_run(e: Env):
    e.adapter_kw = {"calls_per_turn": 5}  # more than planned (3 per case)
    out = e.run(set_name="accuracy", case_ids=BIG, yes=True)
    assert out.status == "paused_cap"
    assert "--resume" in out.message
    first = {x["case_id"] for x in e.lines(out.run_id)}
    assert 0 < len(first) < len(BIG)
    assert e.env.runs.meta(out.run_id)["calls_used"] <= out.plan.cap
    e.adapter_kw = {"calls_per_turn": 2}
    again = e.run(resume=out.run_id, yes=True)
    assert again.status == "done" and again.run_id == out.run_id
    lines = e.lines(out.run_id)
    assert {x["case_id"] for x in lines} == set(BIG)
    assert not first & set(e.adapters[-1].started)  # scored cases did not run again
    assert len(e.env.runs.meta(out.run_id)["segments"]) == 2


def test_daily_limit_pauses(e: Env, config):
    limit = config.settings.limits.per_day.max_llm_calls
    e.env.ledger.add(llm_calls=limit - 7)  # the plan of 6 calls fits
    e.adapter_kw = {"calls_per_turn": 4, "daily_max": limit}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "paused_daily_limit" and "tomorrow" in out.message
    assert len(e.lines(out.run_id)) == 1  # the second case runs again on --resume


def test_plan_that_does_not_fit_today_does_not_start(e: Env, config):
    e.env.ledger.add(llm_calls=config.settings.limits.per_day.max_llm_calls)
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "not_started" and not e.adapters


def test_three_rate_limits_in_a_row_pause(e: Env):
    def busy(case_id: str, text: str, n: int) -> TurnRecord:
        return TurnRecord(text=text, answer="busy", error_type="llm_rate_limited")

    e.adapter_kw = {"script": busy}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "paused_rate_limit"
    assert e.sleeps == [60.0, 60.0] and e.lines(out.run_id) == []


def test_one_rate_limit_waits_and_retries(e: Env):
    state = {"n": 0}

    def flaky(case_id: str, text: str, n: int) -> TurnRecord:
        state["n"] += 1
        if state["n"] == 1:
            return TurnRecord(text=text, answer="busy", error_type="llm_rate_limited")
        return perfect_turn(case_id, text)

    e.adapter_kw = {"script": flaky}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "done" and e.sleeps == [60.0]
    assert all(x["passed"] for x in e.lines(out.run_id))


def test_data_change_during_the_run_rescores(e: Env):
    def change() -> None:
        e.runner.changed = True

    e.adapter_kw = {"on_send": change}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "data_changed"
    assert e.runner.runs == 2  # the two snapshots ran again, with no Gemini call
    raw = (e.env.runs.run_dir(out.run_id) / "results.jsonl").read_text().splitlines()
    assert len(raw) == 4 and "rescored_at" in json.loads(raw[-1])
    assert e.env.runs.meta(out.run_id)["status"] == "data_changed"


def test_large_data_change_is_not_refreshed_without_a_yes(e: Env):
    def change() -> None:
        e.runner.changed = True

    e.adapter_kw = {"on_send": change}
    e.runner.dry_run = lambda sql, params: 2_000_000_000  # type: ignore[method-assign]
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "data_changed" and e.runner.runs == 0
    assert "opsfleet-eval snapshot" in out.message and "--rescore" in out.message


def test_wrong_answer_fails_with_reason_and_report_lists_it(e: Env):
    def wrong(case_id: str, text: str, n: int) -> TurnRecord:
        turn = perfect_turn(case_id, text)
        turn.queries[0].rows = [
            [r[0], 1.0] if len(r) > 1 else [1.0] for r in turn.queries[0].rows
        ]
        turn.answer = "Revenue was 1.0."
        return turn

    e.adapter_kw = {"script": wrong}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    lines = e.lines(out.run_id)
    assert not any(x["passed"] for x in lines)
    tm = next(x for x in lines if x["case_id"] == "TM-01")
    assert tm["scorers"]["execution_match"]["reason"] == "wrong_value"
    assert tm["scorers"]["answer_facts"]["reason"] == "fact_missing"
    report = out.report_path.read_text()
    assert "### TM-01" in report and "wrong_value" in report


def test_judge_runs_through_the_cap_and_human_labels(e: Env):
    e.adapter_kw = {"calls_per_turn": 1}
    out = e.run(set_name="safety", case_ids=["OT-01", "SP-01"])
    assert out.plan.judge_calls == 2
    assert e.judge_llm.calls == 2
    items = e.env.runs.judge_items(out.run_id)
    assert {i["case_id"] for i in items} == {"OT-01", "SP-01"}
    answers = iter(["p", "f"])
    count = review(
        e.env.runs, out.run_id, 10, ask=lambda _: next(answers), show=lambda _: None
    )
    assert count == 2
    text = make_report(e.env, out.run_id).read_text()
    assert "Human labels: 2." in text and "fewer than 20 human labels" in text


def test_cap_in_the_judge_step_pauses_and_resume_scores_only(e: Env):
    e.adapter_kw = {"calls_per_turn": 2}  # 2 agent calls per case; the plan has 1
    out = e.run(set_name="safety", case_ids=["OT-01", "SP-01"])
    assert out.status == "paused_cap"
    paused = [c for c in ("OT-01", "SP-01") if e.env.runs.record(out.run_id, c)]
    scored = {x["case_id"] for x in e.lines(out.run_id)}
    assert set(paused) - scored  # one case has a record but no result yet
    again = e.run(resume=out.run_id, yes=True)
    assert again.status == "done"
    assert not set(e.adapters[-1].started) & set(paused)  # no second agent run
    assert e.judge_llm.calls == 2  # one judge call for each case, none repeated
    items = e.env.runs.judge_items(out.run_id)
    assert len([i for i in items if not i.get("error")]) == 2
    assert e.env.runs.meta(out.run_id)["actual"]["judge_calls"] == 2
    assert all(x["passed"] for x in e.lines(out.run_id))


def test_judge_failure_fails_the_case(e: Env):
    e.judge_llm.fail = {"J-TOPIC"}
    out = e.run(set_name="safety", case_ids=["OT-01"])
    line = e.lines(out.run_id)[0]
    assert not line["passed"]
    assert line["scorers"]["T1:judge_safety"]["reason"] == "judge_failed"
    assert line["scorers"]["T1:H-NOQ"]["pass"]


def test_missing_report_library_is_capability_missing(e: Env):
    from opsfleet_agent.evals.fixtures import CapabilityMissing

    class NoReports(FakeAdapter):
        def start(self, *a: Any, **kw: Any):
            raise CapabilityMissing("no reports table (spec 32)")

    e.env.adapter_factory = lambda cap, model, throttle: NoReports(cap, e.env.ledger)
    out = e.run(set_name="safety", case_ids=["DEL-07"])
    line = e.lines(out.run_id)[0]
    assert (
        not line["passed"] and line["scorers"]["run"]["reason"] == "capability_missing"
    )


def test_compare_two_runs(e: Env):
    from opsfleet_agent.evals.compare import compare_runs

    a = e.run(set_name="accuracy", case_ids=SMALL)

    def wrong_tm(case_id: str, text: str, n: int) -> TurnRecord:
        turn = perfect_turn(case_id, text)
        if case_id == "TM-01":
            turn.queries = []
        return turn

    e.adapter_kw = {"script": wrong_tm}
    e.env.now = lambda: NOW + dt.timedelta(minutes=5)
    b = e.run(set_name="accuracy", case_ids=SMALL)
    runs = e.env.runs
    text = compare_runs(
        runs.meta(a.run_id), e.lines(a.run_id), runs.meta(b.run_id), e.lines(b.run_id)
    )
    assert "Pass to fail (A passed, B failed): TM-01." in text
    assert "only A passed 1 (c)" in text
    report = b.report_path.read_text()
    assert (
        "## Comparison with the last run of the same set" in report
        and "TM-01" in report
    )


# --- Outside errors (spec 23, B2.9; spec 50 round 1) ---------------------------------------------
def _outside_error(text: str, temporary: bool = True) -> TurnRecord:
    attrs = {"error_type": "llm_error", "error_detail": "ServerError: 503 UNAVAILABLE."}
    if not temporary:
        attrs = {"error_type": "llm_error", "error_detail": "API key not valid"}
    return TurnRecord(
        text=text,
        answer="The AI service is not available now.",
        error_type="llm_error",
        spans=[{"name": "llm.route", "kind": "llm", "attributes": attrs}],
    )


def test_outside_error_runs_the_case_again_one_time(e: Env):
    seen: dict[str, int] = {}

    def flaky(case_id: str, text: str, n: int) -> TurnRecord:
        seen[case_id] = seen.get(case_id, 0) + 1
        if case_id == "TM-01" and seen[case_id] == 1:
            return _outside_error(text)
        return perfect_turn(case_id, text)

    e.adapter_kw = {"script": flaky}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "done" and e.sleeps == [60.0]
    tm = next(x for x in e.lines(out.run_id) if x["case_id"] == "TM-01")
    assert tm["status"] == "scored" and tm["passed"]
    assert tm["external_retries"] == ["llm_error: ServerError: 503 UNAVAILABLE."]
    assert e.env.runs.record(out.run_id, "TM-01").external_retries
    assert e.adapters[0].started.count("TM-01") == 2


def test_report_counts_the_calls_of_discarded_runs(e: Env):
    """Round 1 of spec 50: the report counted only the calls of the kept records."""
    seen: dict[str, int] = {}

    def flaky(case_id: str, text: str, n: int) -> TurnRecord:
        seen[case_id] = seen.get(case_id, 0) + 1
        if case_id == "TM-01" and seen[case_id] == 1:
            return _outside_error(text)
        if case_id == "PP-01" and seen[case_id] == 1:
            return TurnRecord(text=text, answer="busy", error_type="llm_rate_limited")
        return perfect_turn(case_id, text)

    e.adapter_kw = {"script": flaky}  # 2 calls for each turn
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert out.status == "done"
    lines = {x["case_id"]: x for x in e.lines(out.run_id)}
    assert lines["TM-01"]["discarded"]["llm_calls"] == 2
    assert lines["PP-01"]["discarded"]["llm_calls"] == 2
    assert e.env.runs.record(out.run_id, "TM-01").discarded["llm_calls"] == 2
    meta = e.env.runs.meta(out.run_id)
    assert meta["actual"]["discarded_calls"] == 4
    assert meta["actual"]["agent_calls"] == 8 == meta["calls_used"]
    report = out.report_path.read_text()
    assert "| Gemini calls, agent | " in report and "| 8 |" in report
    assert "in discarded runs (in the line above) | – | 4 |" in report


def test_second_outside_error_is_shown_but_left_out_of_the_rates(e: Env):
    def down(case_id: str, text: str, n: int) -> TurnRecord:
        return (
            _outside_error(text) if case_id == "TM-01" else perfect_turn(case_id, text)
        )

    e.adapter_kw = {"script": down}
    out = e.run(set_name="accuracy", case_ids=SMALL)
    assert e.sleeps == [60.0]  # one more run only
    tm = next(x for x in e.lines(out.run_id) if x["case_id"] == "TM-01")
    assert tm["status"] == "external_error" and "503" in tm["external_error"]
    from opsfleet_agent.evals.metrics import compute_metrics

    values = compute_metrics(e.lines(out.run_id)).values
    assert values["cases"] == 1 and values["case_pass_rate"] == 1.0
    assert values["external_errors"] == 1 and values["external_retries"] == 1
    report = out.report_path.read_text()
    assert (
        "## Cases with only outside errors" in report and "- TM-01: llm_error" in report
    )
    assert "Cases with only outside errors: 1 (not in the rates)" in report


def test_error_that_is_not_temporary_is_a_failure_without_a_second_run(e: Env):
    def bad_key(case_id: str, text: str, n: int) -> TurnRecord:
        return _outside_error(text, temporary=False)

    e.adapter_kw = {"script": bad_key}
    out = e.run(set_name="accuracy", case_ids=["TM-01"])
    line = e.lines(out.run_id)[0]
    assert e.sleeps == [] and line["status"] == "scored" and not line["passed"]


def test_safety_gate_is_not_complete_with_an_outside_error(e: Env):
    e.adapter_kw = {
        "script": lambda case_id, text, n: _outside_error(text),
        "calls_per_turn": 1,
    }
    out = e.run(set_name="safety", case_ids=["OT-01"])
    line = e.lines(out.run_id)[0]
    assert line["status"] == "external_error"
    assert (
        e.judge_llm.calls == 0
    )  # the judge does not judge the text of an outside error
    assert "Gate: **not complete (1 cases have only outside errors)**" in (
        out.report_path.read_text()
    )


class _FlakyJudge(FakeJudgeLLM):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def generate(self, budget: Any, **kw: Any):
        if self.failures:
            from opsfleet_agent.errors import ErrorType
            from opsfleet_agent.resilience.policies import ServiceFailure

            self.failures -= 1
            budget.charge_llm()
            raise ServiceFailure(
                ErrorType.LLM_ERROR, "The AI service is not available now.", "503",
                temporary=True,
            )  # fmt: skip
        return super().generate(budget, **kw)


def test_judge_outside_error_gets_one_more_call(e: Env):
    e.judge_llm = _FlakyJudge(failures=1)
    e.adapter_kw = {"calls_per_turn": 1}
    out = e.run(set_name="safety", case_ids=["OT-01"])
    line = e.lines(out.run_id)[0]
    assert line["status"] == "scored" and line["passed"]
    assert e.sleeps == [60.0] and e.judge_llm.calls == 1


def test_judge_outside_error_twice_leaves_a_passing_case_out_of_the_rates(e: Env):
    e.judge_llm = _FlakyJudge(failures=5)
    e.adapter_kw = {"calls_per_turn": 1}
    out = e.run(set_name="safety", case_ids=["OT-01"])
    line = e.lines(out.run_id)[0]
    assert line["status"] == "external_error" and line["external_error"].startswith(
        "judge"
    )
    assert line["scorers"]["T1:H-NOQ"]["pass"]  # the code checks still show
