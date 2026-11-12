# Evaluation rounds (spec 50)

One entry for each round of corrections. The details of each round are in its own file.

## Round 1 (2026-11-12)

- **Baseline runs (commit `8814c53`):** accuracy `20261112T1051Z-accuracy-flash-lite-8814c53`, safety `20261112T1101Z-safety-flash-lite-8814c53`.
- **Re-runs (no judge):** safety `20261112T1135Z-safety-flash-lite-d2f8c9a`, accuracy `20261112T1141Z-accuracy-flash-lite-db527de`, safety (5 cases after the review by hand) `20261112T1154Z-safety-flash-lite-db527de`.
- **Analysis, corrections and results:** [round-1-analysis.md](round-1-analysis.md).
- **Failed cases and causes:** safety 28 (agent 7, outside errors 21); accuracy 18 (agent 13, scorer 1, case 1, outside errors 3); 11 accuracy cases skipped by the snapshot rule.
- **Figures after the change (code checks, no judge):** safety 28 of 28 re-run cases pass, so 61 of 61 pass the code checks; accuracy 11 of 11 scored re-run cases pass, 7 cases had outside errors again, 11 cases are still unscored.
- **Cost:** 111 Gemini calls, about 336 MB of BigQuery.
- **Open for round 2:** section 6 of the analysis.

## After round 1: the Docker check (2026-11-12)

- **Found:** "How many orders were placed in 2024?" (`ceo`) ran twice in Docker. Both queries used 2025, and the second one had no status filter (20,671 and 27,629 orders). Both footers said "Period: 1 Jan – 31 Dec 2024" and cited the orders definition.
- **Cause:** the prompt had no rule for a named year, and the calendar gave a ready SQL condition only for `last_year` (2025). The model copied it. The footer came from the model's `active_filters`, not from the SQL.
- **Changes:** a `<named_periods>` block with the exact dates of each year, quarter or month in the question; rules in `write_sql.md`; a new node `check_sql` that compares the SQL with the stated period and with the status filter of each cited definition, sends feedback, and fails the step after 3 mismatches; a footer from the checked steps (spec 31, B7).
- **Regression cases:** RG-01 (`ceo`, orders in 2024) and RG-02 (`maya`, revenue of the brands in 2024) in the accuracy set (spec 21, B7).
