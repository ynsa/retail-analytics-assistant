# Spec 37: CLI

- **Status:** Done (2026-11-12)
- **Phase and stream:** Phase 2, stream C (last part of the stream)
- **Depends on:** 00, 01, 31, 32, 33, 34, 35, 36
- **Requirements covered:** Deliverable 4 (CLI-based chat). The user side of Req 3 (the confirmation panel), Req 5 (no crash, clear errors) and Req 7 (`/trace`, `/stats`).

---

## Part A — Product (review this part)

### A1. Goal

The final terminal interface. A user starts a chat with one command, asks questions, reads answers as formatted text and tables, and manages the saved reports with short commands. Errors and confirmations are clear, and the CLI never stops with a crash.

### A2. Behavior the user sees

1. **Start.** `opsfleet-agent chat --user maya` shows a greeting panel. It contains the name, the role, the brands, the date range of the data and the conversation ID. It ends with the hint "Type a question, or /help."
2. **Development build.** A part of the prototype is not ready yet. Then a line under the greeting names it, for example: "Development build: delete with confirmation is not active yet." When all parts are ready, the line does not appear.
3. **A question.** Maya types a question. A status line shows the current step and the seconds, for example "Running the query (BigQuery) … 4 s". The answer appears as formatted text, with tables where useful. A short grey line follows: "12.4 s · 3 model calls · 2 queries · 34 MB · trace 4f2a9c1e".
4. **Commands.** Commands start with `/`. They never call the AI model or BigQuery.

   | Command | Result |
   |---|---|
   | `/help` | The list of commands and 3 example questions |
   | `/reports` | A table of the user's reports: ID, title, period, date, files |
   | `/open <id>` | The report in the terminal. Charts show as a line with their title. |
   | `/export <id> --format html` or `--format md` | Writes the file and shows its path |
   | `/undo [batch]` | Restores the last deleted reports (spec 33) |
   | `/trace [trace]` | The steps of the last turn, or of the given trace (spec 35) |
   | `/stats [--days N]` | The user's statistics (spec 35) |
   | `/exit` | Ends the chat |

5. **Delete confirmation.** Maya asks to delete reports. A red-bordered panel "Delete these reports?" lists the reports (ID, title, date, why it matches). Under it: "To delete these 3 reports, type: delete 3. Any other answer cancels. You can undo for 30 days with /undo." The prompt changes from `maya>` to `confirm>`.
6. **After the confirmation.** A green line: "Deleted 3 reports. Undo with /undo (batch D-4F8KQ)." Or a yellow line: "Delete cancelled. Nothing was deleted."
7. **Refusals and blocks.** A refusal, a guard block or a limit appears as one yellow line that starts with "Not possible:" or "Limit:".
8. **Errors.** A service error or an internal error appears as one red line that starts with "Error:" and ends with the trace ID. No Python traceback appears.
9. **End.** `/exit` or Ctrl-D shows: "Conversation C-8M2QX saved. Continue it with: opsfleet-agent chat --user maya --resume C-8M2QX".
10. **Other commands of the program.**

    | Command | Result |
    |---|---|
    | `opsfleet-agent conversations --user maya` | Maya's conversations of the last 90 days (spec 34) |
    | `opsfleet-agent stats [--all] [--days N] [--export csv\|json]` | Statistics and export (spec 35) |
    | `opsfleet-agent purge [--dry-run]` | Permanent delete of reports deleted more than 30 days ago (spec 33), and the retention cleanup (spec 35) |

11. **Scripted input.** `printf "How many orders in 2024?\n/exit\n" | opsfleet-agent chat --user maya` works. Without a terminal, the CLI prints plain text without colors and without the status line.

### A3. Scope

- **In scope:**
  - the Typer commands and options,
  - the chat loop and the slash commands,
  - the output of answers, tables, reports, confirmations, errors and progress,
  - the development-build line,
  - the plain mode without a terminal,
  - the session interface between the CLI and the assistant core.
