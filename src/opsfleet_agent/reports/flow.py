"""The report flow: a subgraph for the intent `report` (spec 32, B2.1).

    report_start -> report_library (list, open, export: no model call)
                 -> plan_report -> [next_query -> data_loop -> collect_query]* -> write_report
                 -> write_report (save_last: the results of the last answer, no query)
    write_report -> build_report -> save_report -> render -> END

Each report query runs through the `data_loop` subgraph of spec 31 (guard, query, checks, masker).
It starts at the guard with the planned SQL, and a failed attempt goes back to `write_sql` for a
correction. Model calls: route, plan_report, write_report, and at most one rewrite request of the
figure check. Two model calls stay reserved for the writing, so query corrections cannot use them.
A node that ends the flow early sets `turn.answer`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from opentelemetry import trace
from pydantic import ValidationError

from opsfleet_agent import capabilities
from opsfleet_agent.agent.formatting import ResultData, data_block
from opsfleet_agent.agent.nodes.common import (
    brands_text,
    end_turn,
    is_manager,
    node,
    require_bigquery,
    scope_text,
    turn_of,
    user_format,
)
from opsfleet_agent.agent.nodes.loop_control import with_step
from opsfleet_agent.agent.nodes.route import is_format_error
from opsfleet_agent.agent.nodes.write_sql import calendar_text
from opsfleet_agent.agent.periods import (
    Calendar,
    Period,
    format_day,
    format_range,
    parse_periods,
    span_of,
)
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.agent.subgraphs.data_loop import LoopOutput, build_data_loop
from opsfleet_agent.errors import AgentError, ErrorType, ReportNotFound
from opsfleet_agent.memory import result_cache
from opsfleet_agent.memory.context_builder import ModelRequest, builder_of
from opsfleet_agent.reports.build import (
    allowed_numbers,
    apply_slots,
    build_sections,
    check_slots,
    date_range_section,
    draft_slots,
    method_section,
    report_entities,
)
from opsfleet_agent.reports.charts import check_charts
from opsfleet_agent.reports.files import display_path
from opsfleet_agent.reports.library import ReportLibrary
from opsfleet_agent.reports.models import (
    FigureFixes,
    Report,
    ReportDraft,
    ReportList,
    ReportPlan,
)
from opsfleet_agent.reports.render_md import markdown_body
from opsfleet_agent.reports.text import one_line, scan_strings
from opsfleet_agent.schema import model_schema_text

LIBRARY_ACTIONS = ("list", "open", "export")
RESERVED_WRITE_CALLS = 2  # write_report and the rewrite request of the figure check
PLAN_ATTEMPTS = 2  # a plan without queries gets one more call (spec 50 round 1)
TITLE_MAX_CHARS = 120
# Turn keys of one data-loop run. The flow clears them before the next query.
LOOP_KEYS = (
    "loop_outcome",
    "answer",
    "error_type",
    "retry",
    "next",
    "model_feedback",
    "cannot_answer",
    "guard_outcome",
    "reason_code",
    "block_message",
    "single_step",
    "sql",
    "plan",
)


def library_of(ctx: TurnContext) -> ReportLibrary:
    services = ctx.services
    return ReportLibrary(ctx.store, services.settings, services.scanner, services.clock)


def report_of(turn: TurnState) -> dict[str, Any]:
    return dict(turn.get("report") or {})


def today_of(ctx: TurnContext) -> date:
    return ctx.services.clock().date()


def _period(data: dict[str, str] | None) -> Period | None:
    if not data:
        return None
    try:
        start, end = (date.fromisoformat(data[k]) for k in ("start", "end"))
    except (KeyError, TypeError, ValueError):
        return None
    return Period(min(start, end), max(start, end))


def _period_dict(period: Period | None) -> dict[str, str] | None:
    if period is None:
        return None
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def clip_period(period: Period | None, today: date) -> tuple[Period | None, bool]:
    """`end` is never after today (spec 32, B2.2). Returns the period and True when moved."""
    if period is None or period.end <= today:
        return period, False
    return Period(min(period.start, today), today), True


def last_results_data(state: AgentState, ctx: TurnContext) -> dict[str, Any] | None:
    """The data of the last answer with results, for "save this as a report" (spec 34, B2.2):
    the references in `pinned.last_results` and the rows in the result cache. None when there is
    no such answer, or when a result is older than 30 days (A4)."""
    items = list((state.get("pinned") or {}).get("last_results") or [])
    if not items or ctx.store is None:
        return None
    results, steps, plans = [], [], []
    now = ctx.services.clock()
    for item in items:
        stored = result_cache.get(
            ctx.store, item.get("ref", ""), owner_id=ctx.user.id, now=now
        )
        if stored is None:
            return None
        results.append(stored)
        steps.append({**(item.get("step") or {}), "result_ref": stored["ref"]})
        plans.append(dict(item.get("plan") or {}))
    return {
        "question": items[0].get("question", ""),
        "results": results,
        "steps": steps,
        "plans": plans,
    }


# --- start and library commands ---------------------------------------------------------------------
@node("report_start")
def report_start(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    lib = ctx.services.prompts
    turn = turn_of(state)
    request = (turn.get("route") or {}).get("report_request") or {}
    action = request.get("action") or "create"
    report: dict[str, Any] = {
        "action": action,
        "file_format": request.get("file_format"),
        "report_id": request.get("report_id"),
        "request": turn.get("question", ""),
    }
    trace.get_current_span().set_attribute("report_action", action)
    if action in ("create", "save_last"):
        ctx.budget.use_report_limits()
    if action == "save_last":
        data = last_results_data(state, ctx)
        if data is None:
            return end_turn(
                {**turn, "report": report}, lib.reply("reports.no_last_data")
            )
        queries = []
        for i, step in enumerate(data["steps"], start=1):
            queries.append(
                {
                    "id": f"q{i}",
                    "purpose": step.get("purpose", ""),
                    "sql": step.get("sql", ""),
                    "ref": step.get("result_ref"),
                    "status": "ok",
                    "job_id": step.get("job_id", ""),
                    "bytes_billed": step.get("bytes_billed", 0),
                    "entities": step.get("entities") or {},
                }
            )
        periods = [(p.get("active_filters") or {}).get("period") for p in data["plans"]]
        report.update(
            queries=queries,
            source_question=data["question"],
            period_text_raw=next((p for p in periods if p), None),
            definitions=[
                k for p in data["plans"] for k in p.get("definitions_used") or []
            ],
        )
        turn = {
            **turn,
            "results": list(data["results"]),
            "plans": list(data["plans"]),
        }
    return {"turn": {**turn, "report": report}}


def list_text(lib_prompts: Any, listing: ReportList) -> str:
    if not listing.reports:
        return lib_prompts.reply("reports.empty_list")
    lines = [
        lib_prompts.reply("reports.list_header"),
        "",
        "| ID | Title | Period | Created | Files |",
        "|---|---|---|---|---|",
    ]
    for item in listing.reports:
        period = ""
        if item.period_start and item.period_end:
            try:
                period = format_range(
                    date.fromisoformat(item.period_start),
                    date.fromisoformat(item.period_end),
                )
            except ValueError:
                period = f"{item.period_start}..{item.period_end}"
        title = item.title if len(item.title) <= 50 else item.title[:49] + "…"
        lines.append(
            f"| {item.report_id} | {title.replace('|', '/')} | {period} | "
            f"{item.created_at[:10]} | {', '.join(item.files)} |"
        )
    if listing.total > len(listing.reports):
        lines += [
            "",
            lib_prompts.reply(
                "reports.list_more", shown=len(listing.reports), total=listing.total
            ),
        ]
    return "\n".join(lines)


@node("report_library")
def report_library(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """List, open and export. No model call and no query (spec 32, B5)."""
    ctx = runtime.context
    prompts = ctx.services.prompts
    turn = turn_of(state)
    report = report_of(turn)
    library = library_of(ctx)
    action = report["action"]
    if action == "list":
        return end_turn(turn, list_text(prompts, library.list(ctx.user)))
    report_id = (report.get("report_id") or "").strip()
    if not report_id:
        return end_turn(turn, prompts.reply("reports.no_id"))
    try:
        if action == "open":
            return end_turn(turn, library.get(ctx.user, report_id).body_markdown)
        fmt = report.get("file_format") or "html"
        path = library.export(ctx.user, report_id, fmt)
    except ReportNotFound as exc:
        return end_turn(turn, exc.user_message)
    shown = display_path(path)
    report["files"] = [shown]
    return end_turn(
        {**turn, "report": report}, prompts.reply("reports.exported", path=shown)
    )


# --- plan ---------------------------------------------------------------------------------------
def plan_request(
    state: AgentState, ctx: TurnContext, calendar: Calendar, system: str
) -> ModelRequest:
    """The context of `plan_report` (spec 34, B2.1): schema, calendar and definitions first, then
    the pinned facts, the summary, the recent turns and the request."""
    services = ctx.services
    turn = turn_of(state)
    manager = is_manager(ctx.user)
    first = services.values.first_date("order_items")
    stable = [
        (
            "<schema>\n"
            f"{model_schema_text(services.schema, manager=manager)}\n"
            f"The data starts on {first.isoformat() if first else 'an unknown date'}.\n"
            "</schema>"
        ),
        f"<calendar>\n{calendar_text(calendar)}\n</calendar>",
        f"<definitions>\n{services.prompts.text('definitions')}\n</definitions>",
    ]
    return builder_of(services).build(
        "plan_report",
        system=system,
        state=state,
        stable=stable,
        user_message=turn["question"],
    )


@node("plan_report")
def plan_report(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    require_bigquery(ctx)
    services = ctx.services
    prompts = services.prompts
    settings = services.settings.reports
    turn = turn_of(state)
    report = report_of(turn)
    today = today_of(ctx)
    calendar = Calendar(today)
    user = ctx.user
    system = prompts.system(
        "plan_report",
        today=today.isoformat(),
        scope=scope_text(user),
        max_queries=settings.max_planned_queries,
        title_scope=brands_text(user.brands) if is_manager(user) else "all brands",
    )
    request = plan_request(state, ctx, calendar, system)
    prompt = request.prompt
    plan: ReportPlan | None = None
    planned: list[Any] = []
    sp = trace.get_current_span()
    # Spec 50 round 1: the model sometimes leaves out the queries. Ask one more time.
    for attempt in range(1, PLAN_ATTEMPTS + 1):
        sp.set_attribute("plan_attempts", attempt)
        try:
            result = services.llm.generate(
                ctx.budget,
                system=request.system,
                prompt=prompt,
                schema=ReportPlan,
                purpose="plan_report",
                prompt_version=prompts.version("safety", "plan_report", "definitions"),
            )
        except (AgentError, ValidationError) as exc:
            if not is_format_error(exc):
                raise
            plan = None
        else:
            plan = result.parsed
        planned = [q for q in (plan.queries if plan else []) if q.sql and q.sql.strip()]
        if planned:
            break
        retry = prompts.reply(
            "reports.plan_retry", max_queries=settings.max_planned_queries
        )
        prompt = f"{request.prompt}\n\n{retry}"
    sp.set_attribute("planned_queries", len(planned))
    if len(planned) > settings.max_planned_queries:
        sp.add_event(
            "plan_cut", {"kept": settings.max_planned_queries, "planned": len(planned)}
        )
        planned = planned[: settings.max_planned_queries]
    if plan is None or not planned:
        return end_turn({**turn, "report": report}, prompts.reply("reports.no_plan"))
    period, moved = clip_period(_period(plan.period.model_dump()), today)
    compare, _ = clip_period(
        _period(plan.compare_period.model_dump()) if plan.compare_period else None,
        today,
    )
    report.update(
        title=one_line(plan.title)[:TITLE_MAX_CHARS],
        period=_period_dict(period),
        compare_period=_period_dict(compare),
        period_note=(
            prompts.reply("reports.period_moved", today=format_day(today))
            if moved
            else None
        ),
        definitions=list(plan.definitions_used),
        queries=[
            {
                "id": f"q{i}",
                "purpose": one_line(q.purpose),
                "sql": q.sql.strip(),
                "definitions_used": list(q.definitions_used),
                "status": "pending",
            }
            for i, q in enumerate(planned, start=1)
        ],
        index=0,
    )
    ctx.budget.reserve_llm_calls(RESERVED_WRITE_CALLS)
    return {"turn": {**turn, "report": report}}


# --- the queries ------------------------------------------------------------------------------------
def _without_loop_keys(turn: TurnState) -> TurnState:
    return {k: v for k, v in turn.items() if k not in LOOP_KEYS}  # type: ignore[return-value]


@node("next_query")
def next_query(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Prepare the turn for one report query. The data loop starts at the guard."""
    turn = turn_of(state)
    report = report_of(turn)
    index = report["index"]
    query = report["queries"][index]
    period = report.get("period")
    plan = {
        "purpose": query["purpose"],
        "sql": query["sql"],
        "final": True,
        "definitions_used": query.get("definitions_used") or [],
        "active_filters": {
            "period": f"{period['start']}..{period['end']}" if period else None,
            "brands": [],
            "other": [],
        },
        "assumptions": [],
    }
    report["results_before"] = len(turn.get("results") or [])
    turn = _without_loop_keys(turn)
    turn = {
        **turn,
        "question": (
            f"Report query {query['id']}: {query['purpose']}\n\n"
            f"Report request: {report['request']}"
        ),
        "sql": query["sql"],
        "plan": plan,
        "attempt": 1,
        "step_index": index,
        "single_step": True,
        "next": "guard",
        "retry": None,
        "report": report,
    }
    turn = with_step(turn, purpose=query["purpose"], sql=query["sql"], attempts=1)
    return {"turn": turn}


