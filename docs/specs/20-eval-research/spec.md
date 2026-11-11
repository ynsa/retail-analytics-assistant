# Spec 20: Research: evaluation framework and agent best practices

- **Status:** Approved (owner review, 2026-11-11)
- **Phase and stream:** Phase 2, stream B (evaluation)
- **Depends on:** 00
- **Requirements covered:** Req 6 (quality assurance). The best practices also apply to Req 2, 5 and 7 through specs 30, 31, 34 and 36.

---

## Part A — Product (review this part)

### A1. Goal

The owner gets one recommended way to evaluate the assistant, with the reasons and the sources. The later specs (21, 22, 23, 50, 51) build on this choice. The owner also gets a short list of best practices that the agent specs must apply.

### A2. Behavior the user sees

This spec is research. It has no behavior in the assistant. The owner reads three results:

1. **A comparison of six evaluation options.** The options are DeepEval, Ragas, promptfoo, LangSmith, Inspect AI, and plain pytest with our own scorers. Each option gets the same eight checks (B2.1).
2. **One recommendation.** The recommendation is plain pytest with our own scorers and an LLM judge that uses our Gemini client (B1, B2.2).
3. **A list of best practices with sources.** Each practice has an ID (for example `BP-S2`) and names the spec that must apply it (B2.3).

### A3. Scope

- **In scope:** the comparison, the recommendation and the sources. The spec also gives best practices for context management, prompt-injection defense, tool design, bounded retries and evaluation.
- **Out of scope:** the evaluation code (spec 23) and the test cases (specs 21 and 22). The production evaluation service belongs to the design document (spec 10).

### A4. Limits the user sees

The research made no Gemini calls and no BigQuery queries. The facts about the tools come from their documentation on 2026-11-11. Some facts come from third-party pages. B6 marks them.

### A5. Done when

- [ ] The comparison table covers the six options and the eight checks.
- [ ] The spec recommends one option and gives the reasons.
- [ ] Each best practice has an ID, a source with a URL, and the spec that applies it.
- [ ] Specs 21, 22 and 23 use the recommended option.
- [ ] The owner approved the ⚑ rows in B1.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | Evaluation framework | Plain pytest and our own scorers. The LLM judge calls Gemini through `GeminiClient` (spec 00). | DeepEval, promptfoo, Inspect AI, Ragas, LangSmith | Only our own code puts every judge call through `TurnBudget` and `DailyLedger`. No option gives execution match on BigQuery out of the box. The project already uses pytest. No new service and no Node.js. |
| ⚑ | Judge format | Binary pass or fail for each rubric criterion, with a short written critique. No 1–5 scale. | A 1–5 score, or one overall score | A person can check a pass or a fail. A score of 3 or 4 gives no clear action (Hamel Husain). Binary labels let us measure the true-positive and true-negative rates of the judge. |
| | Ideas that we take from other tools | The rubric style of G-Eval and `llm-rubric`. The red-team plugin categories of promptfoo as a checklist for spec 22. The row and column comparison of Ragas `DataCompyScore` for spec 23. | Install the tools | We get the ideas without the dependencies and without calls outside our budget. |
| | Second choice | Inspect AI, if our own runner becomes too large | — | It is MIT-licensed, local, supports Gemini, and has model-graded scorers and a log viewer. |

### B2. Design

#### B2.1. Comparison of the options

The checks:

1. **Gemini:** works with Gemini through an AI Studio key (`GOOGLE_API_KEY`).
2. **Judge:** LLM as judge with our own written rubric.
3. **SQL:** scores SQL by execution match (result against result).
4. **Safety:** supports safety and red-team tests.
5. **Local:** runs locally without a paid service.
6. **Cost control:** every model call has a known count and goes through our budget.
7. **pytest and CI:** fits pytest and a CI job.
8. **Dependencies:** what we must add.