- **Out of scope:**
  - the logic behind each command (specs 32–36),
  - the local web UI (specs 40 and 52),
  - a user switch inside a chat (gap analysis B1: restart with another `--user`),
  - command history saved to disk.

### A4. Limits the user sees

| Item | Value | Message |
|---|---|---|
| Message length | 4,000 characters (spec 31) | "Your message is too long. Please keep it under 4,000 characters." |
| Unknown command | — | "Unknown command '/x'. Type /help." |
| Wrong command arguments | — | The usage line of the command, for example "Usage: /export <id> --format html\|md" |
| Terminal width | 80 columns or more | Narrower terminals wrap the tables |

### A5. Done when

- [x] `opsfleet-agent --help` and `opsfleet-agent chat --help` list all commands and options.
- [x] Each slash command in A2.4 works, and makes no model call and no query. A unit test with a fake session checks each one.
- [x] The delete panel, the `confirm>` prompt and both outcomes look as in B2.4.
- [x] Text that is not the confirmation cancels the delete, and the CLI sends the same text as a new question.
- [x] Each error type of spec 00 appears as one line with the right prefix and color, and with the trace ID for failures.
- [x] No traceback appears on the screen without `--debug`.
- [x] The development-build line names the missing parts, and disappears when all parts are ready.
- [x] The scripted input of A2.11 works, and its output has no color codes.
- [x] The output fits an 80-column terminal. Snapshot tests check the main screens.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | Interface between the CLI and the core | A small `AssistantSession` class (B3). The CLI only shows the results. The session holds the graph, the pending confirmation and the commands. | The CLI calls the graph directly (spec 01) | The local web UI (spec 52) and the evaluation runner (spec 23) can use the same session. No safety rule lives in the CLI. |
| ⚑ | Footer line with time, calls, queries, bytes and trace ID | On by default in the prototype. `cli.show_footer: false` turns it off. | Off | Reviewers and testers see the cost and speed of each answer. Executives in production do not use this CLI. |
| | Libraries | Typer for commands, Rich for output | `argparse`, plain text | Decided in the architecture. |
| | Line input | Python `input()` through Rich. `readline` history in memory only. | `prompt_toolkit` | No extra dependency. A history file can store personal text. |
| | Progress | LangGraph `stream(..., stream_mode="updates")`. Each node name maps to a status text. | No progress | A 20–90 s wait needs a visible step. |
| | Answer timing | The CLI prints the answer when `scan_output` ends. The memory update (spec 34) runs after that, under the status "Updating the conversation memory". | Print at the graph end | The user reads the answer earlier. |
| | Plain mode | When stdin or stdout is not a terminal: no colors, no status line, the same texts | Always rich output | Scripted demos and tests get clean text. Rich also respects `NO_COLOR`. |

### B2. Design

#### B2.1 The chat loop

```mermaid
flowchart TD
    start(["chat --user maya"]) --> sess["AssistantSession.start()<br/>config, brand check, cache, resume"]
    sess -->|"error"| stop(["Message and exit code 2"])
    sess --> greet["Greeting panel and<br/>development-build line"]
    greet --> prompt{{"maya> or confirm>"}}
    prompt -->|"/command"| cmd["session.command()"]
    cmd --> prompt
    prompt -->|"text"| send["session.send(text)<br/>status line from stream"]
    send -->|"answer"| ans["Markdown answer + footer"]
    send -->|"confirmation"| panel["Delete panel"]
    send -->|"message or error"| line["One colored line"]
    send -->|"replay_input"| resend["Cancel line, then send the same text again"]
    ans --> prompt
    panel --> prompt
    line --> prompt
    resend --> send
    prompt -->|"/exit, Ctrl-D"| bye["Resume hint"]
```

The CLI wraps each `send()` and `command()` call in `try/except`. Any exception there becomes the `internal_error` line (spec 36).

#### B2.2 Status texts

