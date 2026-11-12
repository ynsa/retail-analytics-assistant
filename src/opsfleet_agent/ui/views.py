"""Plain JSON views of the session results for the page (spec 40). Display only: no rule here.

The answer Markdown becomes HTML on the server with the same Markdown settings as the HTML
report (spec 32): raw HTML, links and images are off, so the page can insert the HTML as is.
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt

from opsfleet_agent.config import PerTurnLimits
from opsfleet_agent.identity import UserContext
from opsfleet_agent.reports.charts import draw_svg
from opsfleet_agent.reports.models import Report, ReportSummary
from opsfleet_agent.reports.render_md import SECTION_TITLES
from opsfleet_agent.resilience.safe_node import log_exception
from opsfleet_agent.session import ConfirmationView, TurnResult
from opsfleet_agent.ui.trace_reader import Step, TurnTrace, megabytes

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
    row: dict[str, Any], trace: TurnTrace | None, report_id: str | None
) -> dict[str, Any]:
    """A turn of an earlier conversation, from `conversation_turns` and its trace."""
    totals = trace.totals if trace else None
    return {
        "trace_id": row.get("trace_id"),
        "turn_no": row.get("turn_no"),
        "final": True,
        "question": row.get("user_text") or "",
        "status": row.get("status"),
        "intent": row.get("intent"),
        "error_type": row.get("error_type"),
        "label": label(
            row.get("intent"),
            row.get("error_type"),
            trace.reason_code if trace else None,
        ),
        "answer_html": markdown_html(row.get("answer_text") or ""),
        "totals": totals,
        "cost_line": cost_line(totals) if totals else "",
        "report_id": report_id,
        "confirmation": None,
    }


def steps_view(
    trace: TurnTrace, limits: PerTurnLimits, label_scope: str = ""
) -> dict[str, Any]:
    """The steps pane: the steps and the totals with the limits (spec 40, A4)."""
    totals = trace.totals
    report = trace.intent == "report"
    max_seconds = limits.max_seconds_report if report else limits.max_seconds_question
    return {
        "trace_id": trace.trace_id,
        "intent": trace.intent,
        "steps": [_step_view(s) for s in trace.steps],
        "totals": [
            f"Tokens: {int(totals['tokens_in']):,} in · {int(totals['tokens_out']):,} out",
            f"Model calls: {int(totals['llm_calls'])} of {limits.max_llm_calls}",
            f"Queries: {int(totals['bq_queries'])} of {limits.max_bq_queries}",
            f"Bytes billed: {megabytes(totals['bytes_billed'])}",
            f"Time: {totals['seconds']:.1f} s of {max_seconds:g} s",
        ],
        "scope": label_scope,
    }


def _step_view(step: Step) -> dict[str, Any]:
    return {
        "title": step.title,
        "meta": " · ".join(step.meta),
        "note": step.note,
        "status": step.status,
        "sql": step.sql,
    }


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
