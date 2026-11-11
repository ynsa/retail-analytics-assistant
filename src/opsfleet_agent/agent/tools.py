"""Tool interface (spec 00, B3). The skeleton has no tools yet. Specs 31–33 register them.

A tool with the side effect `destructive` or `external_send` runs only after the confirmation
gate of spec 33. The registry refuses it without a valid grant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from opsfleet_agent.errors import AgentError, ErrorType

SideEffect = Literal["read", "write", "destructive", "external_send"]


@dataclass
class ToolContext:
    user: Any  # identity.UserContext
    budget: Any  # limits.TurnBudget
    bq: Any  # bq.BigQueryService
    store: Any  # sqlite3.Connection
    grant: Any | None = None  # spec 33: a valid confirmation grant for this call


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
        if tool.side_effect in ("destructive", "external_send") and ctx.grant is None:
            raise AgentError(
                ErrorType.CONFIRMATION_REJECTED,
                "This action needs your explicit confirmation first.",
                name,
            )
        return tool.run(ctx, tool.args_model.model_validate(raw_args))
