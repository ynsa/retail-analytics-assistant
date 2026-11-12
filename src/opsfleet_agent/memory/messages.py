"""The recent window of the conversation (spec 34, B2.3).

`AgentState.messages` keeps only the last turns, word for word. Each item has the turn number and
a kind. A query result is a reference with a short preview (`kind = "result_ref"`), never the rows.

The reducer accepts two kinds of updates:

- a list of messages: they are added at the end (`append(items)`),
- `compact(n)`: the n oldest turns go away, after the summary has them.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

MessageKind = Literal["question", "answer", "reply", "confirmation", "result_ref"]


class Message(TypedDict):
    role: Literal["user", "assistant", "tool"]
    turn_no: NotRequired[int]
    kind: NotRequired[MessageKind]
    text: str  # for a result_ref: the purpose of the query
    ref: NotRequired[str]  # "res_" and 8 characters
    columns: NotRequired[list[str]]
    row_count: NotRequired[int]
    preview: NotRequired[list[list[str]]]  # at most 5 masked and formatted rows


COMPACT = "compact"


def append(items: list[Message]) -> list[Message]:
    return list(items)


def compact(turns: int) -> dict[str, Any]:
    """The update that drops the `turns` oldest turns of the window."""
    return {"op": COMPACT, "turns": int(turns)}


def messages_reducer(
    left: list[Message] | None, right: list[Message] | dict[str, Any] | None
) -> list[Message]:
    current = list(left or [])
    if right is None:
        return current
    if isinstance(right, dict):
        if right.get("op") == COMPACT:
            drop = set(turn_numbers(current)[: int(right.get("turns") or 0)])
            numbered = with_turn_numbers(current)
            return [m for m, n in numbered if n not in drop]
        return current
    return current + list(right)


def with_turn_numbers(messages: list[Message]) -> list[tuple[Message, int]]:
    """Each message with its turn number. Messages of an older version have no `turn_no`: a user
    question then starts the next turn."""
    out: list[tuple[Message, int]] = []
    current = 0
    for message in messages:
        number = message.get("turn_no")
        if number is None:
            if message.get("role") == "user" and message.get("kind") != "confirmation":
                current += 1
            number = max(current, 1)
        else:
            current = int(number)
        out.append((message, int(number)))
    return out


def turn_numbers(messages: list[Message]) -> list[int]:
    """The turn numbers in the window, oldest first."""
    return list(dict.fromkeys(n for _, n in with_turn_numbers(messages)))


def turns_of(messages: list[Message]) -> list[tuple[int, list[Message]]]:
    """The window as turns: (turn number, its messages), oldest first."""
    grouped: dict[int, list[Message]] = {}
    for message, number in with_turn_numbers(messages):
        grouped.setdefault(number, []).append(message)
    return list(grouped.items())


def cut_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def message_lines(message: Message, answer_chars: int) -> str:
    """One message as a line of the history block."""
    role = message.get("role")
    if message.get("kind") == "result_ref" or role == "tool":
        columns = ", ".join(message.get("columns") or [])
        head = (
            f"result {message.get('ref', '?')} ({message.get('text', '')}): "
            f"{message.get('row_count', 0)} rows; columns {columns or '(none)'}"
        )
        lines = [head]
        for row in message.get("preview") or []:
            lines.append("  | " + " | ".join(str(v) for v in row) + " |")
        return "\n".join(lines)
    text = message.get("text") or ""
    if role == "assistant":
        text = cut_text(text, answer_chars)
    return f"{role}: {text}"


def turn_text(number: int, messages: list[Message], answer_chars: int) -> str:
    body = "\n".join(message_lines(m, answer_chars) for m in messages)
    return f"[turn {number}]\n{body}"
