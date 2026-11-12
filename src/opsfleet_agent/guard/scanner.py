"""Output scanner: the last safety net (spec 30, B2.6).

It runs on the final chat answer, and later on report bodies, report files (spec 32) and the
conversation summary (spec 34). Each match becomes `[removed]`. The caller adds one note
(`SCANNER_NOTE`). The trace gets the pattern names and the counts, never the matched text.
The patterns are for US-style text (spec 30, B6).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from opsfleet_agent.guard.rules import SCANNER_NOTE
from opsfleet_agent.tracing import span

REMOVED = "[removed]"

EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)
# A US-style number ((555) 123-4567, 555-123-4567, +1 555 123 4567) or an international number
# that starts with "+". Plain numbers (1234567890) and amounts (1,234,567.89) do not match.
PHONE = re.compile(
    r"(?<![\w.,+-])(?:"
    r"(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]\d{4}"
    r"|\+\d{1,3}(?:[ .-]\d{2,4}){2,4}"
    r")(?![\w-]|[.,]\d)"
)
STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z0-9][A-Za-z0-9'-]*\.?\s+){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl)\b\.?"
)
COORDINATES = re.compile(
    r"(?<![\d.])(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})(?!\d|\.\d)"
)

_PHONE_SEPARATORS = set(" .-()+")


def _is_phone(text: str) -> bool:
    digits = sum(ch.isdigit() for ch in text)
    separators = sum(ch in _PHONE_SEPARATORS for ch in text)
    return 10 <= digits <= 15 and separators >= 2


def _is_coordinate(match: re.Match[str]) -> bool:
    lat, lon = float(match.group(1)), float(match.group(2))
    return -90 <= lat <= 90 and -180 <= lon <= 180


# Order matters: an email address can contain digits that look like a phone number.
PATTERNS: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], bool]], ...] = (
    ("email", EMAIL, lambda m: True),
    ("coordinates", COORDINATES, _is_coordinate),
    ("phone", PHONE, lambda m: _is_phone(m.group(0))),
    ("street_address", STREET, lambda m: True),
)


def looks_like_pii(value: str) -> bool:
    """For the result masker: an email address or a phone number anywhere in the value."""
    if EMAIL.search(value):
        return True
    return any(_is_phone(m.group(0)) for m in PHONE.finditer(value))


@dataclass(frozen=True)
class ScanResult:
    text: str
    hits: dict[str, int] = field(default_factory=dict)  # pattern name -> count

    @property
    def found(self) -> bool:
        return bool(self.hits)

    def with_note(self) -> str:
        """The text with the note of spec 30, A4, when something was removed."""
        return f"{self.text}\n\n_{SCANNER_NOTE}_" if self.found else self.text


def scan_text(text: str) -> ScanResult:
    """The scan without a span. The trace and log redactor (spec 35) uses it, because a span
    inside the span exporter would write more spans."""
    hits: dict[str, int] = {}
    for name, pattern, accept in PATTERNS:

        def replace(match: re.Match[str], name: str = name, accept=accept) -> str:
            if not accept(match):
                return match.group(0)
            hits[name] = hits.get(name, 0) + 1
            return REMOVED

        text = pattern.sub(replace, text)
    return ScanResult(text=text, hits=hits)


class OutputScanner:
    def scan(self, text: str, target: str = "answer") -> ScanResult:
        result = scan_text(text)
        hits = result.hits
        with span("guard.scan", "guard", target=target) as sp:
            sp.set_attribute("hits_total", sum(hits.values()))
            for name, count in hits.items():
                sp.set_attribute(f"hits_{name}", count)
        return result
