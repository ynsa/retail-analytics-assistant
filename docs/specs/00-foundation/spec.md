# Spec 00: Foundation

- **Status:** Done (2026-11-11)
- **Phase and stream:** Phase 1 (together, before all other parts)
- **Depends on:** none
- **Requirements covered:** the base for Req 2, 3, 5 and 7. The cost limits protect the development budget.

---

## Part A — Product (review this part)

### A1. Goal

All later parts use the same building blocks: one configuration, one list of users, one tool interface, one trace format, and one set of cost limits. The cost limits work from the first run. So no test or bug can spend money without a limit.

### A2. Behavior the user sees

1. **Users from a file.** The file `config/users.yaml` lists the users, their role (`manager` or `ceo`) and the brands of each manager. The assistant starts only with a user from this file.
2. **A cost limit stops the work.** When an answer reaches a limit (model calls, time, queries, tokens), the assistant stops this answer and tells the user which limit it reached. The chat continues.
3. **A daily limit stops all calls.** When the Gemini calls or the BigQuery bytes of the day reach the daily maximum, the assistant refuses new questions until the next day. It says why and how to change the limit.
4. **The limits are in one file.** A developer changes the limits in `config/settings.yaml`. No code change is necessary.
5. **Every answer leaves a trace.** Each turn writes its steps to a trace file, with the time, the tokens and the errors of each step.

### A3. Scope

- **In scope:** the package layout, the configuration, the user list, the cost limits and the daily counter, the tool interface, the agent state, the database schema, the trace format, the error types, and the Gemini and BigQuery clients with timeouts and limits.
- **Out of scope:** the agent logic (spec 31), the full SQL guard (spec 30), reports (spec 32), the CLI commands (spec 37), the statistics view (spec 35).

### A4. Limits the user sees

Proposed values for the development. All are in `config/settings.yaml`.

