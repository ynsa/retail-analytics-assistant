"""The chat loop (spec 37, B2.1). It reads the input, sends it to the session and shows the
result. All rules of the assistant are in the session.

- Text goes to `session.send()`. When the result has `replay_input`, the text was not the
  delete confirmation: the delete is cancelled, and the same text goes again as a new question.
- A line that starts with `/` is a command. Commands make no model call and no query.
- `/exit` and Ctrl-D end the chat. Two Ctrl-C within 2 s at the prompt also end it. A Ctrl-C at
  the `confirm>` prompt cancels the waiting delete (spec 33). A Ctrl-C during an answer stops
  the answer and the running query (spec 36).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from opsfleet_agent.cli.commands import run_command
from opsfleet_agent.cli.render import Ui
from opsfleet_agent.resilience import messages

CTRL_C_WINDOW_S = 2.0
STOPPED = "Stopped."
CANCELLED = "Delete cancelled. Nothing was deleted."
EXIT_COMMANDS = ("/exit", "/quit")

Reader = Callable[[str], str]


def make_reader(ui: Ui) -> Reader:
    """`input()` through Rich. In plain mode the line is shown after the prompt, so a scripted
    session reads like a terminal session."""
    echo = not sys.stdin.isatty()

    def read(prompt: str) -> str:
        text = ui.console.input(prompt)
        if echo:
            ui.console.print(text, markup=False, highlight=False)
        return text

    return read


def send(ui: Ui, session: Any, text: str) -> None:
    result = _send_once(ui, session, text)
    if result.replay_input:
        _send_once(ui, session, text)


def _send_once(ui: Ui, session: Any, text: str) -> Any:
    shown: list[bool] = []
    with ui.status() as status:

        def on_answer(result: Any) -> None:
            shown.append(True)
            ui.result(result)
            status.set("Updating the conversation memory")

        result = session.send(text, on_answer=on_answer, on_progress=status.progress)
    if not shown:
        ui.result(result)
    return result


def guarded_send(ui: Ui, session: Any, text: str, debug: bool = False) -> None:
    """Spec 36, B2.6 #3: no exception of one turn ends the CLI. The session already catches its
    own errors. This is the last net."""
    from opsfleet_agent.resilience.safe_node import log_exception

    try:
        send(ui, session, text)
    except KeyboardInterrupt:
        bq = getattr(getattr(session, "services", None), "bq", None)
        cancel = getattr(bq, "cancel_running", None)
        if callable(cancel):
            cancel()
        ui.line(STOPPED, "yellow")
    except Exception:  # noqa: BLE001 - the chat continues after any bug (spec 36)
        log_exception("cli", None)
        if debug:
            ui.console.print_exception()
        ui.line(f"Error: {messages.text('internal')}", "red")


def chat_loop(
    ui: Ui,
    session: Any,
    user_id: str,
    read: Reader | None = None,
    clock: Callable[[], float] = time.monotonic,
    debug: bool = False,
) -> None:
    read = read or make_reader(ui)
    last_ctrl_c: float | None = None
    while True:
        pending = _safe(lambda: session.pending is not None, False)
        prompt = "confirm> " if pending else f"{user_id}> "
        try:
            line = read(prompt).strip()
        except EOFError:
            ui.blank()
            break
        except KeyboardInterrupt:
            ui.blank()
            now = clock()
            if pending:
                _safe(lambda: session.cancel_pending("abandoned"), None)
                ui.line(CANCELLED, "yellow")
                last_ctrl_c = now
                continue
            if last_ctrl_c is not None and now - last_ctrl_c <= CTRL_C_WINDOW_S:
                break
            last_ctrl_c = now
            ui.line("Press Ctrl-C again to end, or type /exit.", "dim")
            continue
        last_ctrl_c = None
        if not line:
            continue  # also at `confirm>`: the prompt shows again
        if line.lower() in EXIT_COMMANDS:
            break
        if line.startswith("/"):
            _command(ui, session, line, debug)
            continue
        guarded_send(ui, session, line, debug)


def _command(ui: Ui, session: Any, line: str, debug: bool) -> None:
    from opsfleet_agent.resilience.safe_node import log_exception

    try:
        run_command(ui, session, line)
    except Exception:  # noqa: BLE001 - a command bug never ends the chat
        log_exception("command", None)
        if debug:
            ui.console.print_exception()
        ui.line(f"Error: {messages.text('internal')}", "red")


def end_chat(ui: Ui, session: Any, user_id: str) -> None:
    """Cancel a waiting delete, then show the resume hint (A2.9)."""
    if _safe(session.close, None) is not None:
        ui.line(CANCELLED, "yellow")
    if _safe(session.turn_count, 1) > 0:
        ui.line(saved_line(session.conversation_id, user_id))


def _safe[T](call: Callable[[], T], default: T) -> T:
    """A session call outside a turn. An error is logged, and the chat goes on (spec 36)."""
    from opsfleet_agent.resilience.safe_node import log_exception

    try:
        return call()
    except Exception:  # noqa: BLE001 - for example a checkpoint database error
        log_exception("chat loop", None)
        return default


def saved_line(conversation_id: str, user_id: str) -> str:
    return (
        f"Conversation {conversation_id} saved. Continue it with: "
        f"opsfleet-agent chat --user {user_id} --resume {conversation_id}"
    )
