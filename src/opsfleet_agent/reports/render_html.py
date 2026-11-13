"""The HTML file of a report (spec 32, B2.5).

One file from a Jinja2 template with autoescape, inline CSS and inline SVG charts from matplotlib.
No JavaScript, no external URL, and a Content-Security-Policy that blocks all network loads.
Markdown becomes HTML with markdown-it-py, with raw HTML, links and images turned off.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from opsfleet_agent.reports.charts import draw_svg
from opsfleet_agent.reports.models import Report
from opsfleet_agent.reports.render_md import chart_marker, report_markdown

TEMPLATES = Path(__file__).parent / "templates"
# Attributes with text that a person reads. The scanner checks them with the text nodes.
TEXT_ATTRIBUTES = frozenset({"aria-label", "title", "alt", "content", "summary"})


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    md.disable(["link", "image", "autolink", "html_inline", "html_block"])
    return md


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"], default=True),
        undefined=StrictUndefined,
    )


def render_html(report: Report) -> str:
    body = _markdown().render(report_markdown(report, "marker"))
    for index, chart in enumerate(report.charts):
        figure = f'<figure class="chart">{draw_svg(chart)}</figure>'
        body = body.replace(f"<p>{chart_marker(index)}</p>", figure)
    template = _environment().get_template("report.html.j2")
    return template.render(title=report.title, body=Markup(body))


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        self.texts += [v for k, v in attrs if k in TEXT_ATTRIBUTES and v]

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.texts.append(data)


def html_texts(html: str) -> list[str]:
    """The text content and the readable attribute values of an HTML file (for the scanner)."""
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return collector.texts
