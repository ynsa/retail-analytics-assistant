# Product Definition: Retail Analytics Chat Assistant

- **Version:** 1.0
- **Status:** Proposed
- **Sources:** the assignment brief (`docs/brief/Original task.md`), the answers from OpsFleet, and our decisions in `docs/analysis/gap-analysis.md`. Items marked _(assumption)_ are our decisions. OpsFleet did not state them.

---

## 1. The Big Picture (The "Why")

### 1.1. Project Vision & Purpose

Executives of a retail company ask questions about sales, products and customers in plain language. They get a correct answer in seconds and a report with action items in minutes. They do not need an analyst or SQL for routine questions. The assistant shows only the data that each person is allowed to see, and it never shows personal data about customers.

### 1.2. Target Audience

- **Primary users:** non-technical executives of a retail company.
  - **Brand managers.** Each manager is responsible for one or more brands and analyzes only those brands.
  - **The CEO.** The CEO sees all brands.
- **Size:** tens to a few hundred executives. About 100 managers use the assistant each day, with about 10 questions and 1 saved report each. Only a few people use it at the same time.
- **Secondary users (in the design only):**
  - Analysts, who supply approved examples of questions, queries and reports.
  - A persona editor, for example in the CEO office, who changes the tone of the reports without help from developers.

### 1.3. User Personas

These personas are fictional.

- **Persona 1: "Maya, the Brand Manager"**
  - **Role:** Manager of two clothing brands.
  - **Goal:** To know each week why the sales of the brands went up or down. To compare regions and customer groups. To get a short report with actions for the next period.
  - **Frustration:** An analyst takes days to answer a "why" question. Dashboards show what changed but not why. Maya cannot write SQL.

- **Persona 2: "Daniel, the CEO"**
  - **Role:** Chief executive officer, responsible for all brands.
  - **Goal:** To get a quarterly report with insights and action items across all brands, in the tone that the company uses that week.
  - **Frustration:** Reports from different teams use different definitions and styles. Daniel is concerned that customer data can leak to the wrong people.

### 1.4. Success Metrics

- **Speed:** a simple question gets an answer in 10–30 seconds. A long report takes 1–2 minutes. (Targets from OpsFleet.)
- **Correct answers:** at least 90% of the questions in the test set get the same figures as the reference answer of an analyst. _(assumption)_
- **Safe data:** zero personal customer data in any answer, and zero answers with data from brands outside the user's scope. Measured on a safety test set. _(assumption: the test set)_
- **Safe deletes:** zero reports deleted without an explicit confirmation from the owner.
- **No dead ends:** every failure ends with a clear message, and the conversation stays open. The user does not see a crash.
- **Self-service:** managers get answers to routine questions without an analyst. Target: 80% of routine questions answered in the first conversation. _(assumption)_

---

## 2. The Product Experience (The "What")

### 2.1. Core Features

- **Ask and discuss in plain language.** The user asks about customer behavior, product performance and sales over time, and asks follow-up questions. The assistant also explains what data is available. It can answer questions that need several steps.
- **Reports with insights and action items.** On request, the assistant writes a report, for example "a report for Q1 with action items for Q2". It saves the report in the user's personal report library.
  - A report is different from a chat answer. A chat answer is short and answers one question. A report covers a period and stays in the library. It always has the same sections: summary, findings with numbers, reasons, action items, date range and definitions, and method. The user can list, open, export and delete it.
  - On request, the assistant also saves the report as a file: HTML (the main format) or Markdown. The file can contain charts, for example sales by month or a comparison of brands. The charts show the real figures from the data.
- **Safe by design.**
  - Each manager sees only the data of their brands. The CEO sees all brands.
  - Personal customer data (names, email, addresses, exact location, IP address) is never shown and never goes to the AI model. City, state, country, age, gender and traffic source appear only in totals.
  - The assistant answers only data-analysis questions.
- **Safe delete of saved reports.** A user can delete their own reports with one sentence, for example "Delete all the reports we made in this conversation". The assistant first shows the exact list and asks for an explicit confirmation. The delete also removes the report files that the assistant created.
  - The delete is a "soft delete": the report stays hidden for 30 days, and the user can undo the delete in that period. After 30 days, the system erases the report permanently.
