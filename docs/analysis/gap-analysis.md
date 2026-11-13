# Gap Analysis: Architecture and Product Documentation

Status: draft for internal review. Created: 2026-11-08. Updated: 2026-11-11 with the answers from OpsFleet.

Sources:

- `docs/brief/Original task.md` (the brief).
- `src/opsfleet_agent/bq_client.py` (the BigQuery client that OpsFleet shared).
- The schema of `bigquery-public-data.thelook_ecommerce`. We read the table metadata live on 2026-11-11. It matches the column lists in section 1.5.
- The answers from OpsFleet in Slack: the project manager answered, and the technical contact confirmed the answers. Received by 2026-11-11.

Evidence labels in this document:

- **OpsFleet**: an answer from OpsFleet (section 5).
- **Brief**: text from the assignment.
- **Code**: a fact from the shared file.
- **Data**: a fact from the dataset schema.
- **Practice**: usual industry practice.

---

## 1. What the assignment asks for

### 1.1 Context

- Client: OpsFleet. Project managers: the Team Lead and Harry.
- End users: non-technical executive teams of a retail company.
- Product: a chat agent. It answers sales and performance questions in natural language, continues the discussion, and writes reports with action items.
- Assessment focus: system design, the technical explanation, and an elegant prototype.
- Time budget: 6 to 12 hours.
- Submission: a public GitHub repository with documentation, source code, and an architecture diagram.

### 1.2 Deliverables

| # | Deliverable | Notes from the brief |
|---|---|---|
| 1 | Architecture diagram (Mermaid or similar) | Show building blocks, services, compute, and flow. Name each service and data store and explain the choice. |
| 2 | Detailed technical explanation | Reasons for cloud services, models, and frameworks. Data flow. Error handling and fallbacks. Setup and an example run. One explanation for each requirement. |
| 3 | Working prototype | A chat agent that answers data questions, continues the discussion, and creates a report with action items. |
| 4 | CLI interface | Chat in the terminal. |
| 5 | Runs on another machine | Setup instructions are mandatory. Docker is optional. The brief uses `pip install -r requirements.txt`. |
| 6 | Framework choice | Explain why we chose it and our level of experience with it. |

### 1.3 Requirements and scope

| Req | Topic | In the HLD | In the prototype |
|---|---|---|---|
| 1 | Hybrid intelligence (Golden Bucket) | Yes | No |
| 2 | Safety, PII masking, per-user product scope | Yes | **Yes** |
| 3 | Destructive operations on Saved Reports, with confirmation | Yes | **Yes** |
| 4 | Learning loop (user level and system level) | Yes | No |
| 5 | Resilience and graceful error handling | Yes | **Yes** |
| 6 | Quality assurance | Yes | No |
| 7 | Observability | Yes | **Yes** |
| 8 | Persona management without redeployment | Yes | No |
| – | Extensibility (charts, email, web search) | Yes | No |
| – | Expected capabilities: customer behavior, product performance, time-based metrics, schema questions, multi-step queries, reports with action items | – | **Yes** |

OpsFleet confirmed this scope on 2026-11-11:

- The prototype has a CLI. It needs no UI.
- The agent queries the BigQuery public dataset with our own credentials.
- The prototype implements Req 2, 3, 5 and 7.
- Req 1, 4, 6 and 8 are in the design only. The HLD and the technical explanation describe how the production system does them.

Fixed constraints:

- Data: BigQuery dataset `bigquery-public-data.thelook_ecommerce`, tables `orders`, `order_items`, `products`, `users`.
- LLM: a newer Google Gemini model is preferred. The brief points to a free Google AI Studio key and warns about rate limits. OpsFleet will test with Gemini.

### 1.4 What the shared file gives

`BigQueryRunner` has two methods:

- `execute_query(sql)` runs any SQL text and returns all rows as a pandas DataFrame.
- `get_table_schema(table)` returns the name, type, mode, and description of each column.

The file does not give the controls below. The architecture must add them.

