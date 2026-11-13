# Evaluation report: 20261113T2019Z-accuracy-flash-lite-5982d1f

| Field | Value |
|---|---|
| Date (UTC) | 2026-11-13T20:19:05+00:00 |
| Set | accuracy (set hash 47d4359bce4e) |
| Agent model | gemini-3.5-flash-lite |
| Judge model | gemini-3.5-flash-lite |
| Git commit | 5982d1f |
| Snapshot date | 2026-11-13 |
| Table change times | orders 2026-11-13T03:45:58.556000+00:00, order_items 2026-11-13T03:45:58.965000+00:00, products 2026-11-13T03:45:56.765000+00:00, users 2026-11-13T03:46:16.963000+00:00 |
| Run status | done |
| Approval | --yes (plan approved in the conversation) |

A model answer can differ from run to run. One run is a sample, not a proof.

## Summary

| Metric | Value | Target (spec 50) | Result |
|---|---|---|---|
| Case pass rate | 94.2% | – | – |
| SQL execution-match rate | 92.5% | >= 90.0% | pass |
| Answer correctness | 97.7% | >= 90.0% | pass |
| Judge pass rate, answers | 100.0% | >= 85.0% | pass |
| Judge pass rate, reports | 100.0% | >= 80.0% | pass |
| Judge pass rate, safety criteria | – | – | – |
| Safety pass rate (gate 100%) | – | >= 100.0% | – |
| p95 latency of a question (s) | 15.98 | <= 30.00 | pass |
| p95 latency of a report (s) | 26.48 | <= 120.00 | pass |
| Cases that end with internal_error | 0 | <= 0 | pass |
| Median Gemini calls for each question | 3 | <= 4 | pass |
| Cases that end with limit_exceeded | 0 | <= 0 | pass |

Scored cases: 52. Skipped cases: 0. Cases with only outside errors: 0 (not in the rates). Cases that ran again after an outside error: 2.

## Safety gate

No safety cases in this run.

## Results by capability, difficulty and role

| Capability | Cases | Pass rate | Execution match | Answer correctness |
|---|---|---|---|---|
| customer_behavior | 8 | 100.0% | 100.0% | 100.0% |
| empty_results | 4 | 100.0% | 100.0% | 100.0% |
| multi_step | 8 | 75.0% | 75.0% | 100.0% |
| product_performance | 10 | 100.0% | 100.0% | 100.0% |
| regression | 2 | 100.0% | 100.0% | 100.0% |
| report | 5 | 100.0% | – | 100.0% |
| schema | 5 | 100.0% | – | 100.0% |
| time_metrics | 10 | 90.0% | 90.0% | 88.9% |

| Difficulty | Cases | Pass rate | Execution match | Answer correctness |
|---|---|---|---|---|
| easy | 15 | 100.0% | 100.0% | 100.0% |
| hard | 14 | 92.9% | 88.9% | 100.0% |
| medium | 23 | 91.3% | 90.5% | 95.5% |

| User | Cases | Pass rate | Execution match | Answer correctness |
|---|---|---|---|---|
| alex | 7 | 100.0% | 100.0% | 100.0% |
| ceo | 27 | 100.0% | 100.0% | 100.0% |
| maya | 18 | 83.3% | 76.9% | 93.3% |

## Failures by error type and by scorer reason

| Error type | Spans | Cases that ended with it |
|---|---|---|
| bq_syntax | 1 | 0 |
| guard_parse_error | 2 | 0 |

| Scorer reason | Failed cases |
|---|---|
| fact_missing | 1 |
| missing_column | 1 |
| wrong_value | 2 |

## Latency, tokens, calls and cost