| Limit | Value | Message when reached |
|---|---|---|
| Model calls for each answer | 8 | "This question needs more steps than allowed (8 model calls). Please ask a narrower question." |
| Time for each answer | 60 s for a question, 150 s for a report | "The answer took longer than 60 s. Please ask a narrower question." |
| BigQuery queries for each answer | 6 | "This question needs more queries than allowed (6)." |
| Tokens for each answer | 60,000 in, 8,000 out | "This question needs more text than allowed." |
| Bytes billed for each query | 1 GB | "This query reads too much data (over 1 GB). Please narrow the period or the brands." |
| Gemini calls for each day | 480 (was 300, raised on 2026-11-11 by the owner's decision: the free tier of `gemini-3.5-flash-lite` allows about 500) | "The daily limit of model calls is reached. The limit resets at midnight Pacific time." |
| BigQuery bytes billed for each day | 20 GB | "The daily limit of BigQuery data is reached. The limit resets at midnight Pacific time." |

The day of the daily limits starts at midnight Pacific time (changed on 2026-11-12, see B7). Before, it started at midnight UTC.

20 GB each day for 30 days is 600 GB, inside the free 1 TB each month.

### A5. Done when

- [ ] `uv run opsfleet-agent --help` works.
- [ ] A start with a user that is not in `users.yaml` stops with a clear message.
- [ ] A manager without brands in `users.yaml` stops the start with a clear message.
- [ ] Each limit in A4 stops the work with its message. A unit test proves each one.
- [ ] The daily counter keeps its values after a restart.
- [ ] A call to Gemini or BigQuery that waits longer than its timeout stops with an error type, not a hang.
- [ ] Each Gemini call and each BigQuery query writes one span to the trace file.
- [ ] `uv run pytest` passes without network access (the unit tests use fakes).

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | Development limits | The values in A4 | Lower or higher values | They allow normal development and keep the monthly BigQuery use far below the free 1 TB. Gemini free-tier limits are per minute and per day, so 480 calls each day stay under the free limit of about 500. |
| ⚑ | Where the limits work | In code, before each call (a budget object) | Only in the prompt | A prompt cannot guarantee a limit. The code checks the budget before each Gemini call and each BigQuery query. |
| | Trace format | OpenTelemetry SDK with a small JSONL file exporter | Own tracer, or LangSmith | OpenTelemetry is the standard in the architecture. The same spans go to Cloud Trace in production. Spec 35 decides about LangSmith. |
| | Configuration | `config/settings.yaml` and `config/users.yaml`, checked by pydantic models. Secrets only in `.env`. | Environment variables only | Non-secret settings stay readable and in git. pydantic gives clear errors for a wrong file. |
| | Runtime data | One folder `data/` (git ignores it): `app.db`, `checkpoints.db`, `traces/`, `reports/`, `schema_cache.json` | Several folders | One Docker volume keeps all runtime data. |
| | Gemini client | `google-genai` with a timeout of 30 s and at most 2 retries on HTTP 429 and 5xx | SDK defaults | Without a timeout and a retry limit, a test call hung on 2026-11-11. |
| | BigQuery client | A wrapper around `BigQueryRunner`. It uses `runner.client` with a `QueryJobConfig`. `bq_client.py` stays unchanged. | Change `bq_client.py` | The client's file stays as delivered. |

### B2. Design

```mermaid
flowchart LR
    cli["CLI (spec 01, 37)"] --> graph["Agent graph (spec 31)"]
    graph --> tools["Tool registry"]
    tools --> bqw["bq.py<br/>BigQuery wrapper"]
    graph --> llm["llm.py<br/>Gemini client"]
    llm --> budget["limits.py<br/>TurnBudget + DailyLedger"]
    bqw --> budget
    llm --> tracer["tracing.py<br/>spans to JSONL"]
    bqw --> tracer
    budget --> db[("data/app.db")]
    cfg["config.py<br/>settings.yaml + users.yaml + .env"] --> graph
```

**Budget check:** before each Gemini call and each BigQuery query, the code calls `budget.charge(...)`. The budget checks the turn limits and the daily ledger. If a limit is reached, it raises `LimitExceeded` with the limit name. The graph catches it and answers with the message from A4.

**Error types:** each error in the system has one type from this list. The statistics (spec 35) count failures by type.

| Type | Meaning |
|---|---|
| `llm_timeout`, `llm_rate_limited`, `llm_error` | Gemini did not answer, refused for rate, or failed |
| `bq_syntax`, `bq_not_found`, `bq_permission`, `bq_timeout`, `bq_bytes_cap`, `bq_error` | BigQuery errors |
| `guard_policy_block`, `guard_parse_error` | The SQL guard blocked a query, or could not parse it |
| `empty_result` | A query returned 0 rows |
| `limit_exceeded` | A cost limit in A4 was reached |
| `confirmation_rejected` | The user did not confirm a destructive action |
| `tool_error`, `internal_error` | Any other error |

### B3. Files and interfaces

```text
config/
  settings.yaml        # models, limits, paths (in git)
  users.yaml           # demo users, roles, brands (in git)
src/opsfleet_agent/
  __init__.py          # main() entry point
  bq_client.py         # client file, unchanged
  config.py            # Settings, UsersConfig, load_config()
  identity.py          # Role, UserContext, resolve_user(user_id)
  errors.py            # ErrorType enum, AgentError, LimitExceeded
  limits.py            # TurnBudget, DailyLedger
  tracing.py           # init_tracing(), span(), JsonlSpanExporter
  llm.py               # GeminiClient.generate(...): timeout, retries, budget, span
  bq.py                # BigQueryService.query(sql, params, labels) -> QueryResult
  store/db.py          # SQLite connection and schema migrations
  agent/state.py       # AgentState
  agent/tools.py       # Tool protocol, ToolContext, ToolResult, ToolRegistry
data/                  # runtime data, git ignores it
```

**`users.yaml`:**

```yaml
users:
  - id: ceo
    name: Daniel (CEO)
    role: ceo
  - id: maya
    name: Maya (brand manager)
    role: manager
    brands: ["Calvin Klein", "Carhartt"]
  - id: alex
    name: Alex (brand manager)
    role: manager
    brands: ["Levi's"]
```

**`UserContext`:** `id`, `name`, `role`, `brands` (an empty list for `ceo` means all brands). The CLI creates it from `--user`. The model never sets it.

**`AgentState`** (LangGraph state):

| Field | Content |
|---|---|
| `messages` | The conversation turns (role, text, tool calls, tool results) |
| `user` | `UserContext` |
| `conversation_id` | One ID for each CLI run, or for each resumed conversation |
| `turn` | `trace_id`, attempt counter, last SQL, last error type, references to results |
| `pending_confirmation` | The data of a paused destructive action (spec 33) |
| `summary` | The summary of older turns (spec 34) |

**Tool interface:**

```python
class Tool(Protocol):
    name: str
    description: str
    args_model: type[BaseModel]  # becomes the Gemini function declaration
    side_effect: Literal["read", "write", "destructive", "external_send"]

    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult: ...
```

`ToolContext` gives the tool the `UserContext`, the budget, the tracer, the BigQuery service and the store. A tool with `side_effect` `destructive` or `external_send` runs only after the confirmation gate (spec 33).

**`QueryResult`:** `dataframe`, `row_count`, `truncated` (more rows than the row cap), `bytes_processed`, `bytes_billed`, `job_id`, `duration_ms`.

**Database `data/app.db` (SQLite):**

| Table | Columns |
|---|---|
| `usage_daily` | `day` (the date in `limits.per_day.reset_timezone`, see B7), `llm_calls`, `tokens_in`, `tokens_out`, `bq_queries`, `bq_bytes_billed` |
| `conversations` | `id`, `user_id`, `started_at`, `title` |
| `reports` | defined in spec 32 |
| `audit_events` | `id`, `at`, `user_id`, `conversation_id`, `action`, `details` (JSON) |

LangGraph keeps its checkpoints in `data/checkpoints.db` with `SqliteSaver`.

**Trace record** (one JSON line for each span, in `data/traces/<UTC date>.jsonl`):

```json
{"trace_id": "…", "span_id": "…", "parent_span_id": "…", "name": "llm.generate",
 "kind": "llm", "start": "2026-11-12T10:00:00Z", "duration_ms": 812, "status": "ok",
 "attributes": {"user_id": "maya", "conversation_id": "…", "model": "gemini-3.5-flash-lite",
                "tokens_in": 1830, "tokens_out": 210, "error_type": null}}
```

Span kinds: `turn`, `node`, `llm`, `tool`, `sql`, `guard`, `limit`, `confirm`. A trace never contains result rows or PII. Spec 35 defines the masking of SQL text.

### B4. Tests

- Unit tests with fakes for Gemini and BigQuery: each limit, the daily ledger after a restart, the timeout handling, the config errors, the error types.
- No network access in the unit tests. Live tests have the pytest marker `live` and do not run in the pre-commit checks.

### B5. Cost

Unit tests: 0 calls. One manual check: 1 Gemini call and 1 small BigQuery query (10 MB billed).

### B6. Risks and open points

- The Gemini free-tier limits for each model can change. The daily limit of 480 calls is our own limit, not Google's limit. A `llm_rate_limited` error shows when Google's limit is reached first.
- Later specs extend these interfaces and keep the formats above:
  - Spec 30 replaces the minimal guard with `SqlGuard(...).check(sql) -> GuardResult`.
  - Specs 31, 33 and 34 add fields to `AgentState` (`turn` details, `pinned`, `turn_no`, a fixed `pending_confirmation` structure).
  - Spec 33 adds `grant` to `ToolContext`. `ToolRegistry.run()` refuses a `destructive` or `external_send` tool without a valid grant.
  - Spec 36 adds `schema=` and `allow_fallback=` to `GeminiClient.generate()`, and caps each Gemini attempt by the time that is left in the turn. So the 30 s timeout and the retries never exceed the answer limit.
  - Spec 35 adds the span attribute `error_type` and the span events `retry` and `circuit_open`.
  - Specs 32–35 add database tables and settings sections. Each spec lists its own.

### B7. Implementation notes

#### Reset time zone of the daily counter (2026-11-12)

- Google resets the Gemini free-tier daily quota at midnight Pacific time, not at midnight UTC. Before this change, our counter started a new day 8 hours before Google (7 hours in summer). So the counter could show calls that Google already did not count, or the reverse.
- The day of `usage_daily` is now the date in the time zone of the setting `limits.per_day.reset_timezone`. The default is `America/Los_Angeles`. A wrong time zone name stops the start with a clear message.
- The limit messages say "The limit resets at midnight Pacific time."
- Migration: none. The rows that the earlier version wrote with the UTC date stay. On the day of the change, the Pacific date and the UTC date were the same for most of the day, so the counter of that day continued.
- `limits.next_reset(now, timezone)` gives the next midnight of the reset time zone. The circuit breaker of spec 36 uses it for a daily-quota error.
- New dependency: `tzdata`. A small Docker image can have no system time zone data.
- Tests: `tests/test_limits.py` (the day changes at 08:00 UTC in November, the setting, the old UTC rows, the next reset, a wrong time zone).
