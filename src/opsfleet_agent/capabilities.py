"""The parts of the prototype that are active (spec 37, B2.5).

Each part registers its capability when its module loads. The CLI shows the line
"Development build: <names> not active yet." while a part is missing. When all parts are
registered, the line does not appear.
"""

from __future__ import annotations

# The parts in the order of the specs, with the text that the CLI shows.
TEXTS: dict[str, str] = {
    "guard_full": "brand scope and PII protection",  # spec 30
    "agent_core": "the agent core",  # spec 31
    "reports": "reports",  # spec 32
    "delete": "delete with confirmation",  # spec 33
    "memory": "long-conversation memory",  # spec 34
    "observability": "/trace and /stats",  # spec 35
    "resilience": "retries and the fallback model",  # spec 36
}
_registered: set[str] = set()


def register(name: str) -> None:
    if name not in TEXTS:
        raise ValueError(f"unknown capability: {name}")
    _registered.add(name)


def registered() -> frozenset[str]:
    return frozenset(_registered)


def missing(active: frozenset[str] | None = None) -> list[str]:
    active = registered() if active is None else active
    return [name for name in TEXTS if name not in active]


def development_line(names: list[str]) -> str | None:
    """`Development build: delete with confirmation and long-conversation memory not active
    yet.` None when no part is missing."""
    if not names:
        return None
    texts = [TEXTS.get(n, n) for n in names]
    joined = (
        texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " and " + texts[-1]
    )
    return f"Development build: {joined} not active yet."