| Metric | Value |
|---|---|
| Median latency of a question (s) | 11.94 |
| Median latency of a report (s) | 21.97 |
| Tokens in for each answer, median | 8,643 |
| Tokens in for each answer, p95 | 20,553 |
| Tokens out for each answer, median | 586 |
| Tokens out for each answer, p95 | 3,101 |
| Gemini calls for each answer, p95 | 5 |
| Gemini calls for each answer, maximum | 6 |
| Tool calls for each answer, median | 0 |
| Tool calls for each answer, maximum | 2 |
| BigQuery bytes for each answer, median | 21.0 MB |
| BigQuery bytes of the run | 1,205.9 MB |
| Turns with a new attempt | 1.8% |
| New attempts that end with a result | 100.0% |

| Cost | Planned | Actual |
|---|---|---|
| Gemini calls, agent | 161 | 177 |
| Gemini calls, agent, in discarded runs (in the line above) | – | 5 |
| Gemini calls, judge | 13 | 13 |
| Tokens in | 683000 | 563481 |
| Tokens out | 53500 | 54170 |
| BigQuery bytes, agent | 975.2 MB | 1,205.9 MB |
| BigQuery bytes, snapshots | 0.0 MB | 0.0 MB |
| Price at the paid tier (USD) | 0.34 | 0.3045 |

| Tool | Calls |
|---|---|
| report.export | 1 |
| report.save | 5 |

## Judge

| Rubric:criterion | Passed | Judged | Rate |
|---|---|---|---|
| answer:A1 | 8 | 8 | 100.0% |
| answer:A2 | 8 | 8 | 100.0% |
| answer:A3 | 7 | 8 | 87.5% |
| answer:A4 | 8 | 8 | 100.0% |
| answer:A5 | 8 | 8 | 100.0% |
| answer:A6 | 8 | 8 | 100.0% |
| report:R1 | 5 | 5 | 100.0% |
| report:R2 | 5 | 5 | 100.0% |
| report:R3 | 5 | 5 | 100.0% |
| report:R4 | 5 | 5 | 100.0% |
| report:R5 | 5 | 5 | 100.0% |
| report:R6 | 5 | 5 | 100.0% |
| report:R7 | 5 | 5 | 100.0% |

Human labels: 0. Agreement: –. True-positive rate: –. True-negative rate: –.

**Warning:** fewer than 20 human labels. The rates are a spot check, not a statistical proof. Add labels with `uv run opsfleet-eval review --run 20261113T2019Z-accuracy-flash-lite-5982d1f`.

## Failed cases

### TM-08 (maya, time_metrics)

- Question: How many orders with my brands were delivered in each month of Q3 2026?
- Reasons: execution_match=wrong_value, answer_facts=fact_missing
- Expected: [{"month": "2026-07-01", "orders_delivered": 61}, {"month": "2026-08-01", "orders_delivered": 66}, {"month": "2026-09-01", "orders_delivered": 82}]
- Agent: [{"month": "2026-07", "orders": 44, "all_groups_total_orders": 150}, {"month": "2026-08", "orders": 47, "all_groups_total_orders": 150}, {"month": "2026-09", "orders": 59, "all_groups_total_orders": 150}]
- Trace: 1124830b060473b20db406b825da2ecd

### MS-02 (maya, multi_step)

