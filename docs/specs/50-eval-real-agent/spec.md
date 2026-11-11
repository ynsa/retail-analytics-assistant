# Spec 50: Evaluation on the real agent

- **Status:** Approved (owner review, 2026-11-11)
- **Phase and stream:** Phase 3 (together)
- **Depends on:** 21, 22, 23, and the prototype specs 30–37
- **Requirements covered:** Req 6 (quality assurance). The runs check Req 2, 3, 5 and 7 on the finished prototype.

---

## Part A — Product (review this part)

### A1. Goal

The owner gets proof that the finished assistant meets set targets for correct answers, safety, speed and cost. The team runs the test sets, studies each failure, corrects the cause and runs the sets again. The work ends when the targets are met, or after at most 4 rounds with a list of the remaining gaps.

### A2. Behavior the user sees

1. **A first full run.** The team runs the accuracy set and the safety set on the finished agent. The long conversation runs only in the final run. The owner gets the first reports.
2. **A judge check.** A person labels 30 judged answers. The team adjusts the judge rubric until the judge agrees with the person. Then the rubric does not change.
3. **Rounds of corrections.** In each round, the team groups the failures by cause, corrects the causes, and runs the failed cases again. A log shows each round: what failed, why, what changed, and the new figures.
4. **A final run.** On the final version of the code, the team runs all sets again. The final report shows each target and its result.
5. **The documents for OpsFleet.** The final reports go to `docs/evaluation/`. The README links to them.

### A3. Scope

- **In scope:** the preparation, the judge calibration, the baseline runs, the failure analysis and the correction rounds. The spec also covers the final runs, the exit criteria and the documents in `docs/evaluation/`.
- **Out of scope:** new test cases (only corrections of wrong cases), the model choice (spec 51), and changes of the targets of the product definition.

### A4. Limits the user sees

- At most 4 correction rounds. After round 4, the open gaps go into the final report.
- The full sets need about 520 Gemini calls. The daily limit is 300 calls, so a full set of runs takes 2 days.
- Each run with more than 10 calls needs a cost estimate and the owner's "yes" (spec 23).

### A5. Done when

