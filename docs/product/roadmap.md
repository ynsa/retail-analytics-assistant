# Roadmap: Retail Analytics Chat Assistant

- **Version:** 1.0
- **Status:** Proposed
- **Inputs:** `docs/product/product-definition.md`, `docs/product/architecture.md`, `docs/analysis/gap-analysis.md`, and the user's notes of 2026-11-11.

This roadmap divides the work into parts. Each part gets its own spec in `docs/specs/<NN>-<name>/spec.md` before the work starts. A spec states the goal, the scope, the limits, the tests, and the "done when" check.

---

## 1. Phases

| Phase | Mode | Parts | Why this mode |
|---|---|---|---|
| **1. Foundation and walking skeleton** | Together, one stream | 00, 01 | All later parts share the interfaces, the cost limits and the trace format. A wrong interface here causes rework in every stream. |
| **2. Parallel work** | Three streams | Design (10, 11), Evaluation (20–23), Prototype (30–37), UI spec (40) | These parts touch different files and depend only on Phase 1. |
| **3. Integration and checks** | Together, one stream | 50–55 | The parts join, the evaluation runs on the real agent, and the final checks run in Docker. |

Phase 1 produces a **walking skeleton**: a thin path that already works from end to end. The path is: the CLI, the agent graph, a minimal SQL guard, BigQuery, and an answer, with the trace and the cost limits. Each later part makes one piece of this path complete. The team can test the agent from the first day.

---

## 2. Parts

### Phase 1 — Foundation (together)

| # | Part | Content |
|---|---|---|
| 00 | **Foundation** | Package layout. Config: `users.yaml`, settings and model names. LangGraph state. Tool interface. SQLite schema (reports, audit). Trace and log format. **Development cost limits** (section 4). |
| 01 | **Walking skeleton** | `--user` CLI → agent graph → minimal guard (read-only, table allowlist) → BigQuery wrapper (dry run, byte cap, timeout) → answer. Every turn writes a trace. |

### Phase 2, stream A — Design

| # | Part | Content |
|---|---|---|
| 10 | **Design document (HLD) and technical explanation** | Production design for all requirements (Req 1–8), data flow, error handling and fallbacks, sizing for about 1,000 questions each day. |
| 11 | **Draw.io diagrams** | (a) Production on Google Cloud, with the official Google Cloud icons. (b) The prototype architecture. Both as `.drawio` files, with exported images for the README. |

### Phase 2, stream B — Evaluation

| # | Part | Content |
|---|---|---|
| 20 | **Research: evaluation and agent best practices** | Compare evaluation frameworks (for example DeepEval, Ragas, promptfoo, LangSmith evaluations). Collect the best practices for chatbots and agents: context management, safety, and evaluation. Recommend one approach. |
| 21 | **Evaluation dataset: questions and reference SQL** | Synthetic questions for each capability, from simple to multi-step. For each question: the expected query type, a reference SQL, and the expected result from BigQuery. Questions for both roles. |
| 22 | **Safety and behavior test set** | Prompt injection and provocative messages. Requests for other topics. Requests for PII, the system prompt, keys or other brands. A delete without confirmation. Long conversations. |
| 23 | **Evaluation runner and scoring** | Run the sets on the agent and score them. SQL: compare the result with the reference result, not the SQL text. Reports and answers: an LLM as judge with a written rubric, checked by a person on a sample. Each run shows the number of model calls before it starts. |

### Phase 2, stream C — Prototype

| # | Part | Content |
|---|---|---|
| 30 | **Query safety** | The full SQL guard: brand scope, PII columns, demographic fields only in aggregates. The result masker and the output scanner. The empty-result checks. |
| 31 | **Agent core** | Request routing (analysis, report, delete, other topic). SQL writing and the self-correction loop. Multi-step questions. Topic limits: the agent answers only analysis questions. |
| 32 | **Reports** | How the agent builds a report: the sections, and how many queries and charts are allowed. Size limits. The library. HTML and Markdown files with charts. |
| 33 | **Delete with confirmation** | `interrupt()`, the typed confirmation, soft delete, undo, purge, audit. |
| 34 | **Long conversations and memory** | Keep the important facts of a long conversation inside the context limit: the summary of older turns, the facts that stay (brands, filters, definitions), and short tool outputs. |
| 35 | **Observability and statistics** | The conversation log and the traces. Statistics: time for each question (median, p95), answer length, tokens, tools used, the number and types of failures. A local view (`/stats`, `/trace`). Decide about LangSmith. |
| 36 | **Resilience** | Retries with limits, the fallback model, the circuit breaker, and the messages to the user when a service is not available. |
| 37 | **CLI** | The final CLI: commands (`/reports`, `/export`, `/trace`, `/stats`, `/help`), tables, and Markdown output. |

