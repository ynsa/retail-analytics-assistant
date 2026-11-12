"""Nodes that end a turn with a prepared text and no model call (spec 31, B2.2 and B2.4).

- `prepared_reply`: chitchat, off-topic, unsafe and clarify routes.
- `explain_block`: the guard message of a policy block (no retry).
- `give_up`: the message after the last failed attempt of a step.
- `report_flow`, `delete_flow`: placeholders until specs 32 and 33 replace them.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.nodes.common import end_turn, is_manager, node, turn_of
from opsfleet_agent.agent.nodes.loop_control import current_step
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.identity import UserContext

MAX_CLARIFY_OPTIONS = 4
CHITCHAT_KINDS = ("greeting", "thanks", "acknowledgement", "format_only")
UNSAFE_KINDS = ("prompt_request", "identity_change", "rule_change", "other")


def first_name(user: UserContext) -> str:
    return user.name.split(" (")[0].strip() or user.id


def role_text(lib: PromptLibrary, user: UserContext) -> str:
    return lib.reply(f"roles.{'manager' if is_manager(user) else 'ceo'}")


def prepared_text(
    lib: PromptLibrary, user: UserContext, intent: str, route: dict
) -> str:
    if intent == "off_topic":
        return lib.reply("off_topic")
    if intent == "unsafe":
        kind = route.get("unsafe_kind")
        kind = kind if kind in UNSAFE_KINDS else "other"
        return lib.reply(
            f"unsafe.{kind}", name=first_name(user), role_text=role_text(lib, user)
        )
    if intent == "clarify":
        options = [o.strip() for o in route.get("clarify_options") or [] if o.strip()]
        question = (route.get("clarify_question") or "").strip()
        return lib.reply(
            "clarify",
            question=question or lib.reply("capabilities"),
            options=options[:MAX_CLARIFY_OPTIONS],
        )
    facts = noted_facts(lib, route)
    if facts:
        return lib.reply("memory.noted", facts=facts)
    kind = route.get("chitchat_kind")
    if route.get("format_request") and kind in (None, "acknowledgement", "format_only"):
        kind = "format_only"
    kind = kind if kind in CHITCHAT_KINDS else "acknowledgement"
    return lib.reply(f"chitchat.{kind}", first_name=first_name(user))


def noted_facts(lib: PromptLibrary, route: dict) -> str:
    """The filters and terms that the user set in this message (spec 34), as one text."""
    parts: list[str] = []
    if route.get("clear_filters"):
        parts.append(lib.reply("memory.filters_cleared"))
    for item in route.get("set_filters") or []:
        field, value = item.get("field"), item.get("value")
        if field and value:
            parts.append(f"{field} = {value}")
        elif field:
            parts.append(lib.reply("memory.filter_removed", field=field))
    for item in route.get("set_definitions") or []:
        if item.get("term") and item.get("meaning"):
            parts.append(f"{item['term']} = {item['meaning']}")
    return "; ".join(parts)


@node("prepared_reply")
def prepared_reply(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    text = prepared_text(
        ctx.services.prompts,
        ctx.user,
        turn.get("intent") or "chitchat",
        turn.get("route") or {},
    )
    return end_turn(turn, text)


@node("explain_block")
def explain_block(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    turn = turn_of(state)
    return end_turn(turn, turn.get("block_message") or "", turn.get("error_type"))


@node("give_up")
def give_up(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    lib = runtime.context.services.prompts
    turn = turn_of(state)
    error_type = turn.get("error_type") or "default"
    reasons = lib.reply_value("give_up_reasons")
    helps = lib.reply_value("give_up_help")
    purpose = current_step(turn).get("purpose") or turn.get("question", "")
    text = lib.reply(
        "give_up",
        attempts=runtime.context.services.settings.agent.max_attempts_per_step,
        purpose=purpose.rstrip(". "),
        reason=reasons.get(error_type, reasons["default"]),
        help=helps.get(error_type, helps["default"]),
    )
    return end_turn(turn, text, turn.get("error_type"))


def _not_built(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    return end_turn(turn_of(state), runtime.context.services.prompts.reply("not_built"))


# Extension points: specs 32 and 33 pass their own flows to `build_graph(flows=...)`.
report_flow = node("report_flow")(_not_built)
delete_flow = node("delete_flow")(_not_built)
