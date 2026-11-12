"""The Markdown body of a report and the Markdown file (spec 32, A2.2 and B2.5).

One function writes the body in three chart styles:

- `line`: each chart is one line. This text is `body_markdown` in the library, for `/open`.
- `mermaid`: each chart is a Mermaid block with a data table under it (the `.md` file).
- `marker`: each chart is a marker paragraph that the HTML renderer replaces with an SVG.

The sections are always in this order: summary, findings, reasons, action items, date range and
definitions, method and SQL appendix.
"""

from __future__ import annotations

from typing import Any, Literal

from opsfleet_agent.reports.charts import chart_table, to_mermaid
from opsfleet_agent.reports.models import Report

ChartStyle = Literal["line", "mermaid", "marker"]
SECTION_TITLES = (
    "Summary",
    "Findings",
    "Reasons",
    "Action items",
    "Date range and definitions",
    "Method and SQL appendix",
)


def chart_marker(index: int) -> str:
    return f"[[chart:{index}]]"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def markdown_table(columns: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(_cell(c) for c in columns) + " |",
        "|" + "---|" * len(columns),
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none)"


def _chart_block(index: int, chart: dict[str, Any], style: ChartStyle) -> str:
    if style == "line":
        return f"[Chart: {chart['title']} ({chart['type']}). Export as HTML to see it.]"
    columns, rows = chart_table(chart)
    table = markdown_table(columns, rows)
    if style == "mermaid":
        return f"**Chart: {chart['title']}**\n\n{to_mermaid(chart)}\n\n{table}"
    return f"{chart_marker(index)}\n\n{table}"


def _findings(
    sections: dict[str, Any], charts: list[dict[str, Any]], style: ChartStyle
) -> str:
    parts = []
    for i, finding in enumerate(sections.get("findings") or [], start=1):
        parts.append(f"**{i}.** {finding['text']}")
        table = finding.get("table")
        if table and table.get("columns"):
            parts.append(markdown_table(table["columns"], table["rows"]))
            if table.get("note"):
                parts.append(f"_{table['note']}_")
    for missing in sections.get("missing") or []:
        parts.append(f"> **Missing part:** {missing}")
    if not parts:
        parts.append("No findings.")
    for i, chart in enumerate(charts):
        parts.append(_chart_block(i, chart, style))
    return "\n\n".join(parts)


def _actions(items: list[dict[str, str]]) -> str:
    lines = []
    for i, item in enumerate(items, start=1):
        text = f"{i}. **{item['action'].rstrip('.')}.**"
        if item.get("why"):
            text += f" Why: {item['why']}"
        if item.get("metric_to_watch"):
            text += f" Metric to watch: {item['metric_to_watch']}"
        lines.append(text)
    return "\n".join(lines) or "No action items."


def _date_range(section: dict[str, Any]) -> str:
    parts = ["\n".join(f"- {line}" for line in section.get("lines") or [])]
    definitions = section.get("definitions") or []
    if definitions:
        parts.append("Definitions:")
        parts.append(
            "\n".join(f"- **{d['key']}**: {d['meaning']}" for d in definitions)
        )
    return "\n\n".join(parts)


def _method(section: dict[str, Any]) -> str:
    parts = [" ".join(section.get("lines") or [])]
    for query in section.get("queries") or []:
        heading = f"**{query['id']}: {query['purpose']}**"
        if query.get("status") == "ok":
            mb = query["bytes_billed"] / 1e6
            detail = f"BigQuery job {query['job_id'] or 'unknown'}, {mb:.1f} MB billed."
        else:
            detail = f"No result: {query.get('reason') or 'the query failed'}"
        parts.append(f"{heading}\n\n{detail}")
        if query.get("sql"):
            parts.append(f"```sql\n{query['sql'].strip()}\n```")
    notes = section.get("notes") or []
    if notes:
        parts.append("Notes:\n\n" + "\n".join(f"- {n}" for n in notes))
    return "\n\n".join(parts)


def markdown_body(
    title: str,
    subtitle: str,
    sections: dict[str, Any],
    charts: list[dict[str, Any]],
    style: ChartStyle = "line",
) -> str:
    blocks = [
        f"# {title}",
        f"_{subtitle}_",
        f"## {SECTION_TITLES[0]}\n\n{_bullets(sections.get('summary') or [])}",
        f"## {SECTION_TITLES[1]}\n\n{_findings(sections, charts, style)}",
        f"## {SECTION_TITLES[2]}\n\n{_bullets(sections.get('reasons') or [])}",
        f"## {SECTION_TITLES[3]}\n\n{_actions(sections.get('action_items') or [])}",
        f"## {SECTION_TITLES[4]}\n\n{_date_range(sections.get('date_range') or {})}",
        f"## {SECTION_TITLES[5]}\n\n{_method(sections.get('method') or {})}",
    ]
    return "\n\n".join(blocks).strip() + "\n"


def subtitle_of(report: Report) -> str:
    return (
        f"Report {report.report_id} · {report.sections.get('period_text', '')}".rstrip(
            " ·"
        )
    )


def report_markdown(report: Report, style: ChartStyle = "line") -> str:
    return markdown_body(
        report.title, subtitle_of(report), report.sections, report.charts, style
    )


def render_md_file(report: Report) -> str:
    """The `.md` file: Mermaid charts, each followed by its data table."""
    return report_markdown(report, "mermaid")