1. **No read-only guard.** The method runs any SQL text, also DDL and DML. The brief says that the database is read-only. But the caller's credentials can usually write to the caller's own billing project.
2. **No cost guard.** There is no dry run, no `maximum_bytes_billed`, and no timeout.
3. **No row limit.** `to_dataframe()` loads the full result into memory. The agent can then send too many rows to the LLM.
4. **No PII control.** The method returns raw columns, for example `email` and `street_address`.
5. **No per-user scope.** Every query can read all products.
6. **No error classes.** The method catches every `Exception` and raises it again. The self-correction loop must know a syntax error from a quota error or a temporary error.
7. **No trace link.** The method does not return the BigQuery job ID and does not add job labels. We cannot connect a BigQuery job to a chat turn.
8. **`dataset_id` is not used by `execute_query`.** Generated SQL must use full table names, or the client must set a default dataset in `QueryJobConfig`.
9. **No empty-result signal.** The caller must check `df.empty`.

### 1.5 Dataset facts that cause gaps

| Table | Columns |
|---|---|
| `products` | `id`, `cost`, `category`, `name`, `brand`, `retail_price`, `department`, `sku`, `distribution_center_id` |
| `users` | `id`, `first_name`, `last_name`, `email`, `age`, `gender`, `state`, `street_address`, `postal_code`, `city`, `country`, `latitude`, `longitude`, `traffic_source`, `created_at`, `user_geom` |
| `orders` | `order_id`, `user_id`, `status`, `gender`, `created_at`, `returned_at`, `shipped_at`, `delivered_at`, `num_of_item` |
| `order_items` | `id`, `order_id`, `user_id`, `product_id`, `inventory_item_id`, `status`, `created_at`, `shipped_at`, `delivered_at`, `returned_at`, `sale_price` |

Facts that matter:

- `products` has no owner, manager, or client column.
- `users` has many direct identifiers: names, email, street address, postal code, and exact location.
- The `events` table has `ip_address`, which is also PII. The `events` table is outside the prototype scope, but the design document must list IP address as PII.
- The dataset has no subscription or contract data. "Churn" has no direct field.
- The data is synthetic. Its date range can change when Google regenerates it. The agent must read the latest date from the data and must not assume it.

---

## 2. How we classify the gaps

- **Blocking**: the answer changes the core data model, the security model, or a feature that the prototype must have. A wrong guess causes a large rework.
- **Non-blocking**: we write a documented assumption and continue. A later change is cheap, or it stays inside one component.

How we closed the gaps:

1. We asked OpsFleet one round of questions. The answers closed B1, B2, B3, and N3, and part of N2 and N20 (section 5.1).
2. We do not ask more questions. We decide all other open points ourselves and record each one as **our decision** (section 5.2).
3. Each decision of our own states its reason, so the HLD can refer to it.

### 2.1 Decision log (blocking gaps)

| ID | Gap status | Decision | Selected on | Changed on |
|---|---|---|---|---|
| B1 | Closed 2026-11-11 | `--user` at CLI start. Roles: `manager` (one or more brands) and `ceo` (all brands). No `/switch-user` command. | 2026-11-08 | 2026-11-11 |
| B2 | Closed 2026-11-11, decision changed | Scope by `products.brand` (one or more brands for each manager), enforced by a SQL guard. Was: scope by category. | 2026-11-08 | 2026-11-11 |
| B3 | Closed 2026-11-11, decision changed | Confirmed PII list, plus IP address. Show the raw `user_id`. Demographic fields only in aggregates. Was: a masked customer token. | 2026-11-08 | 2026-11-11 |
| B4 | Closed 2026-11-11, our decision | SQLite store, owner-only, typed confirmation, soft delete with 30-day undo. "Client X" is a brand or another business entity that the report names. | 2026-11-08 | 2026-11-11 |

---

## 3. Blocking gaps

### B1. User identity and roles

**Gap.** Req 2 needs a per-user data scope. Req 3 needs "own" reports. Req 4.1 needs per-user preferences. The brief does not say how the system knows who the user is. A CLI has no sign-in step. The brief also does not define roles, for example CEO, regional executive, or persona editor.

**Impact.** Req 2, 3, 4.1, 8. Prototype and HLD.

**Status.** Closed on 2026-11-11.

**Answer from OpsFleet.** Each manager is responsible for one or more brands and can analyze only those brands. The CEO sees everything. The CLI argument is fine for selecting the user.

**Decision.**

