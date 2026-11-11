# Spec 51: Model comparison

- **Status:** Approved (owner review, 2026-11-11)
- **Phase and stream:** Phase 3 (together)
- **Depends on:** 23, 36, 50
- **Requirements covered:** Req 6 (quality assurance) and Req 5 (the fallback model). It also gives the reasons for the model choice in the technical explanation (Deliverable 2.1).

---

## Part A — Product (review this part)

### A1. Goal

The owner gets a clear choice of the default model and the fallback model, with figures. The team runs the same test sets on `gemini-3.5-flash-lite` and on `gemini-3.8-flash`. A rule that the owner approves before the runs decides the result. So the choice does not depend on a feeling after the runs.

### A2. Behavior the user sees

1. **Runs on both models.** The team runs the accuracy set and the safety set with each model. The code, the cases, the judge and the settings are the same. Only the model name changes.
2. **A comparison document.** `docs/evaluation/model-comparison.md` shows a table with the figures of both models, the cases where only one model passed, and the decision.
3. **The new setting.** `config/settings.yaml` gets the chosen default model and the fallback model.
4. **A fallback that works.** When the default model is not available or refuses calls for rate, the assistant uses the fallback model. The answer looks the same to the user.

### A3. Scope

- **In scope:** the runs, the metrics, the cost estimate for production, the decision rule, the comparison document and the setting.
- **Out of scope:** other models or providers, a fine-tuned model, the fallback code itself (spec 36), and a change of the test sets.

### A4. Limits the user sees

- The new runs need about 435 Gemini calls on `gemini-3.8-flash`, over 2 days. Each run needs the owner's "yes".
- The comparison uses 50 accuracy cases. Only a large difference between the models counts as a real difference (B2.3).

### A5. Done when

- [ ] Both models have a scored run of the accuracy set and the safety set on the same git commit.
- [ ] `docs/evaluation/model-comparison.md` has the table in B2.2, the paired counts and the decision with its reason.
- [ ] `config/settings.yaml` has the default model and the fallback model of the decision.
- [ ] The architecture document and the HLD (spec 10) state the chosen models and refer to the comparison.
- [ ] The owner approved the ⚑ rows in B1.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | Decision rule | The rule in B2.3: safety first, then a paired count of the correct answers, then the cost for each answer. | Choose by the highest pass rate | 50 cases give a large uncertainty. A small difference is not a real difference. Then the cost decides. |
| ⚑ | Judge in the comparison | The judge of spec 23 (`gemini-3.8-flash`). The decision rule uses only code-based metrics. The judge scores are in the report, but they do not decide. | A judge from another model family | A judge can prefer answers of its own model. We have only a Gemini key. |
| | Reuse of the spec 50 runs | The final runs of spec 50 count for `gemini-3.5-flash-lite` if they use the same commit. | Run both models again | This saves about 435 calls. Each run is scored against its own snapshot, so a data change between days does not make the results unfair. |
| | Production cost | Tokens for each answer multiplied by the paid prices on the Google pricing page on the day of the decision | Free-tier cost (0) | Production uses the paid tier (architecture section 4). |

### B2. Design

#### B2.1. The runs

1. Use the git commit of the final runs of spec 50.
2. Set `agent_model: gemini-3.8-flash` with the option `--model` of `opsfleet-eval run`. Change nothing else.
3. Run the accuracy set (day 1) and the safety set (day 2). The long conversation is optional. It costs about 85 more calls.
4. Run `opsfleet-eval compare` on the two accuracy runs and on the two safety runs.

#### B2.2. The comparison table

| Metric | `gemini-3.5-flash-lite` | `gemini-3.8-flash` |
|---|---|---|
| Safety pass rate | | |
| SQL execution-match rate | | |
| Answer correctness | | |
| Judge pass rate, answers and reports (information only) | | |
| Latency median and p95, questions | | |
| Latency median and p95, reports | | |
| Tokens in and out for each answer (median) | | |
| Gemini calls for each answer (median) | | |
| Self-correction rate | | |
| `llm_rate_limited` errors | | |
| Failures by error type | | |
| Production cost for each answer (paid prices) | | |
| Production cost for 1,000 questions each day, for 30 days | | |
| Free-tier limits (requests each minute and each day) | | |

The paired counts on the accuracy set:

- `b`: cases that only `gemini-3.8-flash` passes.
- `c`: cases that only `gemini-3.5-flash-lite` passes.

#### B2.3. The decision rule

1. **Safety first.** A model can be the default only with a safety pass rate of 100% and an execution-match rate of at least 90%.
2. **Quality.** If both models can be the default, compare the paired counts. If `b - c` is 4 or more, `gemini-3.8-flash` has better quality. Otherwise, the quality is equal.
3. **Equal quality.** The default is the model with the lower production cost for each answer, if its p95 latency meets the targets of spec 50.
4. **Better quality.** The default is `gemini-3.8-flash`, if its p95 latency meets the targets of spec 50. The document states the extra cost.
5. **Only one model passes step 1.** That model is the default.
6. **No model passes step 1.** The default stays `gemini-3.5-flash-lite`. The document lists the gaps, and the work goes back to spec 50.

**Fallback model:** the other model, if its safety pass rate is 100%. If it fails safety, the agent has no fallback model. It tells the user that the service is not available (spec 36). The free tier has separate limits for each model, so the fallback also helps when the default model refuses calls for rate.

### B3. Files and interfaces

```text
docs/evaluation/model-comparison.md     # table, paired counts, decision (in git)
config/settings.yaml                    # agent_model, fallback_model
```

`opsfleet-eval run` gets the option `--model <name>`. The run metadata stores the model name, and the run ID contains it.

### B4. Tests

- Offline: the decision rule as a function, with a test for each of the six steps in B2.3.
- Offline: `--model` changes only the model name in the run metadata and in the agent settings of the run.
- The fallback itself has its tests in spec 36.

### B5. Cost

| Run | Gemini calls | BigQuery |
|---|---|---|
| Accuracy set on `gemini-3.8-flash` | about 235 (agent and judge) | about 1 GB, plus a refresh if the data changed |
| Safety set on `gemini-3.8-flash` | about 200 | about 1 GB |
| Long conversation (optional) | about 85 | about 0.6 GB |
| Runs on `gemini-3.5-flash-lite` | 0 new (reuse of spec 50) | 0 |

Each run needs the owner's "yes". The free-tier daily limit of `gemini-3.8-flash` can be lower than our limit of 300. The runner then pauses, and `--resume` continues the next day.

### B6. Risks and open points

- `gemini-3.8-flash` can use more tokens for each answer. Then the token budget of spec 00 (60,000 in) can stop more answers. The table shows `limit_exceeded` in the failures by error type.
- Paid prices and free-tier limits change. The document states the date of the prices and the limits.
- The rule "`b - c` is 4 or more" is a simple threshold, not a statistical test. With 50 cases, it avoids a choice based on one or two cases.
- The judge can prefer `gemini-3.8-flash`. The decision does not use the judge scores for this reason.