| Node | Status text |
|---|---|
| `route` | Reading the question |
| `write_sql` | Writing the query |
| `guard` | Checking the query |
| `run_query` | Running the query (BigQuery) |
| `check_empty` | Checking why the result is empty |
| `write_answer` | Writing the answer |
| `plan_report` | Planning the report |
| `write_report` | Writing the report |
| `render` | Drawing the charts |
| `summarize` | Updating the conversation memory |
| Other | Working |

The status line also shows the attempt number on a retry, for example "Writing the query (attempt 2 of 3)".

#### B2.3 Output formats

- **Answer.** `rich.markdown.Markdown(text)`. Rich markup in the model text is not interpreted. Tables in the answer become Rich tables.
- **Footer.** `<seconds> s · <model calls> model calls · <queries> queries · <MB billed> MB · trace <8 characters>`. Grey. Only after answers and reports.
- **`/reports`.** A Rich table: `ID`, `Title` (at most 50 characters), `Period`, `Created` (local date), `Files` (`HTML`, `MD` or empty). Under it: "Showing the newest 50 of N reports" when necessary. No reports: "You have no saved reports."
- **`/open`.** A panel with the title. The body as Markdown. Each chart as one line: "[Chart: Revenue by month (line). Export as HTML to see it.]".
- **`/export`.** "Saved: data/reports/maya/R-7K2PQ.html", as a `file://` link when the terminal supports links.
- **`/trace`, `/stats`.** The views of spec 35 (a Rich tree and Rich tables).
- **Message lines:**

  | Kind | Prefix | Color | Trace ID |
  |---|---|---|---|
  | Refusal (off-topic, unsafe) | none | default | no |
  | Guard policy block, empty-result note | "Not possible:" | yellow | no |
  | `limit_exceeded` | "Limit:" | yellow | yes |
  | Delete cancelled | none | yellow | no |
  | Delete done, undo done, export done | none | green | no |
  | `llm_*`, `bq_*`, `tool_error`, `internal_error`, `give_up` | "Error:" | red | yes |

#### B2.4 Delete confirmation panel

```text
╭─ Delete these reports? ──────────────────────────────────────────────╮
│ ID       Title                                Created     Why        │
│ R-7K2PQ  Q1 2026 sales review                 2026-11-12  this conv. │
│ R-3M9XA  Levi's returns, October              2026-11-12  this conv. │
│ R-9QF2D  Calvin Klein vs Carhartt, Q3         2026-11-12  this conv. │
│                                                                      │
│ To delete these 3 reports, type: delete 3                            │
│ Any other answer cancels. You can undo for 30 days with /undo.       │
╰──────────────────────────────────────────────────────────────────────╯
confirm>
```

- The border is red. The expected text is bold.
- The rows and the expected text come from the `interrupt()` payload (spec 33). The CLI does not build them.
- At `confirm>`, the commands `/help`, `/reports`, `/open`, `/trace` and `/stats` work and keep the pending delete. `/undo`, `/exit`, Ctrl-C and Ctrl-D cancel it (spec 33).
- An empty line at `confirm>` does nothing and shows the prompt again.

#### B2.5 Development-build line

- Each part registers a capability when its module loads: `guard_full` (30), `agent_core` (31), `reports` (32), `delete` (33), `memory` (34), `observability` (35), `resilience` (36).
- `AssistantSession.missing_capabilities()` returns the parts that are not registered.
- The CLI prints "Development build: <names> not active yet." when the list is not empty. Each name has a plain text, for example `delete` = "delete with confirmation".
- The line of spec 01 ("brand scope and PII protection are not active") becomes the case `guard_full` missing.

#### B2.6 Options, start and exit codes

Options of `chat`: `--user <id>` (required), `--resume <conversation_id>` (spec 34), `--refresh-cache` (spec 30), `--debug` (tracebacks on the screen).

| Situation | Message | Exit code |
|---|---|---|
| Normal end | Resume hint | 0 |
| Unknown user | "User 'x' is not in config/users.yaml." | 2 |
| Wrong brand of the user (spec 30) | The spec 30 message | 2 |
| Resume not found or too old (spec 34) | The spec 34 message | 2 |
| Missing `.env` value | "GOOGLE_API_KEY is not set. See the setup guide." | 2 |
| No schema cache and BigQuery down (spec 36) | The spec 36 message | 3 |