- Prototype: the CLI takes `--user <id>`. A local file (for example `config/users.yaml`) holds the role and the brands of each user. This file simulates an identity provider.
- To change the user, restart the CLI with a different `--user` value. The prototype has no command that changes the user inside a session.
- Production: single sign-on through the company identity provider (OIDC), for example Google Workspace with Identity-Aware Proxy. The agent API reads the user ID and groups from the verified token.
- The agent never takes identity or role from the chat text.
- Roles in the prototype: `manager` (one or more brands) and `ceo` (all brands).
- The design also has a `persona_editor` role for Req 8. The prototype does not implement it.

**Evidence.**

- OpsFleet: "Each manager is responsible for one or more brands and can only analyze those brands, while the CEO sees everything." The technical contact confirmed that the CLI argument is fine for switching users.
- Brief: "Each user should only be able to analyze data on products related to him."
- Brief: "The users are allowed to delete their own reports."
- Brief: "'User A' prefers tables while 'User B' prefers bullet points."
- Brief: "The CEO wants to change the 'tone' of the reports weekly." This needs at least one role with special rights.
- Practice: an identity claim inside a prompt is not trusted (OWASP Top 10 for LLM Applications: prompt injection, excessive agency).

### B2. "Products related to him" (data entitlement model)

**Gap.** The dataset has no link between an executive and a product. The brief does not say which product attribute defines the scope. It also does not say how the scope applies to `users` and `orders`, which are not product tables.

**Impact.** Req 2. This is the core security model of the prototype and the HLD.

**Status.** Closed on 2026-11-11. The decision changed from category to brand. We decided the follow-up point ourselves (see the last bullet of the decision).

**Answer from OpsFleet.** The scope is one or more brands for each manager. The manager-to-brand mapping is not in the dataset. We choose a structure, for example a configuration file or an access table, and document it.

**Decision.**

- A scope is a list of `products.brand` values for each manager. Example: `brand IN ('Calvin Klein', 'Carhartt')`. The `ceo` role has no filter.
- The guard compares exact brand names. The data has 2,756 named brands (24 products have no brand), and some are close variants, for example "Calvin Klein", "Calvin Klein Jeans", and "Calvin Klein Performance". The mapping lists each brand name in full. Some names contain an apostrophe, for example "Levi's", so the guard passes brand names as query parameters, not as text in the SQL.
- Prototype: the mapping is in `config/users.yaml`. Production: the mapping is in an access table. The HLD chooses the store.
- The scope applies to `order_items` rows. A manager sees only the order items of products of their brands. The manager sees orders and customers only through these order items.
- Order-level fields that mix products in and out of scope (for example `orders.num_of_item`) are not available to managers. The agent calculates totals from the order items in scope.
- Our decision: a manager does not see customer statistics that are not linked to their brands, for example new sign-ups. Only the `ceo` role sees them.
- Schema questions ("what data do we have?") are allowed for all users. The answer shows structure, not rows.
- Code enforces the scope, not the prompt:
  - Prototype: a SQL guard parses the generated SQL (for example with `sqlglot`). It replaces each table reference with a subquery that applies the user's scope filter.
  - Production: BigQuery authorized views for each scope group, plus the same SQL guard as a second layer.

**Evidence.**

- OpsFleet: "Each manager is responsible for one or more brands and can only analyze those brands ... The manager-to-brand mapping is not in the dataset, so assume a structure that makes sense to you (e.g. a config or access table) and document it."
- Data: `products` has `brand`, `category`, `department`, and `distribution_center_id`. It has no owner column.
- Brief: "Each user should only be able to analyze data on products related to him."
- Practice: enforce access in the data layer, not in the LLM prompt (least privilege, defense in depth).

### B3. PII definition and allowed output

**Gap.** The brief says that the data contains PII and that the agent must not show it. The brief does not list the PII fields. The brief also asks for "top customers". This conflicts with the rule if the answer shows names or emails. The brief does not say if the LLM provider may receive PII.

**Impact.** Req 2. Prototype and HLD.

**Status.** Closed on 2026-11-11. The decision changed from a masked token to the raw `user_id`. We decided the small-group point ourselves.

**Answer from OpsFleet.**

