# Retail Analytics Chat Assistant

A chat assistant for the executive teams of a retail company. Executives ask questions about sales, products and customers in plain language. The assistant answers with figures from BigQuery, writes reports with action items, protects personal data, and shows each manager only the data of their brands.

This repository contains the design and a command-line prototype for the OpsFleet technical assignment.

## Status

Phase 1 is done: the foundation (spec 00) and the walking skeleton (spec 01). A user can ask a simple data question in the terminal and get an answer from the real BigQuery data. The brand scope, the full PII protection, reports and the delete flow follow in Phase 2 (specs 30–37).

The evaluation is ready (specs 20–23): the test sets, the reference results and the runner. The runs on the finished agent follow in Phase 3 (spec 50).

## Run the prototype

```bash
uv run opsfleet-agent chat --user maya     # users: ceo, maya, alex (config/users.yaml)
uv run pytest                              # unit tests, no network
uv run pytest -m live                      # live smoke test: 2 Gemini calls, 1 small query
```

The cost limits for each answer and for each day are in `config/settings.yaml`. The traces of each answer are in `data/traces/`.

## Evaluate the assistant

The evaluation sets are in `evals/sets/`: 50 accuracy questions with checked reference SQL (spec 21), 61 safety cases and a 32-turn conversation (spec 22). The runner (spec 23) shows a plan with the Gemini calls and the cost before it starts. A run with more than 10 planned calls starts only after you type `yes`.

```bash
uv run opsfleet-eval plan --set accuracy          # the plan only: no Gemini call, free BigQuery dry runs
uv run opsfleet-eval run --set smoke              # 5 cases, at most 10 Gemini calls, no judge
uv run opsfleet-eval run --set accuracy           # asks for "yes" (about 216 calls)
uv run opsfleet-eval run --resume <run_id>        # continue a run that stopped at its cap or the daily limit
uv run opsfleet-eval snapshot --set accuracy      # refresh the reference results after a data change
uv run opsfleet-eval review --run <run_id>        # label 10 judged answers blind
uv run opsfleet-eval compare <run_a> <run_b>      # metric changes and the cases that changed
```

Each run writes `data/eval/runs/<run_id>/report.md`.

## Documents

| Document | Content |
|---|---|
| [Product definition](docs/product/product-definition.md) | What the product does, for whom, and the scope |
| [Architecture](docs/product/architecture.md) | Technology choices and diagrams |
| [Roadmap](docs/product/roadmap.md) | The phases and the parts of the work |
| [Specs](docs/specs/README.md) | One spec for each part of the work, with its review status |
| [Gap analysis](docs/analysis/gap-analysis.md) | Open points in the assignment, the answers from OpsFleet, and the decisions |
| [Assignment](docs/brief/Original%20task.md) | The original task |

## Development setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and the Google Cloud CLI.

```bash
uv sync                          # create the virtual environment and install the dependencies
uv run pre-commit install        # turn on the automatic checks before each commit
cp .env.example .env             # then set GOOGLE_API_KEY and GOOGLE_CLOUD_PROJECT
gcloud auth application-default login
```

The checks before each commit are ruff (Python), Biome (JavaScript and CSS), a secret scan (gitleaks), and checks that `uv.lock` and `requirements.txt` are up to date.

A Docker image and the full setup guide follow with the prototype.