- [ ] The final runs on one git commit meet all exit criteria in B2.4. If a criterion is not met, the final report names it and gives the reason.
- [ ] The safety gate has zero failures in the final runs.
- [ ] `docs/evaluation/iterations.md` has one entry for each round.
- [ ] `docs/evaluation/report-<date>-accuracy.md`, `-safety.md` and `-long.md` exist.
- [ ] Each code bug that a round found has an offline regression test.
- [ ] The owner approved the ⚑ rows in B1.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | Exit criteria | The table in B2.4. The main targets: zero safety failures, 90% execution match, p95 latency of 30 s for questions and 120 s for reports. | Lower targets, or only safety | The 90% and the latency come from the product definition and from OpsFleet. |
| ⚑ | Number of rounds | At most 4 correction rounds | No limit | The assignment has a time budget of 6–12 hours. Each round costs Gemini calls. |
| | Re-runs in a round | Only the failed cases and the smoke set, with no judge calls. This is a prototype shortcut. In production, every change runs the full sets (spec 10, section 4.6). The free code checks (result match, safety rules) score them. | The full sets each time, with the judge | Fewer calls. The judge runs in the first and the final run only. The final run checks all cases. (Owner's choice, 2026-11-11.) |
| | Changes of cases | A case changes only if the case is wrong. A person reviews the change and writes the reason in the log. | Change cases freely | A test set that changes to fit the agent proves nothing. |

### B2. Design

#### B2.1. The loop

```mermaid
flowchart TD
    prep["Preparation<br/>specs 30–37 done, cases ready,<br/>smoke test passes"] --> base["Baseline runs<br/>accuracy, safety"]
    base --> cal["Judge calibration<br/>30 human labels, freeze the rubric"]
    cal --> ana["Failure analysis<br/>one cause for each failure"]
    ana --> corr["Corrections<br/>one change set, regression tests"]
    corr --> rerun["Re-run the failed cases<br/>and the smoke set"]
    rerun --> more{"New failures,<br/>and round below 4?"}
    more -->|"yes"| ana
    more -->|"no"| final["Final runs on one commit<br/>all sets"]
    final --> exit{"Exit criteria met?"}
    exit -->|"yes"| docs["Copy the reports to docs/evaluation/"]
    exit -->|"no, rounds left"| ana
    exit -->|"no, round 4 done"| gaps["Write the open gaps<br/>in the final report"]
    gaps --> docs
```

#### B2.2. Judge calibration

1. Take 30 judged items from the baseline runs: answers, reports and safety `J-` checks, with both judge passes and judge fails.
2. A person labels them blind with `opsfleet-eval review`.
3. Use 20 items to adjust the rubric. Keep 10 items aside. Do not put them in the rubric as examples.
4. Score the 10 kept items with the adjusted rubric. Report the true-positive and true-negative rates.
5. Freeze the rubric. Its hash goes into each later run.
6. A later change of the rubric or the judge model needs a new calibration (BP-E7).

#### B2.3. Failure analysis

For each failed case, the analyst opens the trace with `/trace <trace_id>` and the case record. The analyst gives the failure one cause from this list:

| Cause | Example | Usual correction |
|---|---|---|
| `sql_logic` | A wrong join or a wrong filter | Prompt, schema notes, examples |
| `definition` | Revenue counts returned items | Glossary (spec 31) |
| `scope_or_guard` | The guard blocks an allowed query | Guard rules (spec 30) |
| `safety` | The answer shows a figure of another brand | Guard, scanner, prompt (specs 30, 31) |
| `answer_text` | The SQL is correct, but the text gives a wrong figure | Answer prompt |
| `self_correction` | The loop stops after an error that it can correct | Error messages to the model (BP-T3) |
| `report_build` | A section is missing | Report builder (spec 32) |
| `context_memory` | A filter of an earlier turn is lost | Summary and state (spec 34) |
| `judge_error` | The judge fails a correct answer | Rubric, with a new calibration |
| `case_error` | The reference SQL is wrong | Case review (spec 21) |
| `data_drift` | The data changed during the run | Snapshot refresh |
| `infra` | A rate limit or a timeout | Resilience (spec 36), or run again |

Each entry in `docs/evaluation/iterations.md` has the round, the date and the run IDs. It also has the failed cases, their causes, the changes and the figures after the change.

#### B2.4. Exit criteria

All criteria apply to the final runs on one git commit.

| Area | Criterion | Target | Source |
|---|---|---|---|
| Safety | Safety pass rate (safety set and long conversation) | 100% (zero failures) | Spec 22, product definition 1.4 |
| Correct answers | SQL execution-match rate | ≥ 90% | Product definition 1.4 |
| Correct answers | Answer correctness | ≥ 90% | Product definition 1.4 |
| Correct answers | Judge pass rate, answers (gating criteria) | ≥ 85% | Our target |
| Reports | Judge pass rate, reports (gating criteria) | ≥ 4 of 5 | Our target |
| Judge | Human labels | ≥ 20, with both rates ≥ 0.8 | BP-E5 |
| Speed | p95 latency of a question | ≤ 30 s | OpsFleet (N3) |
| Speed | p95 latency of a report | ≤ 120 s | OpsFleet (N3) |
| No dead ends | Cases that end with `internal_error` | 0 | Product definition 1.4 |
| Cost | Median Gemini calls for each question | ≤ 4 | Our target |
| Cost | `limit_exceeded` in the accuracy set | 0 | Spec 00 |

### B3. Files and interfaces

```text
docs/evaluation/
  iterations.md                     # the log of the rounds (in git)
  report-<date>-accuracy.md         # copies of the final reports (in git)
  report-<date>-safety.md
  report-<date>-long.md
```

The run folders stay in `data/eval/runs/` with the mark `keep` in `run.json`. The copies in `docs/` contain no PII and no secrets. The output scanner checks them.

### B4. Tests

- Each correction of a code bug adds an offline regression test (no network) before the re-run.
- The smoke set runs in each round as a quick check that nothing else broke.
- The final runs are the acceptance test of this spec.

### B5. Cost

| Step | Gemini calls | BigQuery | Days |
|---|---|---|---|
| Baseline: accuracy | about 235 | about 2.5 GB with the refresh | 1 |
| Baseline: safety | about 200 | about 1.4 GB | 1 |
| Judge calibration: 10 items scored again | about 10 | 0 | — |
| Each round: failed cases and smoke set, no judge | about 30–60 | about 0.5 GB | — |
| Final runs, with the long conversation | about 520 | about 4 GB | 2 |
| **Total with 4 rounds** | **about 1,150** | **about 9.5 GB** | **about 5** |

All BigQuery figures are far below the free 1 TB each month. The Gemini calls stay inside the daily limit of 300, so the work spreads over about 5 days. On the free tier, the cost is $0. At the paid tier, the estimate is about $2–4 (prices of 2026-11-11). Each run starts only after its cost estimate and the owner's "yes" (spec 23). The owner can raise the daily limit in `config/settings.yaml` for the evaluation days, inside the free-tier limits of Google.

### B6. Risks and open points

- The free-tier daily limits of Google can be lower than our limit of 300. Then the runs pause more often, and the calendar gets longer.
- A model answer can change between two runs with the same code. One final run is a sample, not a proof. The report must say this.
- The 90% target on 50 cases allows 5 failed cases. One case is 2 percentage points.
- The data can change between the baseline and the final runs. The snapshot refresh of spec 21 handles this. A case with a new result shape can need a new review before the final run.
