"""Tool interface (spec 00, B3). Spec 33 registers the first tool, `delete_reports`.

A tool with the side effect `destructive` or `external_send` runs only after the confirmation
gate of spec 33. The registry refuses it without a valid grant. Only the node `apply_delete`
creates a grant, after the user typed the exact confirmation text. No model output can make one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.tracing import span

SideEffect = Literal["read", "write", "destructive", "external_send"]


@dataclass(frozen=True)
class ConfirmationGrant:
    """The user's typed confirmation for one exact list of IDs (spec 33, B2.4)."""

    ids_hash: str
    report_ids: tuple[str, ...]
    owner_id: str

    def matches(self, args: BaseModel, user_id: str) -> bool:
        """True when the grant belongs to this user and to exactly these arguments."""
        ids = tuple(sorted(getattr(args, "report_ids", ()) or ()))
        return (
            self.owner_id == user_id
            and bool(self.ids_hash)
            and self.ids_hash == getattr(args, "ids_hash", None)
            and self.report_ids == ids
        )


@dataclass
class ToolContext:
    user: Any  # identity.UserContext
    budget: Any  # limits.TurnBudget
    bq: Any  # bq.BigQueryService
    store: Any  # sqlite3.Connection
    grant: ConfirmationGrant | None = None  # spec 33: the confirmation for this call


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_type: ErrorType | None = None
    user_message: str | None = None


class Tool(Protocol):
    name: str
    description: str
    args_model: type[BaseModel]  # becomes the Gemini function declaration
    side_effect: SideEffect

    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def run(self, name: str, ctx: ToolContext, raw_args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentError(
                ErrorType.TOOL_ERROR,
                "The assistant tried to use an unknown tool.",
                name,
            )
        # One `tool` span for each call, with the side effect (the evaluation check H-TOOL reads
        # it). A refused call also has its span, with the error type confirmation_rejected.
        with span(
            f"tool.{name}", "tool", tool=name, side_effect=tool.side_effect
        ) as sp:
            args = tool.args_model.model_validate(raw_args)
            if tool.side_effect in ("destructive", "external_send"):
                grant = ctx.grant
                if not isinstance(grant, ConfirmationGrant) or not grant.matches(
                    args, getattr(ctx.user, "id", "")
                ):
                    raise AgentError(
                        ErrorType.CONFIRMATION_REJECTED,
                        "This action needs your explicit confirmation first.",
                        name,
                    )
            result = tool.run(ctx, args)
            sp.set_attribute("ok", result.ok)
            return result
