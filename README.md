# Retail Analytics Chat Assistant

A chat assistant for the executive teams of a retail company. Executives ask questions about sales, products and customers in plain language. The assistant answers with figures from BigQuery, writes reports with action items, protects personal data, and shows each manager only the data of their brands.

This repository contains the design and a command-line prototype for the OpsFleet technical assignment.

## Status

Phase 1 is done: the foundation (spec 00) and the walking skeleton (spec 01). A user can ask a simple data question in the terminal and get an answer from the real BigQuery data.

Phase 2 has started. Query safety (spec 30) is active:

- Each manager sees only the data of their brands. Code rewrites every query to the brand scope.
- Personal data never reaches the model or the user. The SQL guard blocks PII columns, and it blocks a single customer together with a location, age, gender or traffic source. The result masker cleans the rows, and the output scanner cleans the answer.
- When a query returns no rows, the assistant checks if the data is really empty or if the query has an error.

Self-correction, multi-step questions, reports, the delete flow and the conversation memory follow in Phase 2 (specs 31–37).

The production design document and the diagrams (specs 10 and 11) are ready for review.

## The prototype

![Prototype on one machine](docs/design/diagrams/prototype.png)

Source: [prototype.drawio](docs/design/diagrams/prototype.drawio). The production design on Google Cloud is in the [design document](docs/design/hld.md).

## Run the prototype

```bash
uv run opsfleet-agent chat --user maya     # users: ceo, maya, alex (config/users.yaml)
uv run opsfleet-agent chat --user maya --refresh-cache   # build the value cache again
uv run pytest                              # unit tests, no network
uv run pytest -m live                      # live tests: see the cost in each test file
```

The cost limits for each answer and for each day are in `config/settings.yaml`. The traces of each answer are in `data/traces/`.

At the first start, the assistant builds `data/value_cache.json` with one small query (about 40 MB billed). The cache holds the brand names, the filter values and the date range of the data. The assistant builds it again after 7 days. The start stops when a brand in `config/users.yaml` does not exist in the data, and the message gives the close names.

## Documents

| Document | Content |
|---|---|
| [Product definition](docs/product/product-definition.md) | What the product does, for whom, and the scope |
| [Design document (HLD)](docs/design/hld.md) | Production design on Google Cloud and the technical explanation of each requirement |
| [Architecture](docs/product/architecture.md) | Technology choices and diagrams |
| [Framework choice](docs/framework-choice.md) | Why the project uses LangGraph, and how |
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
