"""The delete flow: a subgraph for the intent `delete` (spec 33, B2.1).

    parse_delete -> undo_delete                         (scope `undo`, no confirmation)
                 -> find_reports -> confirm_delete -> apply_delete

`find_reports` matches in code and saves `pending_confirmation` in the state. `confirm_delete`
pauses the graph with `interrupt()`. Only the next user input, sent with `Command(resume=text)`,
can confirm. On resume, LangGraph runs `confirm_delete` again from its start, so the code before
`interrupt()` only reads the state. The session can also resume with `{"cancel": reason}` (for
`/undo`, `/exit` or an old conversation): code sends it, a user cannot.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from opentelemetry import trace

from opsfleet_agent import capabilities
from opsfleet_agent.agent.nodes.common import end_turn, node, turn_of
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.agent.tools import ConfirmationGrant, ToolContext
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.reports.deletion import UndoResult, undo_batch
from opsfleet_agent.reports.flow import library_of
from opsfleet_agent.reports.matching import MatchResult, match_reports
from opsfleet_agent.store.audit import confirm_span, write_audit

MAX_REPORTS = 50
CONFIRM_MINUTES = 10
SHORT_ANSWERS = frozenset(
    {
        "",
        "yes",
        "y",
        "ok",
        "okay",
        "sure",
        "confirm",
        "confirmed",
        "no",
        "cancel",
        "delete",
        "delete all",
        "delete them",
    }
)
_DELETE_N = re.compile(r"^delete \d+$")
CANCEL_TEXTS = {
    "wrong_number": "delete.wrong_number",
    "not_exact": "delete.not_exact",
    "other_text": "delete.cancelled",
    "abandoned": "delete.cancelled",
    "expired": "delete.expired",
    "changed": "delete.changed",
    "internal_error": "delete.internal",
}


class DeleteOutput(TypedDict):
    """The subgraph returns the turn and the cleared pending confirmation, not the messages."""

    turn: TurnState
    pending_confirmation: dict[str, Any] | None


def ids_hash(
    owner_id: str, conversation_id: str, nonce: str, report_ids: list[str]
) -> str:
    """SHA-256 of the owner, the conversation, the nonce and the sorted IDs (spec 33, B2.3)."""
    text = "\n".join([owner_id, conversation_id, nonce, *sorted(report_ids)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_answer(text: str) -> str:
    """Trim, lower case, single spaces (spec 33, B2.3 rule 1)."""
    return " ".join((text or "").lower().split())


def delete_of(turn: TurnState) -> dict[str, Any]:
    return dict(turn.get("delete") or {})


def _audit(
    ctx: TurnContext, state: AgentState, action: str, details: dict[str, Any]
) -> None:
    write_audit(
        ctx.store,
        user_id=ctx.user.id,
        conversation_id=state.get("conversation_id"),
        action=action,
        details=details,
        at=ctx.services.clock(),
    )


# --- parse and undo -------------------------------------------------------------------------------
@node("parse_delete")
def parse_delete(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    turn = turn_of(state)
    request = (turn.get("route") or {}).get("delete_request") or {}
    data = {
        "scope": request.get("scope"),
        "entity": request.get("entity"),
        "ids": list(request.get("ids") or []),
    }
    trace.get_current_span().set_attribute("delete_scope", data["scope"] or "none")
    return {"turn": {**turn, "delete": data}}


def undo_text(ctx: TurnContext, result: UndoResult) -> str:
    prompts = ctx.services.prompts
    if result.outcome == "nothing":
        return prompts.reply("delete.nothing_to_undo")
    if result.outcome == "too_old":
        return prompts.reply("delete.undo_too_old")
    text = prompts.reply(
        "delete.undone", count=len(result.report_ids), batch_id=result.batch_id
    )
    if result.files_restored:
        text += " " + prompts.reply(
            "delete.undone_files",
            count=result.files_restored,
            folder=f"data/reports/{ctx.user.id}/",
        )
    if result.files_missing:
        text += " " + prompts.reply(
            "delete.files_missing", files=", ".join(result.files_missing)
        )
    return text


@node("undo_delete")
def undo_delete(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """ "Undo the delete" in the chat: the same as `/undo` (spec 33, A2.9)."""
    ctx = runtime.context
    result = undo_batch(
        ctx.store,
        ctx.services.settings,
        ctx.user,
        None,
        now=ctx.services.clock(),
        conversation_id=state.get("conversation_id"),
    )
    turn = turn_of(state)
    data = {
        **delete_of(turn),
        "outcome": f"undo_{result.outcome}",
        "batch_id": result.batch_id,
        "report_ids": list(result.report_ids or []),  # spec 34: the pinned report list
    }
    return end_turn({**turn, "delete": data}, undo_text(ctx, result))


# --- find -----------------------------------------------------------------------------------------
def no_match_text(ctx: TurnContext, match: MatchResult) -> str:
    prompts = ctx.services.prompts
    if match.kind == "close_names":
        return prompts.reply(
            "delete.close_names",
            entity=match.entity,
            names=", ".join(match.close_names),
        )
    if match.kind == "too_many":
        return prompts.reply("delete.too_many", count=match.total, max=MAX_REPORTS)
    if match.kind == "unclear":
        return prompts.reply("delete.which")
    text = (
        prompts.reply("delete.no_match_entity", entity=match.entity)
        if match.entity
        else prompts.reply("delete.no_match")
    )
    if match.not_found:
        text += " " + prompts.reply("delete.not_found", ids=", ".join(match.not_found))
    return text


def confirmation_text(ctx: TurnContext, pending: dict[str, Any]) -> str:
    """The exact list and the text to type, from the pending data only."""
    prompts = ctx.services.prompts
    count = pending["count"]
    header = (
        prompts.reply("delete.list_header_one")
        if count == 1
        else prompts.reply("delete.list_header", count=count)
    )
    lines = [header, ""]
    for i, row in enumerate(pending["rows"], start=1):
        lines.append(
            f"{i}. {row['report_id']} · {row['title']} · {row['created']} · {row['reason']}"
        )
    if pending.get("not_found"):
        lines += [
            "",
            prompts.reply("delete.not_found", ids=", ".join(pending["not_found"])),
        ]
    lines += [
        "",
        prompts.reply("delete.ask", count=count, expected=pending["expected_text"]),
        prompts.reply("delete.ask_note"),
    ]
    return "\n".join(lines)


@node("find_reports")
def find_reports(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    data = delete_of(turn)
    conversation_id = state.get("conversation_id") or ""
    match = match_reports(
        library_of(ctx),
        ctx.services.values,
        ctx.user,
        scope=data.get("scope"),
        entity=data.get("entity"),
        ids=data.get("ids") or [],
        conversation_id=conversation_id,
        max_reports=MAX_REPORTS,
    )
    data.update(match_kind=match.kind, match_count=match.total)
    _audit(
        ctx,
        state,
        "delete_requested",
        {"scope": match.scope, "entity": match.entity, "match_count": match.total},
    )
    confirm_span("request", match.kind, scope=match.scope or "none", count=match.total)
    if match.kind != "matches":
        if match.kind in ("none", "close_names"):
            _audit(
                ctx,
                state,
                "delete_no_match",
                {
                    "scope": match.scope,
                    "entity": match.entity,
                    "close_names": match.close_names,
                },
            )
        return end_turn({**turn, "delete": data}, no_match_text(ctx, match))
    report_ids = sorted(r.report_id for r in match.rows)
    nonce = secrets.token_hex(16)
    pending: dict[str, Any] = {
        "kind": "delete_reports",
        "owner_id": ctx.user.id,
        "conversation_id": conversation_id,
        "report_ids": report_ids,
        "count": len(report_ids),
        "nonce": nonce,
        "ids_hash": ids_hash(ctx.user.id, conversation_id, nonce, report_ids),
        "expected_text": f"delete {len(report_ids)}",
        "created_turn": int(state.get("turn_no") or 0),
        "created_at": ctx.services.clock().isoformat(),
        "rows": [
            {
                "report_id": r.report_id,
                "title": r.title,
                "created": r.created_at[:10],
                "reason": r.reason,
                "files": list(r.files),
            }
            for r in match.rows
        ],
        "not_found": list(match.not_found),
    }
    pending["text"] = confirmation_text(ctx, pending)
    _audit(
        ctx,
        state,
        "delete_confirmation_shown",
        {
            "report_ids": report_ids,
            "ids_hash": pending["ids_hash"],
            "expected_text": pending["expected_text"],
        },
    )
    confirm_span("shown", "pending", count=len(report_ids))
    return {"turn": {**turn, "delete": data}, "pending_confirmation": pending}


# --- confirm --------------------------------------------------------------------------------------
def confirmation_payload(pending: dict[str, Any]) -> dict[str, Any]:
    """The interrupt payload: what the CLI or the UI shows (spec 33, B2.3)."""
    return {
        "type": "confirm_delete",
        "rows": pending["rows"],
        "count": pending["count"],
        "expected_text": pending["expected_text"],
        "text": pending["text"],
        "not_found": pending.get("not_found") or [],
    }


def answer_outcome(
    answer: Any, pending: dict[str, Any], state: AgentState, now: datetime
) -> str:
    """The reason code of the answer rules of B2.3. `confirmed` only for the exact text."""
    if not isinstance(
        answer, str
    ):  # the session cancels: /undo, /exit, an old conversation
        reason = answer.get("cancel") if isinstance(answer, dict) else None
        return reason if reason in ("abandoned", "expired") else "abandoned"
    created = datetime.fromisoformat(pending["created_at"])
    same_turn = int(state.get("turn_no") or 0) == pending.get("created_turn")
    if not same_turn or now - created > timedelta(minutes=CONFIRM_MINUTES):
        return "expired"
    text = normalize_answer(answer)
    if text == pending["expected_text"]:
        return "confirmed"
    if _DELETE_N.match(text):
        return "wrong_number"
    if text in SHORT_ANSWERS:
        return "not_exact"
    return "other_text"


def cancel(
    ctx: TurnContext,
    state: AgentState,
    turn: TurnState,
    pending: dict[str, Any],
    reason: str,
    answer: Any,
) -> dict[str, Any]:
    """End the flow: nothing deleted, one audit event, one span, `pending_confirmation` cleared."""
    _audit(
        ctx,
        state,
        "delete_cancelled",
        {"reason": reason, "report_ids": pending.get("report_ids", [])},
    )
    confirm_span("cancel", "cancelled", reason)
    text = ctx.services.prompts.reply(
        CANCEL_TEXTS[reason], count=pending.get("count", 0)
    )
    error_type = (
        ErrorType.INTERNAL_ERROR.value
        if reason == "internal_error"
        else ErrorType.CONFIRMATION_REJECTED.value
    )
    turn = {
        **turn,
        "delete": {**delete_of(turn), "outcome": "cancelled", "reason": reason},
        "confirmation_prompt": pending.get("text", ""),
        "replay_input": reason == "other_text",
    }
    if isinstance(answer, str):
        turn["confirm_text"] = answer
    return {**end_turn(turn, text, error_type), "pending_confirmation": None}


@node("confirm_delete")
def confirm_delete(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    pending = state.get("pending_confirmation")
    if not pending:
        raise AgentError(
            ErrorType.INTERNAL_ERROR, "No delete is waiting.", "no pending"
        )
    answer = interrupt(confirmation_payload(pending))  # the graph pauses here
    reason = answer_outcome(answer, pending, state, ctx.services.clock())
    trace.get_current_span().set_attribute("confirm_outcome", reason)
    if reason != "confirmed":
        return cancel(ctx, state, turn, pending, reason, answer)
    turn = {
        **turn,
        "delete": {**delete_of(turn), "outcome": "confirmed"},
        "confirm_text": answer,
        "confirmation_prompt": pending.get("text", ""),
    }
    return {"turn": turn}


# --- apply ----------------------------------------------------------------------------------------
def deleted_text(ctx: TurnContext, data: dict[str, Any]) -> str:
    prompts = ctx.services.prompts
    text = prompts.reply(
        "delete.deleted", count=data["count"], batch_id=data["batch_id"]
    )
    if data.get("files_moved"):
        text += " " + prompts.reply("delete.files_moved", folder=data["folder"] + "/")
    if data.get("files_missing"):
        text += " " + prompts.reply(
            "delete.files_missing", files=", ".join(data["files_missing"])
        )
    return text


@node("apply_delete")
def apply_delete(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """The only place that creates a `ConfirmationGrant` (spec 33, B2.4)."""
    ctx = runtime.context
    turn = turn_of(state)
    pending = dict(state.get("pending_confirmation") or {})
    report_ids = sorted(pending.get("report_ids") or [])
    expected = ids_hash(
        pending.get("owner_id", ""),
        pending.get("conversation_id", ""),
        pending.get("nonce", ""),
        report_ids,
    )
    if (
        expected != pending.get("ids_hash")
        or pending.get("owner_id") != ctx.user.id
        or pending.get("conversation_id") != state.get("conversation_id")
        or pending.get("count") != len(report_ids)
    ):
        return cancel(
            ctx, state, turn, pending, "internal_error", turn.get("confirm_text")
        )
    grant = ConfirmationGrant(pending["ids_hash"], tuple(report_ids), ctx.user.id)
    tool_ctx = ToolContext(
        user=ctx.user, budget=ctx.budget, store=ctx.store, grant=grant
    )
    result = ctx.services.tools.run(
        "delete_reports",
        tool_ctx,
        {
            "report_ids": report_ids,
            "ids_hash": pending["ids_hash"],
            "conversation_id": state.get("conversation_id"),
        },
    )
    if not result.ok:
        return cancel(ctx, state, turn, pending, "changed", turn.get("confirm_text"))
    confirm_span(
        "apply", "deleted", count=result.data["count"], batch_id=result.data["batch_id"]
    )
    data = {
        **delete_of(turn),
        "outcome": "deleted",
        "batch_id": result.data["batch_id"],
        "report_ids": report_ids,  # spec 34: the pinned report list
    }
    out = end_turn({**turn, "delete": data}, deleted_text(ctx, result.data))
    return {**out, "pending_confirmation": None}


# --- the subgraph ---------------------------------------------------------------------------------
def _ended(state: AgentState) -> bool:
    return bool(turn_of(state).get("answer"))


def after_parse(state: AgentState) -> str:
    if _ended(state):
        return END
    return (
        "undo_delete"
        if delete_of(turn_of(state)).get("scope") == "undo"
        else "find_reports"
    )


def _pending_or_end(target: str) -> Callable[[AgentState], str]:
    def route(state: AgentState) -> str:
        if _ended(state) or not state.get("pending_confirmation"):
            return END
        return target

    return route


def build_delete_flow() -> Any:
    g = StateGraph(AgentState, context_schema=TurnContext, output_schema=DeleteOutput)
    g.add_node("parse_delete", parse_delete)
    g.add_node("undo_delete", undo_delete)
    g.add_node("find_reports", find_reports)
    g.add_node("confirm_delete", confirm_delete)
    g.add_node("apply_delete", apply_delete)
    g.add_edge(START, "parse_delete")
    g.add_conditional_edges(
        "parse_delete",
        after_parse,
        {"undo_delete": "undo_delete", "find_reports": "find_reports", END: END},
    )
    g.add_edge("undo_delete", END)
    g.add_conditional_edges(
        "find_reports",
        _pending_or_end("confirm_delete"),
        {"confirm_delete": "confirm_delete", END: END},
    )
    g.add_conditional_edges(
        "confirm_delete",
        _pending_or_end("apply_delete"),
        {"apply_delete": "apply_delete", END: END},
    )
    g.add_edge("apply_delete", END)
    return g.compile()


capabilities.register("delete")  # spec 37, B2.5