### B3. Files and interfaces

```text
src/opsfleet_agent/
  session.py            # AssistantSession, TurnOutcome, CommandOutcome, capabilities
  cli/
    main.py             # Typer app: chat, conversations, stats, purge (replaces the spec 01 version)
    chat_loop.py        # the loop of B2.1
    commands.py         # slash command parsing (shlex) and dispatch to the session
    render.py           # Rich output: answers, tables, panels, lines, footer
    status.py           # node name to status text
```

**`AssistantSession`:**

```python
class AssistantSession:
    @classmethod
    def start(
        cls, user_id: str, resume: str | None, settings: Settings
    ) -> "AssistantSession": ...

    user: UserContext
    conversation_id: str
    pending: ConfirmationView | None

    def greeting(self) -> Greeting: ...
    def missing_capabilities(self) -> list[str]: ...
    def send(
        self, text: str, on_progress: Callable[[ProgressEvent], None] | None = None
    ) -> TurnOutcome: ...
    def command(self, name: str, args: list[str]) -> CommandOutcome: ...
    def close(self) -> CloseInfo: ...


@dataclass
class TurnOutcome:
    kind: Literal["answer", "confirmation", "message", "error"]
    text: str
    error_type: ErrorType | None
    confirmation: ConfirmationView | None  # rows, count, expected text
    replay_input: bool  # spec 33, other text at confirm>
    footer: FooterInfo | None  # seconds, calls, queries, bytes, trace_id
```

- When `pending` is set, `send(text)` resumes the graph with `Command(resume=text)`. Otherwise it starts a new turn with the text as input.
- The session writes the turn row for an exception (spec 35).

**Settings:**

```yaml
cli:
  show_footer: true
  status_line: true
```

**New dependencies:** `typer`, `rich`.

**Changes to spec 01:** `cli/main.py` becomes a thin Typer app over `AssistantSession`. The spec 01 development note becomes the capability line of B2.5.

### B4. Tests

Unit tests use Typer's `CliRunner` and a fake `AssistantSession`. Snapshot tests use `rich.console.Console(width=80, record=True)`.

| # | Case | Expected |
|---|---|---|
| 1 | `--help` of each command | All options listed |
| 2 | Start with all capabilities | No development-build line |
| 3 | Start without `delete` and `memory` | The line names both |
| 4 | Each slash command | The expected output. The fake session records 0 model calls and 0 queries. |
| 5 | `/export R-1 --format pdf` | The usage line |
| 6 | `/nothing` | "Unknown command '/nothing'. Type /help." |
| 7 | Answer with a Markdown table | Snapshot at 80 columns |
| 8 | Confirmation outcome | The panel snapshot, prompt `confirm>` |
| 9 | `/reports` at `confirm>` | The list. The pending delete stays. |
| 10 | Other text at `confirm>` with `replay_input` | The cancel line, then a second `send()` with the same text |
| 11 | Each error type | One line, the right prefix, the trace ID for failures |
| 12 | Exception in `send()` | The `internal_error` line, no traceback, the loop continues |
| 13 | `--debug` and an exception | The traceback appears |
| 14 | Piped input (no terminal) | Plain text, no ANSI codes, no status line |
| 15 | Ctrl-D | The resume hint, exit code 0 |
| 16 | Each start error in B2.6 | The message and the exit code |
| 17 | Progress events | The status texts of B2.2 in order |

**Manual demo** (before the submission, inside Docker): the user journey of the product definition (section 2.2), with Maya and with the CEO. About 25 Gemini calls and 12 queries. The runner of the demo states the count first.

### B5. Cost

- Unit tests: 0 Gemini calls, 0 queries.
- Manual demo: about 25 Gemini calls and 12 queries (about 400 MB billed).
- Slash commands: 0 calls and 0 queries.

### B6. Risks and open points