### Phase 2, stream A (continued) — UI spec

| # | Part | Content |
|---|---|---|
| 40 | **Local UI: spec with Excalidraw mockups** | Hand-drawn Excalidraw sketches of each screen: user and role selection, chat, conversation history, delete confirmation, report files. The user approves the sketches before any UI code. |

### Phase 3 — Integration and checks (together)

| # | Part | Content |
|---|---|---|
| 50 | **Evaluation on the real agent** | Run sets 21 and 22 on the agent. Correct the failures. Repeat. |
| 51 | **Model comparison** | Run the test set on `gemini-3.5-flash-lite` and `gemini-3.8-flash`. Compare the correct answers, the latency and the tokens. Choose the default model. |
| 52 | **Local UI** | Build the UI from the approved sketches (part 40). Nice to have. |
| 53 | **Docker and final checks** | The image for `linux/amd64` and `linux/arm64`. The tests and a manual demo inside the image. |
| 54 | **README and setup guide** | Setup for Docker and for uv, ADC on each operating system, and an example run. |
| 55 | **Experience document** | A separate document about the framework choice and the level of experience. |

---

## 3. Where each note goes

| The user's note | Parts |
|---|---|
| Solid testing and evaluation: synthetic questions, expected query types, a dataset that checks the SQL for each question | 20, 21, 23, 50 |
| An evaluation framework, an LLM as judge | 20, 23 |
| UI mockups in Excalidraw in the spec | 40, then 52 |
| Draw.io diagrams: production on Google Cloud, and the prototype | 11 |
| Security: provocative messages do not change the behavior and do not reveal data, keys or the system prompt | 22, 30, 31 |
| The agent discusses only its main topic | 22, 31 |
| A conversation log, maybe LangSmith | 35 |
| Statistics: time (median), answer length, tokens, tools, failures and their types | 35, 23 |
| Limits on queries and time for each answer, and no surprise costs during the implementation | 00 (from the first day), 36 |
| How reports are built, their limits, and what is allowed | 32 |
| Long conversations within the context limit | 34, 22 (a long-conversation test) |
| Best practices for chatbots and agents | 20, then applied in 31, 34 and 36 |
| A spec for each part | Every part in section 2 |

---

## 4. Development cost limits (part 00, active from the first day)

These limits protect the budget while we build. The code enforces them. They are not only rules in a prompt. _(proposed values)_

| Limit | Value | When the agent reaches it |
|---|---|---|
| Model calls for each answer | 8 | Stops and tells the user |
| Time for each answer | 60 s for a question, 150 s for a report | Stops and tells the user |
| BigQuery queries for each answer | 6, including the empty-result checks | Stops and tells the user |
| Bytes billed for each query | 1 GB (`maximum_bytes_billed`) | BigQuery rejects the query |
| Tokens for each answer | a set input and output budget | Stops and tells the user |
| Gemini calls and BigQuery bytes for each day | a local counter with a daily maximum | All calls stop until the next day or until the user raises the limit |
| An evaluation run | shows the planned number of calls first, and waits for a "yes" above 20 calls | Does not start |

The development uses the free-tier key and `gemini-3.5-flash-lite`. A call to Vertex AI or to a paid tier needs the user's approval first.

---

## 5. Parallel work: how

- Phase 1 runs first and alone. It ends with a first git commit (after the user's approval), because the parallel streams need separate git worktrees.
- In Phase 2, at most three streams run at the same time: A (design and UI spec), B (evaluation) and C (prototype). Each stream works in its own worktree.
- Stream C runs its parts in this order: 30, 31, 32, 33, 34, 36, 35, 37. Safety comes first, because every other part depends on it. Reports (32) come before delete (33), because the delete uses the report tables.
- Stream B does not need the finished agent for parts 20–22. Part 21 uses BigQuery directly to get the expected results.
- The streams share the Gemini free-tier rate limit. Evaluation runs and live tests do not run at the same time.
