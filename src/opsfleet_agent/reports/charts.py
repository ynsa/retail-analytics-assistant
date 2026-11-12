"""Charts of a report (spec 32, B2.4 step 3 and B2.5).

The model only chooses the result, the chart type and the columns. Code checks the choice against
the stored result, copies the data (at most 60 points) into the report row, and draws the chart:
an inline SVG with matplotlib for HTML, a Mermaid block for Markdown. So a chart cannot show a
value that is not in the query result.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opsfleet_agent.agent.formatting import ColumnKind, ResultData, format_value
from opsfleet_agent.config import ReportSettings
from opsfleet_agent.reports.models import ChartSpec

MAX_Y_COLUMNS = 3
MERMAID_LABEL_CHARS = 30
SVG_HASH_SALT = "opsfleet-report"
_ORDERED_X = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?(?:[T ].*)?$|^\d{4}-?Q[1-4]$")
_NUMERIC_KINDS = {
    ColumnKind.MONEY,
    ColumnKind.COUNT,
    ColumnKind.RATE,
    ColumnKind.PERCENT,
    ColumnKind.NUMBER,
}


# --- checks ---------------------------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ordered(values: list[Any], kind: str) -> bool:
    if kind == ColumnKind.YEAR:
        return True
    return bool(values) and all(
        _is_number(v) or (isinstance(v, str) and _ORDERED_X.match(v)) for v in values
    )


def check_chart(
    spec: ChartSpec, results: dict[str, ResultData], settings: ReportSettings
) -> tuple[dict[str, Any] | None, list[str]]:
    """The chart data after the checks of B2.4 step 3, or None when the chart is dropped."""
    title = (spec.title or spec.ref).strip()[:120]
    notes: list[str] = []
    result = results.get(spec.ref)
    if result is None or not result["rows"]:
        return None, [f"The chart '{title}' was not drawn: its result is missing."]
    names = [c.lower() for c in result["columns"]]
    wanted = [spec.x.lower(), *(y.lower() for y in spec.y)]
    if not spec.y or any(n not in names for n in wanted):
        return None, [f"The chart '{title}' was not drawn: a column is missing."]
    y_cols = [y.lower() for y in spec.y]
    if any(result["kinds"][names.index(y)] not in _NUMERIC_KINDS for y in y_cols):
        return None, [
            f"The chart '{title}' was not drawn: a value column is not a number."
        ]
    if len(y_cols) > MAX_Y_COLUMNS:
        y_cols = y_cols[:MAX_Y_COLUMNS]
        notes.append(
            f"The chart '{title}' shows only the first {MAX_Y_COLUMNS} value columns."
        )
    xi = names.index(spec.x.lower())
    yis = [names.index(y) for y in y_cols]
    rows = [r for r in result["rows"] if all(_is_number(r[i]) for i in yis)]
    if not rows:
        return None, [f"The chart '{title}' was not drawn: it has no values."]

    kind = spec.type.lower().strip()
    if kind not in settings.chart_types:
        kind = "bar"
    if kind == "pie" and (
        len(y_cols) != 1
        or len(rows) > settings.max_pie_slices
        or any(r[yis[0]] < 0 for r in rows)
    ):
        kind = "bar"
    if kind == "line":
        x_values = [r[xi] for r in rows]
        if len(rows) > settings.max_chart_points or not _ordered(
            x_values, result["kinds"][xi]
        ):
            kind = "bar"
        else:
            rows = sorted(rows, key=lambda r: str(r[xi]))
    if kind == "bar" and len(rows) > settings.max_bar_categories:
        total = len(rows)
        rows = sorted(rows, key=lambda r: r[yis[0]], reverse=True)
        rows = rows[: settings.max_bar_categories]
        notes.append(
            f"The chart '{title}' shows the top {settings.max_bar_categories} of {total} values."
        )
    rows = rows[: settings.max_chart_points]
    chart = {
        "ref": spec.ref,
        "type": kind,
        "requested_type": spec.type,
        "title": title,
        "x": result["columns"][xi],
        "y": [result["columns"][i] for i in yis],
        "kinds": {result["columns"][i]: result["kinds"][i] for i in yis},
        "labels": ["" if r[xi] is None else str(r[xi]) for r in rows],
        "series": {result["columns"][i]: [float(r[i]) for r in rows] for i in yis},
    }
    return chart, notes


def check_charts(
    specs: Iterable[ChartSpec], results: dict[str, ResultData], settings: ReportSettings
) -> tuple[list[dict[str, Any]], list[str]]:
    charts: list[dict[str, Any]] = []
    notes: list[str] = []
    specs = list(specs)
    for spec in specs:
        if len(charts) >= settings.max_charts:
            notes.append(f"Only the first {settings.max_charts} charts are drawn.")
            break
        chart, chart_notes = check_chart(spec, results, settings)
        notes += chart_notes
        if chart:
            charts.append(chart)
    return charts, notes


# --- data table under a chart -----------------------------------------------------------------------
def chart_table(chart: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """The columns and the formatted rows of the chart data."""
    columns = [chart["x"], *chart["y"]]
    rows = []
    for i, label in enumerate(chart["labels"]):
        cells = [label]
        for y in chart["y"]:
            cells.append(
                format_value(chart["series"][y][i], chart["kinds"].get(y, "number"))
            )
        rows.append(cells)
    return columns, rows


# --- SVG ------------------------------------------------------------------------------------------
def _pyplot() -> Any:
    """matplotlib with the Agg backend and a writable config folder (no home folder needed)."""
    os.environ.setdefault(
        "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "opsfleet-matplotlib")
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "svg.fonttype": "none",
            "svg.hashsalt": SVG_HASH_SALT,
            "font.family": "sans-serif",
            "font.size": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    return plt


COLORS = ("#2f5d8a", "#d08c2f", "#5b8c5a", "#8a5d9e", "#b5534a", "#6e7f8d")
_SVG_HEAD = re.compile(r"^.*?(?=<svg)", re.DOTALL)
_SVG_METADATA = re.compile(r"<metadata>.*?</metadata>\s*", re.DOTALL)
_XMLNS = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')


def _value_gid(prefix: str, series: int, index: int, value: float) -> str:
    """An element ID with the drawn value, so a test can compare the SVG with the result."""
    return f"{prefix}_s{series}_i{index}_v{value!r}"


def _clean_svg(svg: str, title: str) -> str:
    """Inline SVG for an offline HTML file: no XML header, no metadata, no URL."""
    svg = _SVG_HEAD.sub("", svg, count=1)
    svg = _SVG_METADATA.sub("", svg)
    svg = _XMLNS.sub("", svg)
    label = title.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return svg.replace("<svg", f'<svg role="img" aria-label="{label}"', 1).strip()


def draw_svg(chart: dict[str, Any]) -> str:
    """The chart as inline SVG. The same data gives the same bytes."""
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(7.0, 3.4), dpi=100)
    try:
        labels = chart["labels"]
        positions = list(range(len(labels)))
        if chart["type"] == "pie":
            y = chart["y"][0]
            values = chart["series"][y]
            wedges, _ = ax.pie(values, labels=labels, colors=COLORS, startangle=90)
            for i, (wedge, value) in enumerate(zip(wedges, values, strict=True)):
                wedge.set_gid(_value_gid("pie", 0, i, value))
            ax.axis("equal")
        elif chart["type"] == "line":
            for s, y in enumerate(chart["y"]):
                (line,) = ax.plot(
                    positions, chart["series"][y], marker="o", color=COLORS[s], label=y
                )
                line.set_gid(f"line_s{s}")
                for i, value in enumerate(line.get_ydata()):
                    point = ax.plot(
                        [positions[i]],
                        [value],
                        linestyle="none",
                        marker="o",
                        markersize=3,
                        color=COLORS[s],
                    )[0]
                    point.set_gid(_value_gid("pt", s, i, float(value)))
        else:
            count = len(chart["y"])
            width = 0.8 / count
            for s, y in enumerate(chart["y"]):
                offsets = [p + (s - (count - 1) / 2) * width for p in positions]
                bars = ax.bar(
                    offsets, chart["series"][y], width=width, color=COLORS[s], label=y
                )
                for i, bar in enumerate(bars):
                    bar.set_gid(_value_gid("bar", s, i, float(bar.get_height())))
        if chart["type"] != "pie":
            ax.set_xticks(positions)
            short = [lbl if len(lbl) <= 18 else lbl[:17] + "…" for lbl in labels]
            ax.set_xticklabels(
                short,
                rotation=30 if len(labels) > 6 else 0,
                ha="right" if len(labels) > 6 else "center",
            )
            ax.yaxis.set_major_formatter(
                plt.FuncFormatter(
                    lambda v, _p: format_value(
                        v, chart["kinds"].get(chart["y"][0], "number")
                    )
                )
            )
            if len(chart["y"]) > 1:
                ax.legend(frameon=False)
        ax.set_title(chart["title"])
        fig.tight_layout()
        buffer = io.StringIO()
        fig.savefig(buffer, format="svg", metadata={"Date": None, "Creator": None})
    finally:
        plt.close(fig)
    return _clean_svg(buffer.getvalue(), chart["title"])


# --- Mermaid ---------------------------------------------------------------------------------------
def mermaid_label(text: str) -> str:
    """A label inside double quotes: no double quote, no line break, at most 30 characters."""
    clean = re.sub(r"\s+", " ", str(text)).replace('"', "'").replace("`", "'").strip()
    if len(clean) > MERMAID_LABEL_CHARS:
        clean = clean[: MERMAID_LABEL_CHARS - 1].rstrip() + "…"
    return clean


def _number(value: float) -> str:
    return f"{round(value, 2):g}" if abs(value) < 1e15 else str(round(value, 2))


def to_mermaid(chart: dict[str, Any]) -> str:
    """A Mermaid block: `pie` for a pie chart, `xychart-beta` for a bar or line chart."""
    title = mermaid_label(chart["title"])
    if chart["type"] == "pie":
        y = chart["y"][0]
        lines = [f'pie title "{title}"']
        for label, value in zip(chart["labels"], chart["series"][y], strict=True):
            lines.append(f'    "{mermaid_label(label)}" : {_number(value)}')
        return "```mermaid\n" + "\n".join(lines) + "\n```"
    labels = ", ".join(f'"{mermaid_label(lbl)}"' for lbl in chart["labels"])
    lines = [
        "xychart-beta",
        f'    title "{title}"',
        f"    x-axis [{labels}]",
        f'    y-axis "{mermaid_label(", ".join(chart["y"]))}"',
    ]
    mark = "line" if chart["type"] == "line" else "bar"
    for y in chart["y"]:
        values = ", ".join(_number(v) for v in chart["series"][y])
        lines.append(f"    {mark} [{values}]")
    return "```mermaid\n" + "\n".join(lines) + "\n```"