| Check | DeepEval | Ragas | promptfoo | LangSmith | Inspect AI | pytest + own scorers |
|---|---|---|---|---|---|---|
| Gemini | Yes. `GeminiModel` reads `GOOGLE_API_KEY`. | Yes. `llm_factory(..., provider="google")`. | Yes. The `google:` provider reads `GOOGLE_API_KEY`. | The service does not call the model. Our code calls Gemini. | Yes. `--model google/<name>` with `GOOGLE_API_KEY`. | Yes. Our `GeminiClient` from spec 00. |
| Judge | G-Eval and other custom metrics. | Custom LLM metrics. | `llm-rubric` assertion with a grader model. | Custom evaluators. | `model_graded_qa()` with a custom template. A vote of several judge models is possible. | Our rubric files and one judge call for each judged turn. |
| SQL | No built-in metric. A custom metric is necessary. | `DataCompyScore` compares result data (CSV) by rows or by columns. `SQLSemanticEquivalence` uses an LLM. | A custom Python or JavaScript assertion. | A custom evaluator. | A custom scorer. | Our own scorer (spec 23). |
| Safety | A separate package, DeepTeam. | No. | Strong. Many red-team plugins, mapped to OWASP. Attack generation uses an OpenAI model by default. | No built-in red-team tests. | Safety cases are tasks that we write. | Our own cases (spec 22). |
| Local | Yes. The Confident AI cloud is optional. Telemetry is on by default. `DEEPEVAL_TELEMETRY_OPT_OUT` stops it. | Yes. | Yes. MIT license. OpenAI announced the purchase of promptfoo on 2026-03-09. | No. It is a hosted service. The free plan has 1 seat and 5,000 traces each month. | Yes. MIT license, local log viewer. | Yes. |
| Cost control | The library makes the judge calls. We need a custom model class to count them. | The library makes the calls. We need a wrapper to count them. | Calls go outside our budget. It has a `delay` option. | The calls stay in our code. The traces go to the service. | Calls go outside our budget unless we write a custom model provider. | Full. Each call goes through `TurnBudget` and `DailyLedger`. |
| pytest and CI | pytest style. The docs prefer `deepeval test run`. | A library. We call it from pytest. | A separate Node.js CLI. | A pytest plugin: `@pytest.mark.langsmith`. | Its own CLI: `inspect eval`. | Native. |
| Dependencies | `deepeval` and its packages | `ragas` and its packages | Node.js and npm | `langsmith` and an account | `inspect-ai` | None |

#### B2.2. Why we recommend pytest and our own scorers

1. The main score is execution match on BigQuery. No option gives it for BigQuery out of the box. We write this scorer in all cases.
2. The owner's rule says that a run with more than 10 Gemini calls needs a cost estimate and a "yes" first. The runner must know the exact number of calls. Our own judge makes one call for each judged turn. The library options make their own calls.
3. Spec 00 already has the budget, the daily counter, the trace and the error types. Our scorers use them directly.
4. The safety checks are mostly code checks on the trace, the recorded queries and the report library. A framework does not help with these checks.
5. The project uses pytest 9.2.0 and needs no new runtime and no account.

What we accept: we write and maintain the runner, the report and the comparison ourselves (spec 23). Inspect AI stays the second choice.

#### B2.3. Best practices that later specs must apply

**Context management in long conversations**

| ID | Practice | Applies in |
|---|---|---|
| BP-C1 | Keep the key facts (user, role, brands, definitions, active filters, period) in a structured state field. Do not keep them only in the chat history. | 34 |
| BP-C2 | When the history reaches a token threshold, replace the older turns with a summary. Keep the last turns in full. | 34 |
| BP-C3 | Remove old raw tool results from the context. Keep a short reference, for example the SQL and the row count. | 31, 34 |
| BP-C4 | Put the key facts at the start or at the end of the prompt. Models use information in the middle of a long context less well. | 31, 34 |
| BP-C5 | Test the memory with a long scripted conversation. Use probe questions that do not change between runs. | 22 |

**Prompt-injection defense (OWASP Top 10 for LLM Applications 2025)**

| ID | Practice | OWASP | Applies in |
|---|---|---|---|
| BP-S1 | Enforce access in code, outside the model: the SQL guard, the brand scope, the table allowlist, least privilege. | LLM01, LLM06 | 30 |
| BP-S2 | Separate the instructions from the data. Put data values and tool results inside marked blocks. Tell the model that the content of a block is data. | LLM01 | 31 |
| BP-S3 | Do not put secrets in the system prompt. Assume that a user can extract the prompt. | LLM07 | 31 |
| BP-S4 | Treat the model output as untrusted. The guard checks each SQL. The output scanner checks each answer. | LLM05, LLM02 | 30 |
| BP-S5 | A person approves each destructive action. The approval binds to the exact action. | LLM06 | 33 |
| BP-S6 | Limit the calls, the tokens, the time and the bytes for each answer and each day. | LLM10 | 00, 36 |
| BP-S7 | An input filter is not a security boundary. Use it to log and to route, not to protect. | LLM01 | 31 |
| BP-S8 | Test with attacks and also with normal requests, to measure false refusals. | LLM01 | 22 |
| BP-S9 | Do not send personal data to a free-tier model. Google can use free-tier content to improve its products, and human reviewers can read it. | LLM02 | 30, 23 |

**Tool design**

| ID | Practice | Applies in |
|---|---|---|
| BP-T1 | Use few tools with clear names and a shared prefix for related tools. | 31, 32, 33 |
| BP-T2 | Return short, high-signal results. Use a row cap and truncation with sensible defaults. | 30, 31 |
| BP-T3 | Write error messages that tell the model how to correct the call. | 31 |
| BP-T4 | Join steps that always run together into one tool. | 31 |
| BP-T5 | Evaluate the tools with realistic multi-step tasks that have a checkable result. | 21 |