- Never shown in the output: first and last name, email, street address, postal code, and exact coordinates.
- IP address is also PII. It appears only in the `events` table, which is outside the prototype scope. The design document must list it.
- City, state, country, age, gender, and traffic source can appear only in aggregates. This also applies to the `gender` column of `orders`.
- The answers can refer to customers by `user_id`, or by a stable pseudonymous ID such as a salted hash.
- PII must never reach the LLM. The system masks it before it sends data to the model.

**Decision.**

- Direct identifiers. The agent never shows them and never sends them to the LLM: `first_name`, `last_name`, `email`, `street_address`, `postal_code`, `latitude`, `longitude`, `user_geom`. In the design: also `events.ip_address`.
- Demographic fields that can appear only in aggregates: `city`, `state`, `country`, `age`, `gender`, `traffic_source`. The SQL guard rejects an output row that puts a `user_id` next to one of these fields.
- The answers show the raw `user_id`. OpsFleet accepts it, and follow-up questions ("tell me more about customer 4521") work directly in SQL.
- A "top customers" answer shows the `user_id` and the spend figures only.
- Three layers of control:
  1. The SQL guard blocks PII columns in the query output.
  2. A result masker cleans the rows before they go to the LLM.
  3. An output scanner checks the final text for email, phone, and address patterns.
- Our decision: the prototype does not hide small groups. The HLD shows the hiding of aggregates of fewer than 5 customers as a production option.

**Evidence.**

- OpsFleet: the PII list, the rule for aggregates, `user_id` or a salted hash is fine, and "PII must never reach the LLM".
- Brief: "The raw transaction logs contain sensitive data. The agent is strictly forbidden from displaying these PIIs in the final output."
- Brief: "Customer behavior (e.g., top customers, total spend)."
- Data: `users` has names, email, street address, postal code, and geo columns.
- Practice: GDPR treats names, emails, addresses, and exact location as personal data. Pseudonymization is a standard control.
- Practice: as we understand the current Gemini API terms, Google can use prompts from unpaid (free-tier) use to improve its products. Thus raw PII must not reach a free-tier model. We must check the current text of the terms.

### B4. Saved Reports library and delete rules

**Gap.** Req 3 requires delete commands on a "Saved Reports" library. The brief does not define these items:

- what a report contains,
- when the agent saves a report,
- where the system stores reports,
- who can see a report,
- what "Client X" is,
- if a delete is permanent.

**Impact.** Req 3. Prototype and HLD.

**Status.** Closed on 2026-11-11. This is our decision. We did not ask OpsFleet.

**Decision.**

- A report is a record with these fields: `report_id`, `owner_id`, `conversation_id`, `title`, `created_at`, `question`, `sql` (a list), `body_markdown`, `entities` (brands, categories, products, and states that the report names), `deleted_at`.
- The agent saves a report only when the user asks to create or save a report. The agent does not save plain answers.
- Storage: a SQLite file in the prototype. Cloud SQL (PostgreSQL) in production (decided in `docs/product/architecture.md`).
- A user sees and deletes only their own reports.
- "Client X" is a business entity that the report names, for example a brand. It cannot be an end customer, because reports never contain customer names (B3). The search uses the `entities` field first and a full-text search of the title and body second.
- A delete is a soft delete with a 30-day restore period. A scheduled job then purges the record. The user can undo the delete.
- Code runs the confirmation flow, not the LLM:
  1. The agent finds the exact list of reports (count, titles, dates).
  2. The system shows the list and asks for an explicit confirmation, for example the user types `delete 3`.
  3. The confirmation is bound to that exact list (a hash of the IDs). It expires after one turn.
  4. The system deletes only the IDs in the list and writes an audit event.
  5. If the request matches zero reports, the system says so and deletes nothing.

**Evidence.**

- Brief: "Delete all reports mentioning Client X" / "Delete all the reports we made in this conversation."
- Brief: "requires a strict confirmation flow before execution, without breaking UX - the users are allowed to delete their own reports."
- Data: the four tables have no "client" entity.
- Practice: a human must approve destructive tool calls (OWASP LLM: excessive agency). Soft delete with undo is a common pattern for bulk deletes.

---

## 4. Non-blocking gaps

### N1. Production cloud platform

