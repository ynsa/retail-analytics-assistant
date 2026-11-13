"""Report checks: the six sections (spec 23, B2.4 `report_structure`) and the report file (`file`)."""

from __future__ import annotations

import re
from pathlib import Path

from opsfleet_agent.evals.scorers import ScoreResult

# Section -> words that its heading can contain (spec 32 B2.3: the six sections of a report).
SECTIONS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "overview"),
    "findings": ("finding", "insight", "key results"),
    "reasons": ("reason", "driver", "why", "explanation"),
    "action items": ("action", "recommendation", "next step"),
    "date range and definitions": ("date range", "definition", "period"),
    "method": ("method", "how this report", "data and sources", "approach"),
}
_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+(?P<h>.+?)\s*#*\s*$|\*\*(?P<b>[^*]+)\*\*:?\s*$)"
)


def headings(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            out.append((m.group("h") or m.group("b") or "").strip().casefold())
    return out


def report_structure(report_text: str | None) -> ScoreResult:
    if not report_text:
        return ScoreResult.fail(
            "section_missing", note="no report in the library for this turn"
        )
    found = headings(report_text)
    missing = [
        name
        for name, words in SECTIONS.items()
        if not any(w in h for h in found for w in words)
    ]
    if missing:
        return ScoreResult.fail("section_missing", missing=missing, headings=found[:20])
    return ScoreResult.ok(headings=found[:20])


def report_file(files: list[str], wanted_suffix: str | None = None) -> ScoreResult:
    """The report file exists. `wanted_suffix` is ".html" when the user asked for HTML."""
    existing = [f for f in files if Path(f).exists()]
    if wanted_suffix:
        existing = [f for f in existing if f.lower().endswith(wanted_suffix)]
    if not existing:
        return ScoreResult.fail("file_missing", files=files, wanted=wanted_suffix)
    return ScoreResult.ok(files=existing)