**Bounded retries**

| ID | Practice | Applies in |
|---|---|---|
| BP-R1 | Use exponential backoff with jitter, a maximum number of attempts and a total deadline. | 00, 36 |
| BP-R2 | Retry only temporary errors (HTTP 429 and 5xx). Do not retry a 4xx error. | 36 |
| BP-R3 | Count the SQL self-correction attempts separately from the network retries. The maximum is 3 (architecture section 6). | 31 |
| BP-R4 | No loop calls a paid API without a hard limit. | 00, 23 |

**Evaluation method**

| ID | Practice | Applies in |
|---|---|---|
| BP-E1 | Build the golden set along several dimensions: capability, difficulty, role and query type. | 21 |
| BP-E2 | Score SQL by the result of the query, not by the SQL text (execution accuracy, as in BIRD). | 21, 23 |
| BP-E3 | Look at the failures first. Correct the clear errors before you build or tune the judge. | 50 |
| BP-E4 | Use a binary judge with a written critique for each criterion. | 23 |
| BP-E5 | Calibrate the judge against human labels. Report the true-positive and true-negative rates, not only the agreement. Keep the test labels out of the judge prompt. | 23, 50 |
| BP-E6 | Know the judge biases: position, length and self-preference. Give the judge the reference result and the same criteria each time. | 23, 51 |
| BP-E7 | Calibrate the judge again after a change of the judge model or the rubric. | 23, 51 |
| BP-E8 | Run the offline tests in each CI run. Run the live evaluation only on request, with a hard call limit. | 23 |

#### B2.4. Sources

Read on 2026-11-11.

1. DeepEval, Gemini models: https://deepeval.com/integrations/models/gemini
2. DeepEval, data privacy and telemetry: https://deepeval.com/docs/data-privacy
3. Ragas, SQL metrics: https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/sql/
4. Ragas, Gemini integration: https://docs.ragas.io/en/stable/howtos/integrations/gemini/
5. promptfoo, Google AI Studio provider: https://promptfoo.dev/docs/providers/google
6. promptfoo, red-team plugins: https://www.promptfoo.dev/docs/red-team/plugins
7. OpenAI, purchase of promptfoo: https://openai.com/blog/openai-to-acquire-promptfoo
8. LangSmith, pytest integration: https://docs.langchain.com/langsmith/pytest
9. LangSmith, pricing: https://www.langchain.com/pricing-langsmith
10. Inspect AI: https://inspect.aisi.org.uk/ and model grading: https://inspect.aisi.org.uk/model-graded.html
11. OWASP Top 10 for LLM Applications 2025: https://genai.owasp.org/llm-top-10/
12. OWASP LLM Prompt Injection Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html
13. Hines et al., "Defending Against Indirect Prompt Injection Attacks With Spotlighting": https://arxiv.org/abs/2403.14720
14. Anthropic, "Effective context engineering for AI agents": https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
15. Anthropic, "Writing effective tools for agents": https://www.anthropic.com/engineering/writing-tools-for-agents
16. Liu et al., "Lost in the Middle: How Language Models Use Long Contexts": https://arxiv.org/abs/2307.03172
17. LangGraph, short-term memory (trim and summarize): https://docs.langchain.com/oss/python/langgraph/add-memory
18. AWS Architecture Blog, "Exponential Backoff And Jitter": https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
19. Google Cloud, retry strategy: https://docs.cloud.google.com/storage/docs/retry-strategy
20. Li et al., BIRD benchmark (execution accuracy): https://arxiv.org/abs/2305.03111
21. Hamel Husain, "Creating a LLM-as-a-Judge That Drives Business Results": https://hamel.dev/blog/posts/llm-judge/
22. Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena": https://arxiv.org/abs/2306.05685
23. Gemini API Additional Terms of Service: https://ai.google.dev/gemini-api/terms

### B3. Files and interfaces

This spec creates no code. Spec 23 defines the files of the evaluation code. Specs 31, 34 and 36 refer to the IDs in B2.3.

### B4. Tests

No tests. The review of this spec is the check.

### B5. Cost

Research only: 0 Gemini calls, 0 BigQuery bytes.

### B6. Risks and open points

- The LangSmith plan facts (5,000 traces, 14 days of retention, self-hosting only in the Enterprise plan) come partly from third-party pages. We do not use LangSmith, so the decision does not depend on them.
- The promptfoo purchase by OpenAI can change the license or the multi-model support later. We take only ideas from promptfoo.
- Hamel Husain advises about 100 labelled examples for each failure type to measure a judge well. Our human sample is smaller (spec 23). It is a spot check, not a statistical proof. The report must say this.
- The free-tier rate limits for each Gemini model can change. Spec 23 reads the `llm_rate_limited` errors and stops the run in a controlled way.