- **Gap.** The brief asks for cloud services but does not name a cloud.
- **Assumption.** Google Cloud. Cloud Run for the agent API. Vertex AI Gemini (paid tier) for the LLM. BigQuery for data. Cloud Storage for the Golden Bucket. Cloud SQL (PostgreSQL) for app state (decided in `docs/product/architecture.md`). Secret Manager for keys. Cloud Logging, Monitoring, and Trace for observability.
- **Evidence.** Brief: BigQuery, Gemini, and a "bucket". Practice: one cloud gives one IAM model and no data egress cost.

### N2. Production user channel

- **Gap.** The prototype is a CLI. Executives do not usually use a CLI. The brief does not name the production channel.
- **Status.** Closed. OpsFleet confirmed that the prototype needs a CLI and no UI. The production channel is our decision.
- **Assumption.** Production uses a web chat or Google Chat / Slack, behind single sign-on. All channels call one agent API. The CLI is a client for the prototype only.
- **Evidence.** OpsFleet: "A CLI interface is expected (no UI needed)." Brief: "non-technical executive teams" and "CLI-based interface for chat interactions".

### N3. Scale and non-functional targets

- **Gap.** The brief gives no user count, latency, availability, or budget targets.
- **Status.** Closed on 2026-11-11 for load and latency.
- **Answer from OpsFleet.**
  - About 100 active managers each day.
  - About 10 questions for each manager each day. This gives about 1,000 questions each day.
  - About 1 saved report for each manager each day. This gives about 100 reports each day.
  - About 1 million new rows each day.
  - Tens to a few hundred executives in total. Only a few are active at the same time.
  - Latency: 10–30 s for a simple question, 1–2 min for a long report.
- **Assumption (not answered).** 99.5% availability in business hours. Region: US, because the public dataset is in the US multi-region.
- **Evidence.** OpsFleet: the load and latency figures above. Practice: usual availability targets for internal analytics tools.

### N4. Business metric definitions

- **Gap.** The brief uses "churn rate", "underspending", "revenue", "Q1", and "last month" but does not define them.
- **Status.** Closed on 2026-11-11. This is our decision. We did not ask OpsFleet.
- **Decision.** A metrics glossary that the agent and the golden trios both use:
  - Revenue = `SUM(order_items.sale_price)` for items with status not `Cancelled` and not `Returned`.
  - Gross margin = `sale_price - products.cost`.
  - Churned customer = a customer with at least one purchase and no purchase in the last 90 days.
  - Monthly churn rate = customers who reached 90 days without a purchase in that month, divided by active customers at the start of the month.
  - Underspending = average spend per customer below the company average or below the compared group.
  - The date of a sale is `order_items.created_at`. All period metrics use this column.
  - "Today" is the current UTC date. The data contains some dates after today, because Google generates it. Period metrics leave out rows with a date after today.
  - Quarters are calendar quarters. "Last month" is the last full calendar month before today.
  - Spec 21 adds more definitions: purchase, return rate, cancellation rate, average order value, and spend per customer. The agent glossary (spec 31) contains all of them.
  - Each answer states the date range and the definitions that it used.
- **Evidence.** Brief: the example questions. Data: no subscription field. Practice: e-commerce churn uses an inactivity window.

### N5. Golden Bucket format and life cycle

- **Gap.** The Golden Bucket does not exist ("Theoretical"). The brief does not define its format, size, or owner.
- **Assumption.**
  - A Cloud Storage bucket with one JSON file for each trio: `question`, `sql`, `report`, `tags`, `metrics_used`, `author`, `approved_at`.
  - An ingest job embeds each question into a vector index (BigQuery vector search or Vertex AI Vector Search).
  - At query time, the agent gets the top-k similar trios as examples.
  - New trios come from analysts, and from high-rated agent answers after an analyst approves them.
  - Optional in the prototype: 10 to 20 seed trios in the repository with simple local retrieval.
- **Evidence.** Brief Req 1: "Explain how you will update the golden bucket over time and how you will provide relevant data at user query time." Brief: the bucket is "(Theoretical)" and Req 1 is design-only. Practice: retrieval of similar examples improves text-to-SQL accuracy.

### N6. Allowed question scope