- Question: Which of my brands grew more from 2024 to 2025, and which categories explain the growth?
- Reasons: execution_match=missing_column
- Expected: [{"brand": "Calvin Klein", "year": 2024, "revenue": 25057.019987106323}, {"brand": "Calvin Klein", "year": 2025, "revenue": 32048.910079956055}, {"brand": "Carhartt", "year": 2024, "revenue": 19825.12990951538}, {"brand": "Carhartt", "year": 2025, "revenue": 30092.719864845276}]
- Agent: [{"brand": "Calvin Klein", "category": "Jeans", "revenue_2024": 152.97999954223633, "revenue_2025": 537.2999992370605, "revenue_growth_pct": 251.2223825629684}, {"brand": "Calvin Klein", "category": "Socks & Hosiery", "revenue_2024": 26.790000915527344, "revenue_2025": 83.58000183105469, "revenue_growth_pct": 211.98207903984115}, {"brand": "Calvin Klein", "category": "Shorts", "revenue_2024": 427.5800018310547, "revenue_2025": 1060.9299926757812, "revenue_growth_pct": 148.12432483570072}, {"brand": "Calvin Klein", "category": "Plus", "revenue_2024": 119.5, "revenue_2025": 259.0, "revenue_growt (+31 rows)
- Trace: 0a7d6144381ecde92744ad51d03e01f7

### MS-05 (maya, multi_step)

- Question: Show my top 5 categories by revenue in 2025. / For the first one, show the monthly revenue.
- Reasons: execution_match=wrong_value
- Expected: [{"month": "2025-01-01", "revenue": 1691.6099853515625}, {"month": "2025-02-01", "revenue": 1094.9400024414062}, {"month": "2025-03-01", "revenue": 1791.6199798583984}, {"month": "2025-04-01", "revenue": 1777.7499961853027}, {"month": "2025-05-01", "revenue": 2384.720012664795}] (+7 rows)
- Agent: [{"month": "2025-01", "revenue": 4387.269989013672, "all_groups_total_revenue": 62141.62994480133}, {"month": "2025-02", "revenue": 3894.710012435913, "all_groups_total_revenue": 62141.62994480133}, {"month": "2025-03", "revenue": 5345.809982299805, "all_groups_total_revenue": 62141.62994480133}, {"month": "2025-04", "revenue": 3858.649998664856, "all_groups_total_revenue": 62141.62994480133}, {"month": "2025-05", "revenue": 6364.540053367615, "all_groups_total_revenue": 62141.62994480133}] (+7 rows)
- Trace: a169b08f76bdfd2fc7612702f9b41ba9, 93b1b7ff82a94917cdedf1c84bea1c0d

## Cases with only outside errors

None.

## Skipped cases

None.

## Comparison with the last run of the same set

- **Warning:** The set files differ. The runs are not comparable case by case.
- **Warning:** The prompts differ.
- **Warning:** judge_model: None -> gemini-3.5-flash-lite.
- **Warning:** git_commit: c5e01e2 -> 5982d1f.

| Metric | A | B | Change |
|---|---|---|---|
| case_pass_rate | 78.9% | 94.2% | +15.3% |
| execution_match_rate | 93.3% | 92.5% | -0.8% |
| answer_correctness | 81.2% | 97.7% | +16.5% |
| safety_pass_rate | – | – | – |
| judge_answer_rate | – | 100.0% | – |
| judge_report_rate | – | 100.0% | – |
| judge_safety_rate | – | – | – |
| latency_p95_question_s | 16.55 | 15.98 | -0.57 |
| latency_p95_report_s | 18.32 | 26.48 | +8.16 |
| calls_median_question | 3 | 3 | 0 |
| tokens_in_median | 8,530 | 8,643 | +113 |
| tokens_out_median | 725 | 586 | -139 |
| bq_bytes_total | 555.7 MB | 1,205.9 MB | +650.1 MB |
| self_correction_share | 0.0% | 1.8% | +1.8% |
| internal_errors | 0 | 0 | 0 |
| limit_exceeded | 0 | 0 | 0 |

Paired counts: only A passed 1 (c), only B passed 4 (b), both passed 14, both failed 0.

Pass to fail (A passed, B failed): MS-02.

Fail to pass (A failed, B passed): PP-07, PP-09, RP-02, RP-05.

Cases in only one run: CB-01, CB-03, CB-05, CB-06, CB-08, ER-01, ER-02, ER-03, ER-04, MS-01, MS-05, MS-07, MS-08, PP-01, PP-03, PP-05, PP-06, PP-08, PP-10, RP-04, SC-01, SC-02, SC-03, SC-04, SC-05, TM-01, TM-02, TM-03, TM-04, TM-05, TM-07, TM-08, TM-09.
