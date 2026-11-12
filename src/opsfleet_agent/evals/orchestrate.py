"""Put the parts of a run together: cases, snapshots, plan, approval, run, report (spec 23, B2.1).

The CLI calls these functions. The tests call them with a fake agent, a fake judge model and a fake
BigQuery runner, so no test uses the network.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsfleet_agent.config import PROJECT_ROOT, AppConfig
from opsfleet_agent.evals.agent_adapter import AgentAdapter
from opsfleet_agent.evals.cases import Case, CaseSet, load_set
from opsfleet_agent.evals.compare import compare_runs
from opsfleet_agent.evals.metrics import compute_metrics, judge_agreement
from opsfleet_agent.evals.plan import RunPlan, approve, make_plan, render_plan
from opsfleet_agent.evals.recorder import (
    CallCap,
    RecordingGeminiClient,
    Throttle,
    TurnSink,
)
from opsfleet_agent.evals.report import build_report, write_report
from opsfleet_agent.evals.runner import RunState, rescore, run_cases
from opsfleet_agent.evals.score import (
    ScoringContext,
    load_prompt_texts,
    load_secret_values,
)
from opsfleet_agent.evals.scorers.judge import Judge, load_rubric
from opsfleet_agent.evals.snapshot import (
    QueryRunner,
    RefreshReport,
    SnapshotStore,
    refresh,
)
from opsfleet_agent.evals.storage import (
    CallStats,
    RunStore,
    file_hash,
    git_state,
    make_run_id,
)
from opsfleet_agent.limits import DailyLedger, TurnBudget

TABLE_MIN_BILL = (
    10 * 1024 * 1024
)  # BigQuery bills at least 10 MB for each table in a query
BASELINE_DIR = PROJECT_ROOT / "evals" / "baseline"


@dataclass
class EvalEnv:
    """The services of the evaluation. Tests replace the factories with fakes."""

    config: AppConfig
    ledger: DailyLedger
    runs: RunStore
    snapshots: SnapshotStore
    stats: CallStats
    reference_runner: Callable[[], QueryRunner | None]
    adapter_factory: Callable[[CallCap, str, Throttle], AgentAdapter]
    judge_llm_factory: Callable[[], Any]
    ask: Callable[[str], str] | None = input
    log: Callable[[str], None] = print
    sleep: Callable[[float], None] | None = None
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))


@dataclass
class RunOutcome:
    status: str
    message: str
    run_id: str | None = None
    plan: RunPlan | None = None
    report_path: Path | None = None


# ------------------------------------------------------------------------------------------------
# Cases and snapshots
# ------------------------------------------------------------------------------------------------


def select_cases(
    env: EvalEnv, set_name: str, case_ids: list[str] | None
) -> tuple[CaseSet, list[Case], dict[str, str]]:
    """The cases to run and the skipped ones (draft, needs_review, or no snapshot)."""
    case_set = load_set(set_name)
    env.snapshots.import_baseline(BASELINE_DIR / "snapshots.json")
    selected = case_set.select(case_ids)
    run, skipped = [], {}
    for case in selected:
        flag = env.snapshots.needs_review(case.id)
        if case.status != "ready":
            skipped[case.id] = case.status
        elif flag:
            skipped[case.id] = f"needs_review: {flag}"
        else:
            run.append(case)
    return case_set, run, skipped


def billed_estimate(case: Case, bytes_estimated: int) -> int:
    """BigQuery bills at least 10 MB for each table of a query."""
    sql = " ".join(ref.sql for ref in case.all_references().values())
    tables = set(re.findall(r"thelook_ecommerce\.(\w+)", sql))
    return max(bytes_estimated, TABLE_MIN_BILL * max(1, len(tables)))


def plan_snapshots(
    env: EvalEnv, cases: list[Case], force: bool = False
) -> tuple[RefreshReport | None, int]:
    """The free dry runs of the snapshots that need a refresh, and the estimate of billed bytes."""
    runner = env.reference_runner()
    if runner is None:
        return None, 0
    report = refresh(
        cases,
        env.config.users,
        runner,
        env.snapshots,
        max_bytes=env.config.settings.eval.snapshot_max_bytes_billed,
        now=env.now(),
        force=force,
        dry_only=True,
    )
    by_id = {c.id: c for c in cases}
    billed = sum(
        billed_estimate(by_id[i.case_id], i.bytes_estimated)
        for i in report.items
        if i.action == "planned"
    )
    return report, billed


def refresh_snapshots(
    env: EvalEnv, cases: list[Case], force: bool = False
) -> RefreshReport:
    runner = env.reference_runner()
    if runner is None:
        return RefreshReport()
    return refresh(
        cases,
        env.config.users,
        runner,
        env.snapshots,
        max_bytes=env.config.settings.eval.snapshot_max_bytes_billed,
        now=env.now(),
        force=force,
        on_bytes=lambda b: env.ledger.add(bq_queries=1, bq_bytes_billed=b),
    )


def table_times(env: EvalEnv) -> dict[str, str]:
    runner = env.reference_runner()
    return runner.table_times() if runner is not None else {}


# ------------------------------------------------------------------------------------------------
# Plan, run, report
# ------------------------------------------------------------------------------------------------


def hashes(case_set: CaseSet) -> dict[str, str]:
    prompts = PROJECT_ROOT / "src" / "opsfleet_agent" / "agent" / "prompts"
    return {
        "set": case_set.sha256,
        "prompts": file_hash(list(prompts.glob("*"))) if prompts.exists() else "",
        "rubrics": file_hash(list((PROJECT_ROOT / "evals" / "rubrics").glob("*.md"))),
        "settings": file_hash([PROJECT_ROOT / "config" / "settings.yaml"]),
    }


def build_plan(
    env: EvalEnv,
    set_name: str,
    cases: list[Case],
    skipped: dict[str, str],
    model: str,
    judge_enabled: bool,
    snapshot_bytes: int,
) -> RunPlan:
    return make_plan(
        set_name=set_name,
        cases=cases,
        skipped=skipped,
        settings=env.config.settings,
        agent_model=model,
        judge_enabled=judge_enabled,
        ledger_today=env.ledger.today(),
        stats=env.stats,
        snapshot_bytes=snapshot_bytes,
    )


def _judge(env: EvalEnv, cap: CallCap, throttle: Throttle) -> Judge:
    settings = env.config.settings
    llm = RecordingGeminiClient(
        env.judge_llm_factory(),
        TurnSink(),
        cap,
        throttle,
        model=settings.eval.judge_model,
    )
    return Judge(
        llm,
        new_budget=lambda: TurnBudget(settings.limits, env.ledger),
        temperature=settings.eval.judge_temperature,
    )


def _scoring(env: EvalEnv, judge: Judge | None, run_id: str) -> ScoringContext:
    return ScoringContext(
        users=env.config.users,
        store=env.snapshots,
        accuracy_store=env.snapshots,
        judge=judge,
        rubrics={n: load_rubric(n) for n in ("answer", "report", "safety")},
        prompt_texts=load_prompt_texts(),
        secret_values=load_secret_values(),
        on_judge=lambda item: env.runs.append_judge(run_id, item),
    )


def _actual(env: EvalEnv, run_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    lines = env.runs.results(run_id)
    turns = [t for x in lines for t in x["turns"]]
    judged = env.runs.judge_items(run_id)
    ev = env.config.settings.eval
    agent_calls = sum(t["llm_calls"] for t in turns)
    judge_calls = sum(i.get("calls", 0) for i in judged)
    tin = sum(t["tokens_in"] for t in turns) + sum(
        i.get("tokens_in", 0) for i in judged
    )
    tout = sum(t["tokens_out"] for t in turns) + sum(
        i.get("tokens_out", 0) for i in judged
    )
    price = ev.prices.get(
        meta.get("agent_model", ""), next(iter(ev.prices.values()), None)
    )
    cost = (
        (tin * price.input_per_m + tout * price.output_per_m) / 1e6 if price else None
    )
    return {
        "agent_calls": agent_calls,
        "judge_calls": judge_calls,
        "tokens_in": tin,
        "tokens_out": tout,
        "bq_bytes": sum(t["bq_bytes_billed"] for t in turns),
        "snapshot_bytes": meta.get("actual", {}).get("snapshot_bytes", 0),
        "cost_usd": round(cost, 4) if cost is not None else None,
        "scored_cases": len(lines),
    }


def make_report(env: EvalEnv, run_id: str) -> Path:
    meta = env.runs.meta(run_id)
    case_set = load_set(meta["set"])
    by_id = case_set.by_id()
    lines = env.runs.results(run_id)
    metrics = compute_metrics(lines)
    labels: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for other in env.runs.all_run_ids():  # pool the labels of runs with the same judge
        other_meta = env.runs.meta(other)
        same = other_meta.get("judge_model") == meta.get(
            "judge_model"
        ) and other_meta.get("hashes", {}).get("rubrics") == meta.get("hashes", {}).get(
            "rubrics"
        )
        if same:
            labels += env.runs.labels(other)
            items += env.runs.judge_items(other)
    agreement = judge_agreement(labels, items)

    def expected_rows(case_id: str) -> tuple[list[str], list[list[Any]]] | None:
        snap = env.snapshots.current(case_id)
        return (snap.columns, snap.rows) if snap else None

    def agent_rows(case_id: str) -> tuple[list[str], list[list[Any]]] | None:
        record = env.runs.record(run_id, case_id)
        if record is None or not record.turns:
            return None
        queries = [q for q in record.turns[-1].queries if q.error_type is None]
        return (queries[-1].columns, queries[-1].rows) if queries else None

    previous = env.runs.last_run_of_set(meta["set"], before=run_id)
    comparison = None
    if previous:
        comparison = compare_runs(
            env.runs.meta(previous),
            env.runs.results(previous),
            meta,
            lines,
            heading=False,
        )
    text = build_report(
        meta,
        lines,
        metrics,
        agreement,
        meta.get("skipped", {}),
        expected_rows=expected_rows,
        agent_rows=agent_rows,
        questions={c.id: " / ".join(c.turn_texts) for c in by_id.values()},
        comparison=comparison,
    )
    return write_report(env.runs.run_dir(run_id) / "report.md", text)


def _new_meta(
    env: EvalEnv,
    run_id: str,
    case_set: CaseSet,
    plan: RunPlan,
    approval: str,
    model: str,
) -> dict[str, Any]:
    commit, dirty = git_state()
    snaps = [env.snapshots.current(c) for c in plan.case_ids]
    dates = sorted(s.created_at for s in snaps if s is not None)
    return {
        "run_id": run_id,
        "created_at": env.now().isoformat(timespec="seconds"),
        "set": case_set.name,
        "case_ids": plan.case_ids,
        "skipped": plan.skipped,
        "agent_model": model,
        "judge_model": plan.judge_model,
        "judge_enabled": plan.judge_model is not None,
        "git_commit": commit,
        "git_dirty": dirty,
        "hashes": hashes(case_set),
        "plan": plan.to_json(),
        "approval": approval,
        "segments": [],
        "status": "running",
        "table_times": table_times(env),
        "snapshot_date": dates[0][:10] if dates else None,
        "keep": False,
        "actual": {"snapshot_bytes": 0},
    }


MAX_AUTO_REFRESH_BYTES = (
    1_000_000_000  # the owner's rule: up to 1 GB runs without a "yes"
)


def _check_data_change(
    env: EvalEnv, state: RunState, meta: dict[str, Any], cases: list[Case]
) -> tuple[str, str]:
    """After the last case: if the tables changed since the run started, refresh the snapshots
    and score all stored records again (no Gemini call). Above 1 GB, only say what to do."""
    end_times = table_times(env)
    if not end_times or end_times == meta.get("table_times"):
        return "done", ""
    _, billed = plan_snapshots(env, cases)
    meta["table_times_end"] = end_times
    if billed > MAX_AUTO_REFRESH_BYTES:
        return "data_changed", (
            f" The data changed during the run. The snapshot refresh needs about"
            f" {billed / 1e6:.0f} MB of BigQuery. Run: uv run opsfleet-eval snapshot --set"
            f" {meta['set']}, then: uv run opsfleet-eval report --run {meta['run_id']} --rescore"
        )
    again = refresh_snapshots(env, cases)
    actual = meta["actual"]
    actual["snapshot_bytes"] = actual.get("snapshot_bytes", 0) + again.bytes_billed
    rescore(state, cases)
    meta["table_times"] = end_times
    return "data_changed", (
        " The data changed during the run: the snapshots are new, and the cases are"
        " scored again."
    )


def execute_run(
    env: EvalEnv,
    *,
    set_name: str | None = None,
    case_ids: list[str] | None = None,
    model: str | None = None,
    judge: bool = True,
    yes: bool = False,
    resume: str | None = None,
) -> RunOutcome:
    settings = env.config.settings
    env.runs.cleanup(settings.eval.keep_runs_days, env.now())
    meta: dict[str, Any] | None = None
    if resume:
        meta = env.runs.meta(resume)
        set_name, case_ids = meta["set"], meta["case_ids"]
        model, judge = meta["agent_model"], meta["judge_enabled"]
    assert set_name is not None
    model = model or settings.models.default
    case_set, cases, skipped = select_cases(env, set_name, case_ids)
    judge = judge and case_set.judge
    all_cases = list(cases)
    if meta is not None:
        done = {r["case_id"] for r in env.runs.results(resume or "")}
        cases = [c for c in cases if c.id not in done]
    _, snapshot_bytes = plan_snapshots(env, cases)
    plan = build_plan(env, case_set.name, cases, skipped, model, judge, snapshot_bytes)
    env.log(render_plan(plan))
    if not plan.fits_today:
        return RunOutcome(
            "not_started", "The run does not fit the daily budget.", plan=plan
        )
    approval = approve(plan, yes, env.ask)
    if approval is None:
        return RunOutcome("not_started", "Stop. Nothing runs.", plan=plan)

    refreshed = refresh_snapshots(env, cases)
    review = {i.case_id for i in refreshed.items if i.action == "needs_review"}
    for case_id in review:
        skipped[case_id] = "needs_review: the result shape changed"
    cases = [c for c in cases if c.id not in review]

    if meta is None:
        run_id = make_run_id(env.now(), case_set.name, model, git_state()[0])
        meta = _new_meta(env, run_id, case_set, plan, approval, model)
        env.runs.create(run_id, meta)
    run_id = meta["run_id"]
    meta["skipped"] = {**meta.get("skipped", {}), **skipped}
    meta["actual"]["snapshot_bytes"] = (
        meta["actual"].get("snapshot_bytes", 0) + refreshed.bytes_billed
    )
    meta["segments"].append(
        {
            "started_at": env.now().isoformat(timespec="seconds"),
            "plan": plan.to_json(),
            "approval": approval,
        }
    )
    meta["status"] = "running"
    env.runs.save_meta(run_id, meta)

    cap = CallCap(plan.cap)
    throttle = Throttle(settings.eval.call_interval_s)
    adapter = env.adapter_factory(cap, model, throttle)
    judge_obj = _judge(env, cap, throttle) if judge else None
    state = RunState(
        run_id=run_id,
        store=env.runs,
        adapter=adapter,
        scoring=_scoring(env, judge_obj, run_id),
        cap=cap,
        ledger=env.ledger,
        daily_max_calls=settings.limits.per_day.max_llm_calls,
        set_name=case_set.name,
        rate_limit_wait_s=settings.eval.rate_limit_wait_s,
        external_wait_s=settings.eval.rate_limit_wait_s,
        log=env.log,
        stats=env.stats,
    )
    if env.sleep is not None:
        state.sleep = env.sleep
    status, message = run_cases(state, cases)
    env.stats.save()
    meta = {
        **env.runs.meta(run_id),
        **{k: meta[k] for k in ("skipped", "segments", "actual")},
    }
    if status == "done":
        status, note = _check_data_change(env, state, meta, all_cases)
        message += note
    meta["status"] = status
    meta["actual"] = {**meta["actual"], **_actual(env, run_id, meta)}
    meta["calls_used"] = cap.used
    env.runs.save_meta(run_id, meta)
    path = make_report(env, run_id)
    if status in ("done", "data_changed"):
        metrics = compute_metrics(env.runs.results(run_id)).values
        env.runs.append_index(
            {
                "run_id": run_id,
                "set": case_set.name,
                "status": status,
                "agent_model": model,
                "judge_model": meta.get("judge_model"),
                "git_commit": meta.get("git_commit"),
                "cases": metrics.get("cases"),
                "case_pass_rate": metrics.get("case_pass_rate"),
                "execution_match_rate": metrics.get("execution_match_rate"),
                "safety_pass_rate": metrics.get("safety_pass_rate"),
            }
        )
    return RunOutcome(status, message, run_id=run_id, plan=plan, report_path=path)


def plan_only(
    env: EvalEnv,
    set_name: str,
    case_ids: list[str] | None,
    model: str | None,
    judge: bool,
) -> RunPlan:
    case_set, cases, skipped = select_cases(env, set_name, case_ids)
    _, snapshot_bytes = plan_snapshots(env, cases)
    return build_plan(
        env,
        case_set.name,
        cases,
        skipped,
        model or env.config.settings.models.default,
        judge and case_set.judge,
        snapshot_bytes,
    )


def snapshot_command(
    env: EvalEnv,
    set_name: str,
    case_ids: list[str] | None,
    force: bool,
    yes: bool,
    export: bool,
) -> RefreshReport | None:
    """`opsfleet-eval snapshot`: free dry runs, the estimate, a "yes" above 1 GB, then the refresh."""
    case_set = load_set(set_name)
    cases = case_set.select(case_ids)
    env.snapshots.import_baseline(BASELINE_DIR / "snapshots.json")
    planned, billed = plan_snapshots(env, cases, force=force)
    if planned is None:
        env.log("No BigQuery runner. Nothing to do.")
        return None
    todo = [i for i in planned.items if i.action == "planned"]
    errors = [i for i in planned.items if i.action == "error"]
    env.log(
        f"Snapshots: {len(todo)} to refresh, {len(planned.items) - len(todo) - len(errors)} up to"
        f" date, {len(errors)} errors. BigQuery: about {billed / 1e6:.0f} MB billed"
        " (at least 10 MB for each table of a query). Gemini calls: 0."
    )
    for item in errors:
        env.log(f"  {item.case_id}/{item.ref}: {item.reason}")
    if todo and billed > 1_000_000_000 and not yes:
        answer = env.ask('Type "yes" to run the queries: ') if env.ask else ""
        if answer.strip().lower() != "yes":
            env.log("Stop. Nothing runs.")
            return planned
    report = refresh_snapshots(env, cases, force=force) if todo else planned
    for item in report.items:
        if item.action in ("created", "refreshed", "needs_review", "error"):
            env.log(
                f"  {item.case_id}/{item.ref} ({item.user}): {item.action}, "
                f"{item.bytes_billed / 1e6:.1f} MB billed. {item.reason}"
            )
    env.log(f"Billed in total: {report.bytes_billed / 1e6:.1f} MB.")
    if export:
        count = env.snapshots.export(
            [c.id for c in case_set.cases], BASELINE_DIR / "snapshots.json"
        )
        env.log(f"Exported {count} snapshots to evals/baseline/snapshots.json.")
    return report