- **Gap.** The brief says "only analysis questions" but does not give the boundary.
- **Assumption.** Allowed: analysis of the four tables, schema questions, report create / list / delete, and preference statements. The agent refuses other requests with a short message. Controls: an input classifier, the system prompt, and a tool allowlist. The agent treats database values and golden trios as data, not as instructions.
- **Evidence.** Brief Req 2: "The agent is only allowed to answer analysis questions and needs to be safeguarded against malicious users." Brief: "Answer questions about the general structure of the database."

### N7. Read-only guarantee

- **Gap.** The brief says that the database is read-only. The shared client does not enforce it (section 1.4).
- **Assumption.** We enforce it in three layers:
  1. The SQL guard accepts one statement only, and only `SELECT` or `WITH`.
  2. A table allowlist accepts only the four tables.
  3. A dedicated service account has only BigQuery Data Viewer on the dataset and BigQuery Job User in the billing project.
- **Evidence.** Brief: "A read-only connection to a SQL database." Code: `execute_query` runs any SQL. Practice: least privilege.

### N8. Cost and retry budget

- **Gap.** The brief says "without inflating costs" but gives no limits.
- **Assumption.**
  - A maximum of 3 self-correction attempts for each query step, and 3 query steps for each question (spec 31).
  - A BigQuery dry run before each query, and a `maximum_bytes_billed` cap (for example 1 GB).
  - A maximum of 200 rows to the LLM. The SQL must aggregate the data.
  - A token budget for each turn.
  - Exponential backoff with jitter on HTTP 429 and 5xx errors.
  - A fallback to a smaller Gemini model, and a circuit breaker for each external service.
  - On a final failure, the agent shows a clear message. The CLI stays open.
- **Evidence.** Brief Req 5: "attempt to self-correct before giving up, without crashing the user interface and without inflating costs" and "resilient to API/3rd party services failures/downtime". Practice: bounded retries.

### N9. Observability scope and tools

- **Gap.** The brief asks for metrics and deep-dive analysis. It does not name tools or say if a dashboard is necessary in the prototype.
- **Assumption.**
  - Prototype: structured JSON logs and one trace for each turn, in a local JSONL or SQLite file. Each trace has a `trace_id` and spans for LLM calls, tool calls, SQL text, BigQuery job ID, tokens, latency, retries, and guard decisions. A CLI command (`/trace`) shows the trace.
  - Production: OpenTelemetry to Cloud Trace and Cloud Logging, plus an LLM trace tool (for example Langfuse). Dashboards and alerts in Cloud Monitoring.
  - Metrics: turn success rate, SQL error rate, self-correction success rate, empty-result rate, guard blocks, delete confirmations (asked / confirmed / cancelled), latency p50 / p95, tokens and cost for each turn, BigQuery bytes billed, user feedback.
  - Logs and traces mask PII.
- **Evidence.** Brief Req 7: "We need to know when the agent is failing and why ... understanding what the message correspondence is and what went wrong." Practice: OpenTelemetry semantic conventions for GenAI.

### N10. Data retention

- **Gap.** The brief does not give retention periods for conversations, traces, reports, or audit events.
- **Assumption.** Traces: 30 days. Conversations: 90 days. Reports: until the user deletes them, plus 30 days of soft delete. Audit events: 1 year.
- **Evidence.** Practice: the GDPR storage-limitation principle.

### N11. Persona management

- **Gap.** The brief does not say who edits the persona, where, or how the persona interacts with safety rules and user preferences.
- **Assumption.**
  - A user with the `persona_editor` role edits a versioned prompt configuration (versioned YAML files in Cloud Storage, decided in `docs/product/architecture.md`) through a small admin page.
  - The agent reads the configuration at each turn, with a 60-second cache.
  - Each version records the author and the time. An editor can roll back to an earlier version.
  - Priority: safety rules first, then the company tone, then user format preferences.
  - The persona can change the tone only. It cannot change safety rules or SQL rules.
- **Evidence.** Brief Req 8: "The system must allow non-developers to update the agent's instructions without redeployment." Practice: prompt versioning with rollback.

### N12. User-level learning

- **Gap.** The brief does not say if preferences are explicit or inferred, or if users can see them.
- **Assumption.** A profile for each user: output format (table or bullets), depth, charts or text, and similar fields. The agent saves an explicit statement at once. The agent proposes an inferred preference after it repeats several times, and saves it after the user agrees. The user can show and reset the profile (for example `/prefs`).
- **Evidence.** Brief Req 4.1: "learn user preferences over time." Practice: transparent memory that the user controls.