- **Reliable and traceable.** The assistant corrects its own query errors and empty results before it gives up, with a maximum number of attempts. For an empty result, it first checks whether the data really has no rows or the query has an error. An example of an error is a filter value that does not exist. If the assistant cannot answer, it tells the user why and what can help, and the chat continues. It stays open when an outside service fails. The support team can see each step of each conversation to find out what went wrong.

### 2.2. User Journey

1. Maya starts the assistant. The assistant knows that Maya manages two brands.
2. Maya asks: "Why did sales of my brands drop last month, compared to the month before?"
3. The assistant checks the data for Maya's brands only. In about 20 seconds, it answers with the figures, the main reasons, the date range and the definitions that it used.
4. Maya asks a follow-up question: "How does the West compare to the East?" The assistant answers in the context of the first question.
5. Maya asks: "Create a report for Q1 with insights and action items for Q2, as an HTML file." The assistant writes the report in 1–2 minutes, saves it in Maya's library, and creates an HTML file with a chart of the monthly sales. Maya opens the file in a browser.
6. Maya asks for the email addresses of the top customers. The assistant refuses and offers the customer IDs and their total spend.
7. Maya asks about a brand that Maya does not manage. The assistant says that the brand is outside Maya's scope.
8. At the end, Maya says: "Delete all the reports we made in this conversation." The assistant lists the 3 reports and asks Maya to type `delete 3`. Maya types it, and the assistant deletes the reports. Maya can undo the delete for 30 days.

---

## 3. Project Boundaries

### 3.1. What's In-Scope for this Version

This version is a working prototype with a command-line interface (CLI), plus a design of the full production system. OpsFleet confirmed this scope.

- A chat in the terminal. The person who starts the assistant gives the user name at the start.
- Two roles: brand manager (one or more brands) and CEO (all brands). _(assumption: the brand-to-manager mapping)_
- Questions on the company's sales data: orders, order items, products and customers.
- Reports with insights and action items, a personal report library, and a list of saved reports.
- Report files in HTML or Markdown, with charts drawn from the real figures. This is our addition. The brief lists charts as a future capability, and we chose to build them for report files in this version.
- Brand scope, protection of personal data, and analysis-only answers (Requirement 2).
- Delete of the user's own reports with a strict confirmation and a 30-day undo (Requirement 3).
- Self-correction of query errors and empty results, clear error messages, and cost limits (Requirement 5).
- A trace of each conversation, and metrics that show when and why the assistant fails (Requirement 7).
- An evaluation harness: test questions with checked expected results, a safety and behavior test set, and an evaluation report. This is our addition, because the quality of the answers must be measured, not assumed.
- The prototype runs on another machine with any operating system. The main way is a Docker image. The setup guide also explains a run without Docker.
- A full design document for production, which also covers the items in section 3.2 that say "in the design".

### 3.2. What's Out-of-Scope (Non-Goals)

- **In the design, not built in this version:**
  - Learning from approved analyst examples (the "golden knowledge") (Requirement 1).
  - Learning the preferences of each user, and learning from past conversations (Requirement 4).
  - The production test and evaluation process before each release (Requirement 6). The prototype already has an evaluation harness (section 3.1).
  - A change of report tone by non-developers, without a new release (Requirement 8).
  - New capabilities: charts in plain chat answers, reports by email, and web search for trends.
  - A production channel such as a web chat or Slack, with company sign-in.
- **Not planned:**
  - A production web or mobile user interface for executives in this version.
  - Any change to the sales data. The only data that the assistant changes is the report library.
  - Website events and other tables outside the four sales tables.
  - Languages other than English. _(assumption)_
  - Hiding of totals for very small customer groups (fewer than 5 customers). The design shows it as a later option. _(assumption)_

### 3.3. Nice to Have (Our Own Wish)

This item is not a requirement from OpsFleet. OpsFleet expects a CLI and needs no UI. We want this item for easier manual testing. We build it only after the required scope works.

- **A local web UI for manual testing.**
  - It runs only on the developer's machine.
  - It shows the history of conversations, so a tester can open an earlier conversation again.
  - It lets the tester select the user and the role before a conversation. The tester can then compare the answers for a brand manager and for the CEO.
  - It uses the same assistant as the CLI. All safety rules (brand scope, personal data, delete confirmation) stay the same.
  - The CLI stays the main interface of the prototype.
