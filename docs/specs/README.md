# Specs

Each part of the work has one spec. The phases and the order are in [the roadmap](../product/roadmap.md). New specs use [the template](_template.md).

## How to review a spec

- **Part A (Product):** review all of it. It describes what the user or the reviewer gets and sees.
- **Part B (Technical):** review only the rows marked ⚑ in section B1. These are the main decisions. The other rows follow from earlier decisions.
- A spec changes to **Approved** after the review. Work on a part starts only when its spec is approved.

## Index

| # | Spec | Phase / stream | Depends on | ⚑ | Status |
|---|---|---|---|---|---|
| 00 | [Foundation](00-foundation/spec.md) | 1 | – | 2 | Done |
| 01 | [Walking skeleton](01-walking-skeleton/spec.md) | 1 | 00 | 0 | Done |
| 10 | [Design document (HLD) and technical explanation](10-design-document/spec.md) | 2 / A | 00 | 3 | Done |
| 11 | [Draw.io diagrams](11-drawio-diagrams/spec.md) | 2 / A | 10 | 2 | Done |
| 20 | [Research: evaluation and agent best practices](20-eval-research/spec.md) | 2 / B | 00 | 2 | Done |
| 21 | [Evaluation dataset](21-eval-dataset/spec.md) | 2 / B | 20 | 3 | Done |
| 22 | [Safety and behavior test set](22-safety-test-set/spec.md) | 2 / B | 20 | 3 | Done |
| 23 | [Evaluation runner and scoring](23-eval-runner/spec.md) | 2 / B | 20, 21, 22, 37 | 3 | Done |
| 30 | [Query safety](30-query-safety/spec.md) | 2 / C | 00, 01 | 3 | Done |
| 31 | [Agent core](31-agent-core/spec.md) | 2 / C | 30 | 3 | Done |
| 32 | [Reports](32-reports/spec.md) | 2 / C | 31 | 3 | Done |
| 33 | [Delete with confirmation](33-delete-confirmation/spec.md) | 2 / C | 32 | 3 | Done |
| 34 | [Long conversations and memory](34-long-conversations/spec.md) | 2 / C | 31 | 3 | Done |
| 35 | [Observability and statistics](35-observability/spec.md) | 2 / C | 00 | 3 | Done |
| 36 | [Resilience](36-resilience/spec.md) | 2 / C | 00, 31 | 3 | Done |
| 37 | [CLI](37-cli/spec.md) | 2 / C | 31–36 | 2 | Done |
| 40 | [Local test UI: screens and behavior](40-local-ui/spec.md) | 2 / A | 31–35 | 2 | Done |
| 50 | [Evaluation on the real agent](50-eval-real-agent/spec.md) | 3 | 21–23, 30–37 | 2 | Approved |
| 51 | [Model comparison](51-model-comparison/spec.md) | 3 | 50 | 2 | Approved |
| 52 | [Local test UI: build](52-local-ui-build/spec.md) | 3 | 40 | 0 | Done |
| 53 | [Docker and final checks](53-docker-and-final-checks/spec.md) | 3 | 37 | 3 | Approved |
| 54 | [README and setup guide](54-readme-and-setup-guide/spec.md) | 3 | 53 | 3 | Done |
| 55 | [Framework choice and experience document](55-experience-document/spec.md) | 3 | – | 0 | Done |