### N13. System-level learning

- **Gap.** "Improve itself" has no definition.
- **Assumption.** An offline loop, not self-modification in production:
  1. Collect feedback, user corrections, failed SQL, and self-corrections.
  2. An analyst reviews them and approves new trios, prompt fixes, and new test cases.
  3. A regression evaluation runs.
  4. The team releases the change.
- **Evidence.** Brief Req 4.2: "learn from past interactions and improve itself." Practice: automatic learning from user input can let an attacker poison the memory. Human review is the usual control.

### N14. QA targets and acceptance criteria

- **Gap.** The brief asks how we evaluate but gives no targets.
- **Assumption.**
  - An offline test set of about 50 cases, from golden trios and hand-written cases.
  - Checks: the SQL runs, the result matches the result of the reference SQL, and an LLM judge scores intent and report quality. A human checks a sample of the judge scores.
  - A safety test set: PII extraction, prompt injection, cross-scope access, and delete without confirmation. The release gate is zero failures.
  - UX: task success rate, turns to an answer, feedback ratio, time to first token, and a pilot with a few executives.
- **Evidence.** Brief Req 6. Practice: offline evaluation plus a release gate.

### N15. Report format and export

- **Gap.** The brief does not define the report structure or the output format.
- **Decision (2026-11-11, replaces the first assumption).** The agent shows the report in the chat and saves it in the library. On request, it also saves a file: HTML (the main format) or Markdown, with charts from the real figures. Sections: summary, findings with numbers, reasons, action items, date range and definitions, and method. Spec 32 gives the details.
- **Evidence.** Brief: "create a report for Q1 including insights and actions items for Q2".

### N16. Conversation sessions

- **Gap.** The brief refers to "this conversation" but does not define a conversation.
- **Assumption.** One CLI run is one conversation with an ID. The session state holds the history. The agent summarizes long sessions. A `--resume <id>` option continues an earlier conversation.
- **Evidence.** Brief: "Delete all the reports we made in this conversation" and "discuss about it".

### N17. Language

- **Gap.** The brief does not name a language.
- **Assumption.** English only.
- **Evidence.** The brief and the example questions are in English.

### N18. Extensibility contract

- **Gap.** The brief asks for easy extension but does not define the interface.
- **Assumption.** A tool registry. Each tool declares a name, an input schema, and a side-effect class: `read`, `write`, `destructive`, or `external_send`. Tools in the `destructive` and `external_send` classes use the same confirmation gate as delete. Charts, email, and web search are in the design only.
- **Evidence.** Brief: "easily extendable for new capabilities (generating graphs, sending reports via mail, searching the web for trends, etc)". Practice: MCP-style tool interfaces.

### N19. Production data source

- **Gap.** The brief says "a SQL database" with raw transaction logs, but the dataset is in BigQuery.
- **Assumption.** The production warehouse is BigQuery. The HLD keeps a small adapter interface for other SQL engines.
- **Evidence.** Brief: "A read-only connection to a SQL database" and "Use BigQuery integration to query and analyze the specified tables".

### N20. Evaluator test environment

- **Gap.** The brief does not say which GCP project and which Gemini tier OpsFleet will use to test.
- **Status.** Closed. OpsFleet confirmed that the agent queries BigQuery with the credentials of the person who runs it. The Gemini tier for testing is our decision: the free tier.
- **Assumption.**
  - OpsFleet uses its own free AI Studio key and its own GCP project with ADC.
  - The setup guide covers these items: `gcloud auth application-default login`, `GOOGLE_CLOUD_PROJECT`, the BigQuery API, Python 3.12, and `pip install -r requirements.txt` (exported with `uv export`).
  - The setup guide tells the reader to create the Gemini key in a project with no billing account (the free tier). A key from a project with billing in prepay mode and no credits returns HTTP 402.
  - The agent makes few LLM calls for each turn and retries with backoff on HTTP 429, so it works within free-tier limits.
- **Evidence.** OpsFleet: "using your own credentials". Brief: the Setup Instructions section and "Please be mindful of the rate limits". Code: `.env.example` and `requires-python = ">=3.12"`.

