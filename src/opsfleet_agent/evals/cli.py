"""`opsfleet-eval`: plan, run, snapshot, review, report, compare (spec 23, B3).

Every command that can call Gemini first shows the plan with the call count and the cost estimate.
A run with more than `eval.ask_above_calls` planned calls needs a typed "yes" or `--yes`.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from opsfleet_agent.config import load_config
from opsfleet_agent.errors import ConfigError

app = typer.Typer(
    add_completion=False,
    help="Evaluation of the assistant: plan, run, snapshot, review, report, compare.",
)
console = Console()

SET_HELP = "Set name: accuracy, safety, safety_long, owner_session, smoke"
CASES_HELP = "Comma-separated case IDs, for example PP-01,TM-01"


def _ids(text: str | None) -> list[str] | None:
    return [i.strip() for i in text.split(",") if i.strip()] if text else None


def _env(need_agent: bool = False):
    """The real environment: config, daily ledger, stores, BigQuery and the Gemini clients."""
    from opsfleet_agent.evals.orchestrate import EvalEnv
    from opsfleet_agent.evals.snapshot import BigQueryReferenceRunner, SnapshotStore
    from opsfleet_agent.evals.storage import CallStats, RunStore
    from opsfleet_agent.limits import DailyLedger
    from opsfleet_agent.store.db import connect

    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    settings = config.settings
    conn = connect(settings.data_path("app.db"))
    ledger = DailyLedger(conn, settings.limits)
    runner_cache: dict[str, object] = {}

    def reference_runner():
        if "r" not in runner_cache:
            runner_cache["r"] = None
            if not config.secrets.google_cloud_project:
                console.print(
                    "[yellow]GOOGLE_CLOUD_PROJECT is not set: no snapshot check.[/yellow]"
                )
                return None
            from google.cloud import bigquery

            client = bigquery.Client(project=config.secrets.google_cloud_project)
            runner_cache["r"] = BigQueryReferenceRunner(
                client,
                settings.bigquery.dataset,
                settings.bigquery.tables,
                settings.eval.snapshot_max_bytes_billed,
                settings.bigquery.location,
            )
        return runner_cache["r"]

    def adapter_factory(cap, model, throttle):
        from opsfleet_agent.evals.agent_adapter import SessionAdapter

        return SessionAdapter(
            config, cap, model=model, real_ledger=ledger, throttle=throttle
        )

    def judge_llm_factory():
        from opsfleet_agent.llm import GeminiClient

        # No hidden retries and no fallback model (spec 36): the cap and the throttle see each
        # request, and the judge is always the same model.
        models = settings.models.model_copy(
            update={
                "default": settings.eval.judge_model,
                "max_retries": 0,
                "fallback": None,
            }
        )
        return GeminiClient(models, config.secrets.google_api_key)

    return EvalEnv(
        config=config,
        ledger=ledger,
        runs=RunStore(settings.data_path("eval", "runs")),
        snapshots=SnapshotStore(settings.data_path("eval", "snapshots")),
        stats=CallStats(settings.data_path("eval", "call_stats.json")),
        reference_runner=reference_runner,
        adapter_factory=adapter_factory,
        judge_llm_factory=judge_llm_factory,
        ask=lambda prompt: console.input(prompt) if sys.stdin.isatty() else "",
        log=console.print,
    )


@app.command()
def plan(
    set_name: str = typer.Option(..., "--set", help=SET_HELP),
    cases: str | None = typer.Option(None, "--cases", help=CASES_HELP),
    model: str | None = typer.Option(None, "--model", help="Agent model for the run"),
    no_judge: bool = typer.Option(
        False, "--no-judge", help="Only the free code checks"
    ),
) -> None:
    """Show the plan of a run. No Gemini call. BigQuery: only free dry runs."""
    from opsfleet_agent.evals.orchestrate import plan_only
    from opsfleet_agent.evals.plan import render_plan

    env = _env()
    console.print(
        render_plan(plan_only(env, set_name, _ids(cases), model, not no_judge))
    )


@app.command()
def run(
    set_name: str | None = typer.Option(None, "--set", help=SET_HELP),
    cases: str | None = typer.Option(None, "--cases", help=CASES_HELP),
    model: str | None = typer.Option(
        None, "--model", help="Agent model for the run (spec 51)"
    ),
    no_judge: bool = typer.Option(
        False, "--no-judge", help="Only the free code checks"
    ),
    yes: bool = typer.Option(
        False, "--yes", help="The owner approved this plan in the conversation"
    ),
    resume: str | None = typer.Option(None, "--resume", help="Run ID of a paused run"),
) -> None:
    """Run a set on the agent: plan, "yes", run, score, report."""
    from opsfleet_agent.evals.orchestrate import execute_run

    if not set_name and not resume:
        console.print("[red]Give --set or --resume.[/red]")
        raise typer.Exit(code=2)
    outcome = execute_run(
        _env(),
        set_name=set_name,
        case_ids=_ids(cases),
        model=model,
        judge=not no_judge,
        yes=yes,
        resume=resume,
    )
    console.print(outcome.message)
    if outcome.report_path:
        console.print(f"Report: {outcome.report_path}")
    if outcome.status == "not_started":
        raise typer.Exit(code=1)


@app.command()
def snapshot(
    set_name: str = typer.Option("accuracy", "--set", help=SET_HELP),
    cases: str | None = typer.Option(None, "--case", "--cases", help=CASES_HELP),
    force: bool = typer.Option(False, "--force", help="Run all reference SQL again"),
    yes: bool = typer.Option(False, "--yes", help="Approve more than 1 GB of BigQuery"),
    export: bool = typer.Option(
        False, "--export", help="Write the snapshots to evals/baseline/snapshots.json"
    ),
) -> None:
    """Refresh the reference snapshots (spec 21). No Gemini call."""
    from opsfleet_agent.evals.orchestrate import snapshot_command

    snapshot_command(_env(), set_name, _ids(cases), force, yes, export)


@app.command()
def review(
    run_id: str = typer.Option(..., "--run", help="Run ID"),
    n: int | None = typer.Option(
        None, "--n", help="Number of items (default: eval.human_sample)"
    ),
    reviewer: str = typer.Option("owner", "--reviewer"),
) -> None:
    """Label judged answers blind: pass or fail. The report then shows the judge agreement."""
    from opsfleet_agent.evals.orchestrate import make_report
    from opsfleet_agent.evals.review import review as do_review

    env = _env()
    count = do_review(
        env.runs,
        run_id,
        n or env.config.settings.eval.human_sample,
        ask=console.input,
        show=console.print,
        reviewer=reviewer,
    )
    console.print(f"{count} new labels. Report: {make_report(env, run_id)}")


@app.command()
def report(
    run_id: str = typer.Option(..., "--run", help="Run ID"),
    rescore_run: bool = typer.Option(
        False,
        "--rescore",
        help="Score the records again with the current snapshots (no Gemini)",
    ),
) -> None:
    """Write report.md of a run again."""
    from opsfleet_agent.evals.cases import load_set
    from opsfleet_agent.evals.orchestrate import _scoring, make_report
    from opsfleet_agent.evals.recorder import CallCap
    from opsfleet_agent.evals.runner import RunState, rescore

    env = _env()
    if rescore_run:
        meta = env.runs.meta(run_id)
        cases = load_set(meta["set"]).select(meta["case_ids"])
        state = RunState(
            run_id=run_id,
            store=env.runs,
            adapter=None,  # type: ignore[arg-type]
            scoring=_scoring(env, None, run_id),
            cap=CallCap(0),
            ledger=env.ledger,
            daily_max_calls=0,
            set_name=meta["set"],
        )
        console.print(f"Scored again: {rescore(state, cases)} cases.")
    console.print(f"Report: {make_report(env, run_id)}")


@app.command()
def compare(run_a: str, run_b: str) -> None:
    """Compare two runs: metric changes and the cases that changed."""
    from opsfleet_agent.evals.compare import compare_runs

    env = _env()
    text = compare_runs(
        env.runs.meta(run_a),
        env.runs.results(run_a),
        env.runs.meta(run_b),
        env.runs.results(run_b),
    )
    out = env.config.settings.data_path("eval", "compare", f"{run_a}__{run_b}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    console.print(text)
    console.print(f"Saved: {out}")


def main() -> None:
    app()
