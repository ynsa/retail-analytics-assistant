"""The user of a conversation (spec 00). The CLI creates it from --user. The model never sets it."""

from __future__ import annotations

from dataclasses import dataclass

from opsfleet_agent.config import Role, UsersConfig
from opsfleet_agent.errors import ConfigError


@dataclass(frozen=True)
class UserContext:
    id: str
    name: str
    role: Role
    brands: tuple[str, ...]  # empty for the ceo role: all brands

    @property
    def all_brands(self) -> bool:
        return self.role is Role.CEO

    def as_state(self) -> dict[str, object]:
        """Plain data for the graph state: the checkpoint stores only JSON-like values."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "brands": list(self.brands),
        }

    def describe(self) -> str:
        scope = "all brands" if self.all_brands else ", ".join(self.brands)
        return f"{self.name} · {self.role.value} · {scope}"


def resolve_user(users: UsersConfig, user_id: str) -> UserContext:
    for entry in users.users:
        if entry.id == user_id:
            return UserContext(
                id=entry.id,
                name=entry.name,
                role=entry.role,
                brands=tuple(entry.brands),
            )
    known = ", ".join(u.id for u in users.users)
    raise ConfigError(
        f"Unknown user '{user_id}'. Known users in config/users.yaml: {known}."
    )