### N21. Agent framework and default model (our decision, not a client question)

- **Gap.** Deliverable 6 asks us to choose a framework and to state our experience with it. The brief prefers "one of the newer Google Gemini models" but does not name one. `pyproject.toml` has `google-genai` but no agent framework.
- **Why non-blocking.** This is our decision, not a question for OpsFleet. We make it in the architecture decisions. Work on the product documents and on the SQL guard does not depend on it. Reclassified from B5 on 2026-11-08.
- **Status.** Closed on 2026-11-11. Decision: **LangGraph**. The reasons are in `docs/product/architecture.md`, section 1.1. Development model: `gemini-3.5-flash-lite`. A test call succeeded on 2026-11-11. Spec 51 chooses the final model.
- **Assumption.**
  - Candidates: **LangGraph** or **Google ADK**.
  - The framework must give: native Gemini support, a pause step for human approval (Req 3), state between turns, and tracing hooks (Req 7).
  - Default model: a current Gemini Flash-class model, because of cost and free-tier limits. The model name is a configuration value, not code.
  - `docs/product/architecture.md` (section 1.1) compares the two frameworks. A separate document states the level of experience with the chosen framework.
- **Evidence.** Brief: "Use a framework of your choice, explain why you choose it and your level of experience with it." Brief: "You should preferably use one of the newer Google Gemini models ... Please be mindful of the rate limits." Code: `pyproject.toml` dependencies.

---

## 5. Questions and answers

### 5.1 Asked and answered

The questions went to OpsFleet in Slack. The project manager answered on 2026-11-10 or 2026-11-11. The technical contact confirmed the answers and the CLI argument.

| # | Question | Answer (short) | Gaps | Status |
|---|---|---|---|---|
| 1 | Roles, and how to select the user in the CLI | Managers see one or more brands. The CEO sees everything. We define the mapping. The `--user` CLI argument is fine. | B1, B2 | Closed |
| 2 | Other PII fields. Is a customer ID acceptable? | Names, email, street address, postal code, exact coordinates, and IP address are PII. Demographic fields only in aggregates. `user_id` or a salted hash is fine. PII must never reach the LLM. | B3 | Closed |
| 3 | Scope: a local CLI prototype plus an architecture document | Yes. CLI, no UI. BigQuery public dataset with our own credentials. Prototype: Req 2, 3, 5, 7. The other requirements are design only. | Section 1.3, N2, N20 | Closed |
| 4 | Load in production | About 100 active managers each day, 10 questions and 1 report each. About 1M new rows each day. A few users at the same time. 10–30 s for a simple question, 1–2 min for a long report. | N3 | Closed |

### 5.2 Decided by us (not asked)

On 2026-11-11 we decided to ask OpsFleet no more questions. We decided these points ourselves:

| # | Point | Our decision | Where |
|---|---|---|---|
| 1 | Saved reports: visibility, delete type, and "Client X" | A user sees and deletes only their own reports. Soft delete with a 30-day undo. "Client X" is a brand or another business entity that the report names. | B4 |
| 2 | Manager access to customer statistics that are not linked to their brands | No access. Only the `ceo` role sees them. | B2 |
| 3 | Hiding aggregates of fewer than 5 customers | Not in the prototype. A production option in the HLD. | B3 |
| 4 | Revenue, churn, "underspending", fiscal year | The definitions in N4. Calendar year. Each answer states the definitions it used. | N4 |
| 5 | Gemini tier for testing | Free tier. The setup guide explains how to create a free-tier key. | N20 |
| 6 | Production channel | Web chat or Google Chat / Slack, behind single sign-on. | N2 |

N21 (framework and model) is also our decision. On 2026-11-11 we chose LangGraph (`docs/product/architecture.md`, section 1.1).

---

## 6. Next steps

1. Write the AWOS product definition (`docs/product/product-definition.md`) from this document.
2. Write the HLD skeleton with an assumptions register. Each assumption in this document gets an ID in the register.
3. Confirm the default Gemini model with a test call. (N21: LangGraph was chosen on 2026-11-11.)
4. Start the prototype with the parts that the decisions for B1–B4 define:
   - the SQL guard (read-only, scope, PII, cost),
   - the report library and the confirmation flow,
   - retries and fallbacks,
   - tracing.
