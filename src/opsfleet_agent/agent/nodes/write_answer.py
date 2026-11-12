"""`write_answer`: the model writes the answer from the formatted results (spec 31, B2.5).

After the model call, code:

1. removes copied `<data>` blocks, then replaces each calculation placeholder with the computed
   value,
2. runs the figure check: a number that no result explains is replaced,
3. adds the footer: period, brands, definitions and assumptions.

`scan_output` then runs the output scanner and the prompt-leak check.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from opentelemetry import trace

from opsfleet_agent.agent.calc import (
    check_figures,
    numbers_in,
    resolve_placeholders,
    result_numbers,
)
from opsfleet_agent.agent.footer import build_footer
from opsfleet_agent.agent.formatting import strip_data_markup
from opsfleet_agent.agent.nodes.common import (
    INTERNAL_MESSAGE,
    brands_text,
    is_manager,
    node,
    turn_of,
    user_format,
)
from opsfleet_agent.agent.periods import Calendar
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.memory.context_builder import ModelRequest, builder_of

TRACEABLE_HISTORY_MESSAGES = 6


def _definitions(ctx: TurnContext, turn: TurnState) -> str:
    lib = ctx.services.prompts
    keys = [k for p in turn.get("plans") or [] for k in p.get("definitions_used") or []]
    lines = [
        f"- {k}: {lib.definitions[k].meaning}"
        for k in dict.fromkeys(keys)
        if k in lib.definitions
    ]
    return "\n".join(lines) or "(none)"


def build_request(
    state: AgentState, ctx: TurnContext, turn: TurnState, system: str
) -> ModelRequest:
    """The context of `write_answer` (spec 34, B2.1): the pinned facts, the summary, the recent
    turns, the definitions and notes of this turn, the data blocks and the question."""
    notes = "\n".join(f"- {n}" for n in turn.get("notes") or []) or "(none)"
    return builder_of(ctx.services).build(
        "write_answer",
        system=system,
        state=state,
        middle=[
            f"<definitions>\n{_definitions(ctx, turn)}\n</definitions>",
            f"<notes>\n{notes}\n</notes>",
        ],
        results=list(turn.get("results") or []),
        user_message=turn["question"],
    )


def allowed_numbers(
    state: AgentState, ctx: TurnContext, turn: TurnState, computed: list[float]
) -> list[float]:
    """Values that explain a number in the answer (spec 31, "Calculated figures" rule 4)."""
    allowed = result_numbers(turn.get("results") or []) + computed
    allowed += numbers_in(turn.get("question", ""))
    allowed += numbers_in(" ".join(turn.get("notes") or []))
    allowed.append(float(len(ctx.user.brands)))
    # Figures of earlier answers passed this check when they were shown.
    earlier = [
        m for m in (state.get("messages") or [])[:-1] if m["role"] == "assistant"
    ]
    for message in earlier[-TRACEABLE_HISTORY_MESSAGES:]:
        allowed += numbers_in(message["text"])
    return allowed


def finalize(
    state: AgentState, ctx: TurnContext, turn: TurnState, text: str
) -> TurnState:
    lib = ctx.services.prompts
    sp = trace.get_current_span()

    def calc_error(reason: str) -> None:
        sp.add_event("calc_error", {"reason": reason[:120]})

    results = turn.get("results") or []
    text = strip_data_markup(text)  # the data markup is for the model only
    calc = resolve_placeholders(text, results, on_error=calc_error)
    check = check_figures(
        calc.text,
        allowed_numbers(state, ctx, turn, calc.computed),
        lib.reply("figure_removed"),
    )
    sp.set_attribute("calculated_figures", len(calc.computed))
    sp.set_attribute("calc_errors", calc.errors)
    sp.set_attribute("checked_figures", check.checked)
    sp.set_attribute("unverified_figures", check.unverified)
    answer = check.text.strip() or INTERNAL_MESSAGE
    footer = ""
    if results:
        footer = build_footer(turn, ctx.user, lib, Calendar.now().today)
        answer = f"{answer}\n\n{footer}"
    return {
        **turn,
        "answer": answer,
        "footer": footer,
        "calc_errors": calc.errors,
        "unverified_figures": check.unverified,
    }


@node("write_answer")
def write_answer(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    lib = ctx.services.prompts
    turn = turn_of(state)
    user = ctx.user
    scope_key = "manager" if is_manager(user) else "ceo"
    system = lib.system(
        "write_answer",
        persona=ctx.scratch.get("persona", ""),
        user_format=lib.format_text(user_format(state, turn)),
        max_rows=ctx.services.settings.agent.answer_max_rows,
        scope_phrase=lib.reply(
            f"scope_phrase.{scope_key}", brands=brands_text(user.brands)
        ),
    )
    request = build_request(state, ctx, turn, system)
    result = ctx.services.llm.generate(
        ctx.budget,
        system=request.system,
        prompt=request.prompt,
        purpose="write_answer",
        prompt_version=lib.version("safety", "write_answer", "definitions"),
    )
    return {"turn": finalize(state, ctx, turn, result.text)}
