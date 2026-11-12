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
