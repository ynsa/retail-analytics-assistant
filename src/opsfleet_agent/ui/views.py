"""Plain JSON views of the session results for the page (spec 40). Display only: no rule here.

The answer Markdown becomes HTML on the server with the same Markdown settings as the HTML
report (spec 32): raw HTML, links and images are off, so the page can insert the HTML as is.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from markdown_it import MarkdownIt

from opsfleet_agent.config import PerTurnLimits
from opsfleet_agent.identity import UserContext
from opsfleet_agent.observability.trace_view import TraceNode, TraceTree
from opsfleet_agent.reports.charts import draw_svg
from opsfleet_agent.reports.models import Report, ReportSummary
from opsfleet_agent.reports.render_md import SECTION_TITLES
from opsfleet_agent.resilience.safe_node import log_exception
from opsfleet_agent.session import ConfirmationView, TurnResult

INTENT_TEXT = {
    "analysis": "data question",
    "schema": "question about the data",
    "clarify": "clarifying question",
    "report": "report",
    "delete": "delete reports",
    "chitchat": "chat",
    "off_topic": "off topic",
    "unsafe": "unsafe request",
}
GUARD_TEXT = {"pass": "pass", "policy_block": "block", "parse_error": "parse error"}
STEP_TITLES = {
    "route": "Route",
    "write_sql": "Write SQL",
    "guard": "SQL guard",
    "run_query": "BigQuery",
    "check_empty": "Check the empty result",
    "write_answer": "Write the answer",
    "schema_answer": "Answer about the data",
    "prepared_reply": "Prepared reply",
    "give_up": "Stop without an answer",
    "explain_block": "Explain the block",
    "plan_report": "Plan the report",
    "write_report": "Write the report",
    "build_report": "Build the report",
    "save_report": "Save the report",
    "render": "Write the file",
    "parse_delete": "Read the delete request",
    "find_reports": "Find the reports",
    "confirm_delete": "Confirmation",
    "apply_delete": "Delete",
    "undo_delete": "Undo",
    "summarize": "Summary of older turns",
}
# Shown only when they called Gemini or BigQuery, or failed.
OPTIONAL_STEPS = frozenset({"check_empty"})

# The label above an answer. "amber" marks a refusal or a stop (spec 40, A2.2).
REASON_TEXT = {
    "pii_column": "personal data",
    "row_value": "personal data",
    "demographic_pairing": "personal data",
    "brand_out_of_scope": "brand not in your scope",
    "table_not_allowed": "table not allowed",
    "not_select": "not a read query",
    "forbidden_construct": "not allowed in a query",
}
ERROR_TEXT = {
    "limit_exceeded": "Limit reached",
    "confirmation_rejected": "Delete cancelled",
    "empty_result": "No data",
    "guard_parse_error": "Query not accepted",
    "sql_mismatch": "Query did not match the question",
}


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    md.disable(["link", "image", "autolink", "html_inline", "html_block"])
    return md


_MD = _markdown()


def markdown_html(text: str) -> str:
    return _MD.render(text or "")


def user_view(user: UserContext) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "role": user.role.value,
        "brands": list(user.brands),
        "scope": scope_text(user),
    }


def scope_text(user: UserContext) -> str:
    return "all brands" if user.all_brands else ", ".join(user.brands)


def label(
    intent: str | None, error_type: str | None, reason_code: str | None = None
) -> dict[str, str] | None:
    """The label of an answer: a refusal is amber, another error is red, else none."""
    if intent == "unsafe":
        return {"text": "Refused · unsafe request", "tone": "amber"}
    if intent == "off_topic":
        return {"text": "Refused · off topic", "tone": "amber"}
    if error_type == "guard_policy_block":
        reason = REASON_TEXT.get(reason_code or "", "policy")
        return {"text": f"Refused · {reason}", "tone": "amber"}
    if error_type in ERROR_TEXT:
        return {"text": ERROR_TEXT[error_type], "tone": "amber"}
    if error_type:
        return {"text": f"Error · {error_type.replace('_', ' ')}", "tone": "red"}
    return None


def megabytes(value: float) -> str:
    """The same unit as the CLI footer: 1 MB = 1,000,000 bytes."""
    return f"{value / 1e6:.1f} MB"


def cost_line(totals: dict[str, Any]) -> str:
    """ "Answered in 18.2 s · 2 model calls · 1 query · 10.5 MB billed"."""
    calls = int(totals.get("llm_calls") or 0)
    queries = int(totals.get("bq_queries") or 0)
    return (
        f"Answered in {float(totals.get('seconds') or 0):.1f} s · "
        f"{calls} model {'call' if calls == 1 else 'calls'} · "
        f"{queries} {'query' if queries == 1 else 'queries'} · "
        f"{megabytes(float(totals.get('bytes_billed') or 0))} billed"
    )


def confirmation_view(view: ConfirmationView) -> dict[str, Any]:
    return {
        "count": view.count,
        "expected_text": view.expected_text,
        "rows": [
            {
                "report_id": r.get("report_id"),
                "title": r.get("title"),
                "created": r.get("created"),
                "reason": r.get("reason"),
            }
            for r in view.rows
        ],
        "not_found": list(view.not_found),
    }


def _reason_code(result: TurnResult) -> str | None:
    return next((s["reason_code"] for s in result.steps if s.get("reason_code")), None)


def turn_view(result: TurnResult, final: bool = True) -> dict[str, Any]:
    """One answer of the running session. `final` is False for the early answer of spec 34
    (`on_answer`): the summary of older turns can still add cost."""
    confirmation = result.confirmation
    return {
        "trace_id": result.trace_id,
        "turn_no": result.turn_no,
        "final": final,
        "status": result.status,
        "intent": result.intent,
        "error_type": result.error_type,
        "label": None
        if confirmation
        else label(result.intent, result.error_type, _reason_code(result)),
        "answer_html": "" if confirmation else markdown_html(result.answer),
        "totals": dict(result.totals),
        "cost_line": cost_line(result.totals),
        "report_id": result.report_id,
        "confirmation": confirmation_view(confirmation) if confirmation else None,
        "delete_outcome": result.delete_outcome,
        "batch_id": result.batch_id,
        "replay_input": result.replay_input,
        "saved": result.saved,
    }


def history_turn_view(
    row: dict[str, Any], report_id: str | None = None
) -> dict[str, Any]:
    """A turn of an earlier conversation, from `conversation_turns` with the figures of
    spec 35. A row from before spec 35 has no figures, so it has no cost line."""
    totals = row.get("totals") or {}
    return {
        "trace_id": row.get("trace_id"),
        "turn_no": row.get("turn_no"),
        "final": True,
        "question": row.get("user_text") or "",
        "status": row.get("status"),
        "intent": row.get("intent"),
        "error_type": row.get("error_type"),
        "label": label(
            row.get("intent"), row.get("error_type"), row.get("guard_block_reason")
        ),
        "answer_html": markdown_html(row.get("answer_text") or ""),
        "totals": totals or None,
        "cost_line": cost_line(totals) if totals.get("seconds") else "",
        "report_id": report_id,
        "confirmation": None,
    }


# --- the steps pane (A2.2) -------------------------------------------------------------------------
def steps_view(
    tree: TraceTree, limits: PerTurnLimits, scope: str = ""
) -> dict[str, Any]:
    """The steps and the totals with the limits (spec 40, A4), from the trace tree of `/trace`
    (spec 35). Each `node.*` span is one step; its child spans give the details."""
    nodes = list(_walk(tree.root))
    route = next((n for n in nodes if n.name == "node.route"), None)
    intent = tree.root.attributes.get("intent") or (
        route.attributes.get("intent") if route else None
    )
    totals = trace_totals(tree.root, nodes)
    report = intent == "report"
    max_seconds = limits.max_seconds_report if report else limits.max_seconds_question
    steps = [
        step
        for node in nodes
        if node.kind == "node" and (step := _step(node, scope)) is not None
    ]
    return {
        "trace_id": tree.trace_id,
        "intent": intent,
        "steps": steps,
        "totals": [
            f"Tokens: {int(totals['tokens_in']):,} in · {int(totals['tokens_out']):,} out",
            f"Model calls: {int(totals['llm_calls'])} of {limits.max_llm_calls}",
            f"Queries: {int(totals['bq_queries'])} of {limits.max_bq_queries}",
            f"Bytes billed: {megabytes(totals['bytes_billed'])}",
            f"Time: {totals['seconds']:.1f} s of {max_seconds:g} s",
        ],
    }


def _walk(node: TraceNode) -> Iterator[TraceNode]:
    """The spans under `node` in start order (the tree keeps the children in start order)."""
    for child in node.children:
        yield child
        yield from _walk(child)


def trace_totals(turn: TraceNode, nodes: list[TraceNode]) -> dict[str, float]:
    """The figures of the `turn` span. A turn that stopped with an internal error has none:
    then the sums of the `llm` and `sql` spans."""
    llm = [n.attributes for n in nodes if n.kind == "llm"]
    queries = [n.attributes for n in nodes if _is_query(n)]
    sums = {
        "llm_calls": len(llm),
        "tokens_in": sum(a.get("tokens_in") or 0 for a in llm),
        "tokens_out": sum(a.get("tokens_out") or 0 for a in llm),
        "bq_queries": len(queries),
        "bytes_billed": sum(a.get("bytes_billed") or 0 for a in queries),
        "seconds": round(turn.duration_ms / 1000, 2),
    }
    out = {}
    for key, fallback in sums.items():
        value = turn.attributes.get(key)
        out[key] = float(value if isinstance(value, int | float) else fallback)
    return out


def _step(node: TraceNode, scope: str) -> dict[str, Any] | None:
    name = node.name.removeprefix("node.")
    attrs = node.attributes
    kids = node.children
    llm = [k for k in kids if k.kind == "llm"]
    queries = [k for k in kids if _is_query(k)]
    failed = node.status == "error"
    if name not in STEP_TITLES and not (llm or queries or failed):
        return None
    if name in OPTIONAL_STEPS and not (llm or queries or failed):
        return None
    title = STEP_TITLES.get(name, name.replace("_", " ").capitalize())
    meta: list[str] = []
    note: str | None = None
    sql = _child_attr(kids, "sql_masked", kind="sql")
    if name == "route" and attrs.get("intent"):
        title = f"Route: {INTENT_TEXT.get(attrs['intent'], attrs['intent'])}"
    elif name == "write_sql" and int(attrs.get("attempt") or 0) > 1:
        title = f"Write SQL (attempt {attrs['attempt']})"
    elif name == "guard":
        title, note, sql = _guard_text(kids, scope)
    elif name == "plan_report" and attrs.get("planned_queries"):
        title = f"Plan the report: {attrs['planned_queries']} queries"
    elif name == "build_report" and attrs.get("charts") is not None:
        title = f"Build the report: {attrs['charts']} charts"
    elif name == "save_report":
        saved = _child_attr(kids, "report_id")
        title = f"Save the report: {saved}" if saved else title
    elif name in ("confirm_delete", "apply_delete"):
        outcome = attrs.get("confirm_outcome") or _child_attr(kids, "outcome")
        title = f"{title}: {outcome}" if outcome else title
    if llm:
        fallback = any(k.attributes.get("fallback_used") for k in llm)
        meta.append("Gemini (fallback model)" if fallback else "Gemini")
    if queries:
        count = len(queries)
        billed = sum(q.attributes.get("bytes_billed") or 0 for q in queries)
        title = f"{title}: {count} {'query' if count == 1 else 'queries'}"
        meta.append(megabytes(billed))
    seconds = node.duration_ms / 1000
    if seconds >= 0.05 or llm or queries:
        meta.append(f"{seconds:.1f} s")
    if failed:
        note = attrs.get("error_type") or "error"
    return {
        "title": title,
        "meta": " · ".join(meta),
        "note": note,
        "status": "error" if failed else "ok",
        "sql": sql,
    }


def _guard_text(
    kids: list[TraceNode], scope: str
) -> tuple[str, str | None, str | None]:
    """The title, the note and the SQL of the guard step. A blocked query never runs, so its
    step shows the SQL of the model. A query that passes shows its SQL in the BigQuery step,
    after the scope rewrite."""
    check = next((k for k in kids if k.kind == "guard"), None)
    attrs = check.attributes if check else {}
    outcome = str(attrs.get("outcome") or "")
    title = f"SQL guard: {GUARD_TEXT.get(outcome, outcome)}" if outcome else "SQL guard"
    sql = None if outcome == "pass" else attrs.get("sql_masked")
    reason = attrs.get("reason_code")
    if reason:
        return title, f"reason: {str(reason).replace('_', ' ')}", sql
    return title, (f"scope: {scope}" if scope and outcome == "pass" else None), sql


def _is_query(node: TraceNode) -> bool:
    """A BigQuery job that ran: a `sql` span with billed bytes."""
    return node.kind == "sql" and "bytes_billed" in node.attributes


def _child_attr(kids: list[TraceNode], name: str, kind: str | None = None) -> Any:
    return next(
        (
            k.attributes[name]
            for k in kids
            if k.attributes.get(name) and kind in (None, k.kind)
        ),
        None,
    )


def standalone_svg(svg: str) -> str:
    """The report SVG is made for inline use, without the XML namespaces. The page shows it as
    an image file (`<img src="data:...">`), which needs them."""
    return svg.replace(
        "<svg",
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"',
        1,
    )


def report_summary_view(summary: ReportSummary) -> dict[str, Any]:
    return {
        "report_id": summary.report_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "conversation_id": summary.conversation_id,
        "files": summary.files,
    }


def report_view(report: Report, max_charts: int = 1) -> dict[str, Any]:
    """The report card in the chat (spec 40, A2.5): the sections, the first chart as SVG and
    the action items. The chart comes from the saved data, the same as in the HTML file."""
    sections = report.sections or {}
    actions = [
        str(a.get("action") or "")
        for a in sections.get("action_items") or []
        if isinstance(a, dict)
    ]
    charts = []
    for chart in report.charts[:max_charts]:
        try:
            svg = standalone_svg(draw_svg(chart))
            charts.append({"title": chart.get("title") or "", "svg": svg})
        except Exception:  # noqa: BLE001 - a chart that cannot be drawn only drops the picture
            log_exception("ui report chart", report.trace_id)
    return {
        **report_summary_view(report.summary()),
        "period_text": sections.get("period_text") or "",
        "sections": list(SECTION_TITLES),  # each report file has all six sections
        "summary": [str(s) for s in sections.get("summary") or []],
        "action_items": actions,
        "charts": charts,
    }