- `input()` on Windows has no line editing. The main way to run the prototype is Docker (Linux), where `readline` works.
- Some terminals do not render the box characters or the links. Rich falls back to plain characters.
- The CLI depends on all specs of stream C. It is the last part, so its snapshot tests change when the texts of other specs change.
- `/stats` and `/trace` read local files. In the Docker image, these files are in the `data/` volume.

### B7. Implementation notes (2026-11-12)

#### Deviations from this spec

| # | Spec text | Implementation | Reason |
|---|---|---|---|
| 1 | B3: `AssistantSession.start(user_id, resume, settings)` | `startup.start_session(config, user_id, resume, refresh_cache)` returns `Started` (the session, the user, the warnings, the retention result). Each start problem raises `StartError` with the message and the exit code of B2.6. | The session constructor and `AssistantSession.resume()` of specs 33 and 34 stay as they are. The evaluation adapter uses them. |
| 2 | B3: `TurnOutcome` with `kind` and `FooterInfo` | The session keeps `TurnResult` (specs 31–36). `display.describe(result)` gives the kind (`answer`, `confirmation`, `message`, `error`), the color and the prefixed text. `display.footer_line(result)` gives the footer line from `totals`. `TurnResult.footer` stays the line with the period and the brands. | The rules of B2.3 are in one core module, so the local UI shows the same lines. |
| 3 | B1: progress from `stream(..., stream_mode="updates")` | The node wrapper opens a `node.<name>` span when a node starts. `observability/progress.py` sends a `ProgressEvent(node, attempt)` to `send(text, on_progress=...)`. | An update arrives when a node ends, so the status would show the step before. The spans also show the nodes inside `data_loop` and the report flow. |
| 4 | B2.2: "Other: Working" | The first text is "Working". A node that is not in the table keeps the current text. | `scan_output` and `finish` are short. A change to "Working" for some milliseconds only blinks. |
| 5 | B2.3: the `limit_exceeded` line has the trace ID | As specified. The CLI adds " Trace: 4f2a9c1e." to the limit message of spec 36, which has no trace. | The table of B2.3. |
| 6 | B2.3: the error line | A turn that ends with an error type of a failed SQL step (`bq_syntax`, `guard_parse_error`, after `give_up`) is also a red "Error:" line with the trace. A `tool_error` without a trace (a report that is not found, spec 32 B7 #16) is a normal line. | The `give_up` text has the error type of the last attempt. |
| 7 | A2.11: plain mode | Also: in plain mode, the CLI shows the input line after the prompt (`maya> How many orders in 2024?`). A message line is not cut at 80 columns: the terminal wraps it, so a command in it stays one line to copy. | A scripted session then reads like a terminal session (`docs/images/cli-session.txt`). |
| 8 | B3: `cli/commands.py` dispatches the commands | The commands are in the core module `commands.py` (`run_command`, `CommandOutcome` with `title` and `data`). `cli/commands.py` only parses the line (shlex). | The local UI (spec 52) uses the same commands. |
| 9 | A2.4: `/reports` is a Rich table | The session gives a Markdown table, and Rich shows it as a table. | The text is the same for the chat answer "Show my reports" and for the UI. |
| 10 | B2.6: messages | "User 'x' is not in config/users.yaml." (exit 2), "GOOGLE_API_KEY is not set. See the setup guide." (exit 2), a configuration error while the services start (exit 2), BigQuery down or another service error at the start (exit 3). | B2.6. Before, BigQuery down gave exit code 1. |
| 11 | B2.6: `--debug` | It also shows each log record with its stack trace on stderr, so an error that the session catches shows its traceback too. | The session catches every error of a turn (spec 36, B7 #9). |
| 12 | — | The start hides the google-auth warning about user credentials without a quota project. | The public dataset needs no quota project. The warning confused the chat output. |
| 13 | B4: the manual demo inside Docker | Not run here. Spec 53 runs it. | The budget of this work was 10 Gemini calls. |

#### Files

- `cli/main.py` (Typer: `chat`, `conversations`, `stats`, `purge`, and `ui` of spec 52), `cli/chat_loop.py` (the loop of B2.1, Ctrl-C and Ctrl-D, the replay, the resume hint), `cli/commands.py` (parsing), `cli/render.py` (`Ui`: Rich output, plain mode, the status line with the seconds), `cli/status.py` (B2.2).
- Core: `display.py` (B2.3), `startup.py` (B2.6), `commands.py` (A2.4), `capabilities.py` (B2.5). Each part registers its capability when its module loads. All 7 parts are registered, so the development-build line does not appear.

#### Interfaces for the local UI (spec 52) and the evaluation runner

| Member | Use |
|---|---|
| `startup.start_session(config, user_id, resume=None, services=None)` | The session and the start errors (`StartError.message`, `.exit_code`). It also runs the retention of spec 35. The local UI builds the services once with `startup.start_services()` and gives them with `services` (2026-11-12). |
| `session.send(text, on_answer=None, on_progress=None)` | `on_progress` gets `ProgressEvent(node, attempt)`. `cli.status.status_text(event)` gives the text of B2.2. |
| `session.command(name, args)` | `CommandOutcome(kind, text, path, title, data)`. `data` is `Stats` for `/stats` and `TraceTree` for `/trace`. `commands.COMMANDS` lists the commands, `commands.KEEP_PENDING` the commands that keep a waiting delete. |
| `session.greeting()`, `session.missing_capabilities()`, `session.last_trace()` | The greeting panel, the development-build line (`capabilities.development_line()`), and the trace of `/trace` without an ID. |
| `display.describe(result)`, `display.footer_line(result)` | The presentation rules of B2.3. |

The evaluation adapter needs no change: it calls `send(text)` and `close()`.

#### Tests

- `tests/test_cli.py` covers the cases 1–17 of B4: a fake session for the loop, the real session with a fake model and a fake BigQuery for the commands (0 model calls, 0 queries), Typer's `CliRunner` for the commands of the program and piped input, and Rich consoles of 80 columns. The snapshots are in `tests/snapshots/` (the greeting, an answer with a table, the delete panel).
- 948 unit tests pass in total.

#### Live check (2026-11-12)

Two sessions as `maya`, with piped input. The transcript is in [`docs/images/cli-session.txt`](../../images/cli-session.txt).

| Session | Input | Gemini calls | Queries | Bytes billed |
|---|---|---|---|---|
| Test run | `/help`, "What was the revenue of my brands last month?", `/trace`, "What is the weather in Paris today?", `/stats`, `/reports`, `/exit` | 4 | 1 | 21.0 MB |
| Transcript, part 1 | "What was the revenue of my brands in Q3 2026, by month?", `/trace`, "What is the weather in Paris today?", `/stats`, `/reports`, `/exit` | 4 | 1 | 21.0 MB |
| Transcript, part 2 | `--resume C-M5DJP`, `/trace`, `/stats`, `/reports`, `/exit` | 0 | 0 | 0 |

- In total: 8 Gemini calls and 2 queries, 41.9 MB billed. `usage_daily` for 2026-11-12 (Pacific date): 26 calls, 10 queries, 199,229,440 bytes before; 34 calls, 12 queries, 241,172,480 bytes after.
- The data question: a table of three months ($9,639.81, $8,552.36, $11,388.86; total $29,581.03), the footer "14.2 s · 3 model calls · 1 query · 21.0 MB · trace 8d936a2b", no "Assumed" line.
- `/trace`: 17 steps, with the model, the tokens, the guard decision, the masked SQL of the model, the BigQuery job and its bytes.
- The off-topic question: the prepared reply as one plain line, without a footer. 1 call.
- `/stats`: 4 turns of the day (2 questions, 2 refusals), the times, the tokens, the routes, the nodes, 41.9 MB billed.
- After the first run, the `tools_used` of its 2 turns still had the nodes of every turn (deviation #8 of spec 35 came after it). A script removed these 3 node names from the 4 rows of the day, so the figures of the transcript use one rule.
