"""Data models of the reports (spec 32, B2.2, B2.3 and B3).

`ReportPlan` and `ReportDraft` are the structured outputs of the model. `Report` and
`ReportSummary` are rows of the library. The built content (`sections`, `charts`) is plain JSON,
so a file export works from the row alone, also after the result cache expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


# --- model outputs ---------------------------------------------------------------------------------
class PeriodModel(BaseModel):
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD, included


class PlannedQuery(BaseModel):
    id: str = ""
    purpose: str
    sql: str
    definitions_used: list[str] = Field(default_factory=list)


class ReportPlan(BaseModel):
    title: str
    period: PeriodModel
    compare_period: PeriodModel | None = None
    # Required (spec 50 round 1): with a default, the JSON schema let the model leave it out.
    queries: list[PlannedQuery]
    definitions_used: list[str] = Field(default_factory=list)


class TableRef(BaseModel):
    ref: str
    columns: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    text: str
    table: TableRef | None = None


class ActionItem(BaseModel):
    action: str
    why: str = ""
    metric_to_watch: str = ""


class ChartSpec(BaseModel):
    ref: str
    type: str = "bar"
    x: str
    y: list[str] = Field(default_factory=list)
    title: str = ""


class ReportDraft(BaseModel):
    title: str | None = None  # used only when no plan exists ("save this as a report")
    summary: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)
    definitions_used: list[str] = Field(default_factory=list)


class FigureFixes(BaseModel):
    """The answer to the one rewrite request of the figure check (spec 32, B2.4 step 6)."""

    sentences: list[str] = Field(default_factory=list)


# --- library rows ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReportSummary:
    report_id: str
    title: str
    period_start: str | None
    period_end: str | None
    created_at: str
    conversation_id: str
    file_html: str | None = None
    file_md: str | None = None

    @property
    def files(self) -> list[str]:
        return [n for n, f in (("HTML", self.file_html), ("MD", self.file_md)) if f]


@dataclass(frozen=True)
class ReportList:
    reports: list[ReportSummary]
    total: int


@dataclass
class Report:
    report_id: str
    owner_id: str
    conversation_id: str
    title: str
    question: str
    created_at: str
    period_start: str | None
    period_end: str | None
    scope_brands: list[str]
    sql: list[dict[str, Any]]
    body_markdown: str
    sections: dict[str, Any]
    charts: list[dict[str, Any]]
    definitions: list[str]
    entities: dict[str, list[str]]
    calculated_figures: int = 0
    unverified_figures: int = 0
    file_html: str | None = None
    file_md: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    trace_id: str | None = None
    deleted_at: str | None = None
    delete_batch_id: str | None = None

    def summary(self) -> ReportSummary:
        return ReportSummary(
            report_id=self.report_id,
            title=self.title,
            period_start=self.period_start,
            period_end=self.period_end,
            created_at=self.created_at,
            conversation_id=self.conversation_id,
            file_html=self.file_html,
            file_md=self.file_md,
        )