def _missing_reason(ctx: TurnContext, turn: TurnState) -> str:
    prompts = ctx.services.prompts
    reasons = prompts.reply_value("reports.missing_reasons")
    attempts = ctx.services.settings.agent.max_attempts_per_step
    if turn.get("cannot_answer"):
        return f"The data cannot answer this part: {turn['cannot_answer']}"
    key = turn.get("loop_outcome") or turn.get("error_type") or "default"
    if key not in reasons:
        key = "default"
    return prompts.render_string(reasons[key], attempts=attempts)


@node("collect_query")
def collect_query(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Record the result of one query, or mark the part as missing (spec 32, A2.9)."""
    ctx = runtime.context
    turn = turn_of(state)
    report = report_of(turn)
    time_over = ctx.budget.remaining_seconds() <= 0
    if turn.get("answer") and time_over:
        return {"turn": {**turn, "report": report}}  # the time limit ends the report
    ctx.budget.check_time()
    index = report["index"]
    queries = list(report["queries"])
    query = dict(queries[index])
    steps = turn.get("steps") or []
    step = dict(steps[index]) if index < len(steps) else {}
    results = turn.get("results") or []
    has_result = len(results) > report.get("results_before", 0)
    if has_result and not turn.get("answer") and turn.get("loop_outcome") == "answer":
        query.update(
            status="ok",
            ref=results[-1]["ref"],
            sql=turn.get("sql") or query["sql"],
            job_id=step.get("job_id", ""),
            bytes_billed=step.get("bytes_billed", 0),
            entities=step.get("entities") or {},
            row_count=results[-1]["row_count"],
        )
    else:
        query.update(status="missing", reason=_missing_reason(ctx, turn))
        query["sql"] = turn.get("sql") or query["sql"]
        trace.get_current_span().add_event(
            "report_part_missing",
            {"query": query["id"], "error_type": turn.get("error_type") or ""},
        )
    queries[index] = query
    report.update(queries=queries, index=index + 1)
    turn = _without_loop_keys(turn)
    return {"turn": {**turn, "question": report["request"], "report": report}}


# --- write and build ---------------------------------------------------------------------------------
def _notes(report: dict[str, Any], prompts: Any) -> list[str]:
    notes = []
    for query in report.get("queries") or []:
        if query.get("status") == "missing":
            notes.append(
                prompts.reply(
                    "reports.missing_part",
                    purpose=query["purpose"],
                    reason=query.get("reason", ""),
                )
            )
    return notes


def write_request(
    state: AgentState, ctx: TurnContext, turn: TurnState, system: str
) -> ModelRequest:
    """The context of `write_report` (spec 34, B2.1). The report block, the definitions and the
    notes come before the data blocks of this turn."""
    prompts = ctx.services.prompts
    report = report_of(turn)
    period = _period(report.get("period"))
    compare = _period(report.get("compare_period"))
    keys = list(dict.fromkeys(report.get("definitions") or []))
    definitions = "\n".join(
        f"- {k}: {prompts.definitions[k].meaning}"
        for k in keys
        if k in prompts.definitions
    )
    notes = "\n".join(f"- {n}" for n in _notes(report, prompts)) or "(none)"
    report_block = [
        f"Title: {report.get('title') or '(none: write a title)'}",
        f"Period: {period.text() if period else report.get('period_text_raw') or 'not given'}",
    ]
    if compare:
        report_block.append(f"Compared with: {compare.text()}")
    return builder_of(ctx.services).build(
        "write_report",
        system=system,
        state=state,
        middle=[
            "<report>\n" + "\n".join(report_block) + "\n</report>",
            f"<definitions>\n{definitions or '(none)'}\n</definitions>",
            f"<notes>\n{notes}\n</notes>",
        ],
        results=list(turn.get("results") or []),
        user_message=report["request"],
    )


@node("write_report")
def write_report(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    services = ctx.services
    prompts = services.prompts
    settings = services.settings.reports
    turn = turn_of(state)
    report = report_of(turn)
    if not turn.get("results"):
        text = prompts.reply(
            "reports.no_result", attempts=services.settings.agent.max_attempts_per_step
        )
        return end_turn(turn, text, ErrorType.EMPTY_RESULT.value)
    ctx.budget.reserve_llm_calls(0)
    user = ctx.user
    scope_key = "manager" if is_manager(user) else "ceo"
    system = prompts.system(
        "write_report",
        persona=ctx.scratch.get("persona", ""),
        user_format=prompts.format_text(user_format(state, turn)),
        max_findings=settings.max_findings,
        min_actions=settings.min_action_items,
        max_actions=settings.max_action_items,
        max_charts=settings.max_charts,
        chart_types=", ".join(settings.chart_types),
        max_words=f"{settings.max_words:,}",
        scope_phrase=prompts.reply(
            f"scope_phrase.{scope_key}", brands=brands_text(user.brands)
        ),
    )
    request = write_request(state, ctx, turn, system)
    result = services.llm.generate(
        ctx.budget,
        system=request.system,
        prompt=request.prompt,
        schema=ReportDraft,
        purpose="write_report",
        prompt_version=prompts.version("safety", "write_report", "definitions"),
    )
    report["draft"] = result.parsed.model_dump()
    return {"turn": {**turn, "report": report}}


def fix_figures(
    ctx: TurnContext, results: list[ResultData], texts: list[str]
) -> list[str] | None:
    """The one rewrite request of the figure check (spec 32, B2.4 step 6)."""
    prompts = ctx.services.prompts
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    contents = "\n\n".join(
        [*(data_block(r) for r in results), f"<sentences>\n{numbered}\n</sentences>"]
    )
    try:
        result = ctx.services.llm.generate(
            ctx.budget,
            system=prompts.system("fix_figures"),
            prompt=contents,
            schema=FigureFixes,
            purpose="fix_figures",
            prompt_version=prompts.version("safety", "fix_figures"),
        )
    except AgentError as exc:
        if (
            exc.error_type is ErrorType.LIMIT_EXCEEDED
            and ctx.budget.remaining_seconds() <= 0
        ):
            raise
        return None  # no rewrite: the untraceable figures are removed
    sentences = result.parsed.sentences
    return sentences if len(sentences) == len(texts) else None


@node("build_report")
def build_report(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    services = ctx.services
    prompts = services.prompts
    settings = services.settings.reports
    turn = turn_of(state)
    report = report_of(turn)
    user = ctx.user
    sp = trace.get_current_span()
    results = list(turn.get("results") or [])
    by_ref = {r["ref"]: r for r in results}
    draft = ReportDraft.model_validate(report["draft"])

    def calc_error(reason: str) -> None:
        sp.add_event("calc_error", {"reason": reason[:120]})

    removed = prompts.reply("figure_removed")
    allowed = allowed_numbers(results, report["request"], user)
    slots = draft_slots(draft)
    check = check_slots(slots, results, allowed, removed)
    rewrites = 0
    if check.unverified_keys:
        fixed = fix_figures(ctx, results, [slots[k] for k in check.unverified_keys])
        if fixed is not None:
            rewrites = len(fixed)
            slots.update(zip(check.unverified_keys, fixed, strict=True))
            check = check_slots(
                slots, results, allowed, removed, on_calc_error=calc_error
            )
    draft = apply_slots(draft, check.texts)

    charts, chart_notes = check_charts(draft.charts, by_ref, settings)
    sections, notes = build_sections(draft, by_ref, settings, _notes(report, prompts))
    title = one_line(report.get("title") or draft.title or report["request"])[
        :TITLE_MAX_CHARS
    ]
    period = _period(report.get("period"))
    if period is None and report.get("period_text_raw"):
        period = span_of(parse_periods(report["period_text_raw"]))
    compare = _period(report.get("compare_period"))
    queries = report.get("queries") or []
    definitions = list(
        dict.fromkeys(
            [
                *(report.get("definitions") or []),
                *(k for q in queries for k in q.get("definitions_used") or []),
                *draft.definitions_used,
            ]
        )
    )
    sections["date_range"] = date_range_section(
        period,
        compare,
        report.get("period_note"),
        today_of(ctx),
        user,
        definitions,
        prompts,
    )
    sections["method"] = method_section(
        queries, user, check.calculated, check.unverified, notes + chart_notes
    )
    sections["period_text"] = period.text() if period else ""
    sections["calculated_figures"] = check.calculated
    sections["unverified_figures"] = check.unverified
    (title, sections, charts), hits = scan_strings(
        [title, sections, charts], services.scanner, "report"
    )
    entities = report_entities(queries, results, user, title, services.values.brands)
    report_id = library_of(ctx).new_report_id()
    body = markdown_body(
        title,
        f"Report {report_id} · {sections['period_text']}".rstrip(" ·"),
        sections,
        charts,
    )
    scanned = services.scanner.scan(body, target="report_body")
    for name, value in (
        ("calculated_figures", check.calculated),
        ("unverified_figures", check.unverified),
        ("checked_figures", check.checked),
        ("calc_errors", check.calc_errors),
        ("figure_rewrites", rewrites),
        ("charts", len(charts)),
        ("scanner_hits", hits + sum(scanned.hits.values())),
    ):
        sp.set_attribute(name, value)
    report["built"] = {
        "report_id": report_id,
        "title": title,
        "period": _period_dict(period),
        "sections": sections,
        "charts": charts,
        "definitions": definitions,
        "entities": entities,
        "body_markdown": scanned.text,
        "calculated_figures": check.calculated,
        "unverified_figures": check.unverified,
        "calc_errors": check.calc_errors,
    }
    return {"turn": {**turn, "report": report}}


# --- save and render ---------------------------------------------------------------------------------
@node("save_report")
def save_report(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    services = ctx.services
    turn = turn_of(state)
    report = report_of(turn)
    ctx.budget.check_time()  # a report over the time limit saves nothing (spec 32, A2.10)
    built = report["built"]
    period = built.get("period") or {}
    user = ctx.user
    sql = [
        {
            "id": q.get("id"),
            "purpose": q.get("purpose"),
            "sql": q.get("sql"),
            "job_id": q.get("job_id") or None,
            "bytes_billed": int(q.get("bytes_billed") or 0),
            "status": q.get("status"),
        }
        for q in report.get("queries") or []
    ]
    saved = Report(
        report_id=built["report_id"],
        owner_id=user.id,
        conversation_id=state.get("conversation_id") or "",
        title=built["title"],
        question=services.scanner.scan(report["request"], target="report").text,
        created_at=services.clock().isoformat(),
        period_start=period.get("start"),
        period_end=period.get("end"),
        scope_brands=list(user.brands),
        sql=sql,
        body_markdown=built["body_markdown"],
        sections=built["sections"],
        charts=built["charts"],
        definitions=built["definitions"],
        entities=built["entities"],
        calculated_figures=built["calculated_figures"],
        unverified_figures=built["unverified_figures"],
        model=services.settings.models.default,
        prompt_version=services.prompts.version(
            "safety", "plan_report", "write_report", "definitions"
        ),
        trace_id=turn.get("trace_id") or None,
    )
    library_of(ctx).create(saved)
    report["saved_id"] = saved.report_id
    report.pop("draft", None)
    return {"turn": {**turn, "report": report}}


def chat_text(
    report: dict[str, Any], prompts: Any, files: list[str], file_error: str
) -> str:
    built = report["built"]
    sections = built["sections"]
    lines = [f"**{built['title']}**"]
    if sections.get("period_text"):
        lines.append(f"_{sections['period_text']}_")
    lines.append("")
    lines += [f"- {s}" for s in sections.get("summary") or []]
    actions = sections.get("action_items") or []
    if actions:
        lines += ["", "**Action items**"]
        lines += [f"{i}. {a['action']}" for i, a in enumerate(actions, start=1)]
    for missing in sections.get("missing") or []:
        lines += ["", f"> Missing part: {missing}"]
    lines += ["", prompts.reply("reports.saved", report_id=built["report_id"])]
    for path in files:
        lines.append(prompts.reply("reports.file_written", path=path))
    if file_error:
        lines.append(file_error)
    return "\n".join(lines)


@node("render")
def render(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Write the requested file, then the chat answer (spec 32, A2.1 and A2.3)."""
    ctx = runtime.context
    prompts = ctx.services.prompts
    turn = turn_of(state)
    report = report_of(turn)
    files: list[str] = []
    file_error = ""
    fmt = report.get("file_format")
    if fmt in ("html", "md"):
        try:
            path = library_of(ctx).export(ctx.user, report["saved_id"], fmt)
            files.append(display_path(path))
        except AgentError as exc:
            file_error = f"The file was not written: {exc.user_message}"
    report["files"] = files
    text = chat_text(report, prompts, files, file_error)
    turn = {
        **turn,
        "report": report,
        "unverified_figures": report["built"]["unverified_figures"],
        "calc_errors": report["built"]["calc_errors"],
    }
    return end_turn(turn, text)


# --- the subgraph ------------------------------------------------------------------------------------
def _ended(state: AgentState) -> bool:
    return bool(turn_of(state).get("answer"))


def after_start(state: AgentState) -> str:
    if _ended(state):
        return END
    action = report_of(turn_of(state)).get("action")
    if action in LIBRARY_ACTIONS:
        return "report_library"
    return "write_report" if action == "save_last" else "plan_report"


def after_collect(state: AgentState) -> str:
    if _ended(state):
        return END
    report = report_of(turn_of(state))
    return "next_query" if report["index"] < len(report["queries"]) else "write_report"


def _next_or_end(target: str) -> Callable[[AgentState], str]:
    def route(state: AgentState) -> str:
        return END if _ended(state) else target

    return route


def build_report_flow() -> Any:
    g = StateGraph(AgentState, context_schema=TurnContext, output_schema=LoopOutput)
    g.add_node("report_start", report_start)
    g.add_node("report_library", report_library)
    g.add_node("plan_report", plan_report)
    g.add_node("next_query", next_query)
    g.add_node("data_loop", build_data_loop())
    g.add_node("collect_query", collect_query)
    g.add_node("write_report", write_report)
    g.add_node("build_report", build_report)
    g.add_node("save_report", save_report)
    g.add_node("render", render)
    g.add_edge(START, "report_start")
    g.add_conditional_edges(
        "report_start",
        after_start,
        {
            "report_library": "report_library",
            "plan_report": "plan_report",
            "write_report": "write_report",
            END: END,
        },
    )
    g.add_edge("report_library", END)
    g.add_conditional_edges(
        "plan_report",
        _next_or_end("next_query"),
        {"next_query": "next_query", END: END},
    )
    g.add_edge("next_query", "data_loop")
    g.add_edge("data_loop", "collect_query")
    g.add_conditional_edges(
        "collect_query",
        after_collect,
        {"next_query": "next_query", "write_report": "write_report", END: END},
    )
    for name, nxt in (
        ("write_report", "build_report"),
        ("build_report", "save_report"),
        ("save_report", "render"),
    ):
        g.add_conditional_edges(name, _next_or_end(nxt), {nxt: nxt, END: END})
    g.add_edge("render", END)
    return g.compile()


capabilities.register("reports")  # spec 37, B2.5
