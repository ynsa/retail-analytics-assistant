"""Text helpers for reports: clean model text, normalize names, count words (spec 32, B2.4)."""

from __future__ import annotations

import re
from typing import Any

from opsfleet_agent.guard.scanner import OutputScanner

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(?:https?|ftp|mailto):[^>]*>", re.IGNORECASE)
_URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z!][^>]*>")
_MARKERS = re.compile(r"\[\[|\]\]")
_SPACES = re.compile(r"[ \t]+")
_WORD = re.compile(r"\S+")


def clean_model_text(text: str) -> str:
    """Model text for the report and the files: no links, no images, no raw HTML (B1)."""
    text = _IMAGE.sub("", text or "")
    text = _LINK.sub(r"\1", text)
    text = _REF_LINK.sub(r"\1", text)
    text = _AUTOLINK.sub("", text)
    text = _TAG.sub("", text)
    text = _URL.sub("", text)
    text = _MARKERS.sub("", text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def one_line(text: str) -> str:
    return " ".join(clean_model_text(text).split())


def normalize_value(text: str) -> str:
    """`value_norm` of the report entities: lower case, trimmed, single spaces (spec 32, B3)."""
    return " ".join((text or "").casefold().split())


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


_SEPARATOR = "\n\x00\n"  # no scanner pattern crosses a NUL character


def scan_strings(value: Any, scanner: OutputScanner, target: str) -> tuple[Any, int]:
    """Run the output scanner on every string in a JSON-like value, in one scan (one span).
    Returns the clean value and the number of hits."""
    strings: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, list):
            for i in item:
                collect(i)
        elif isinstance(item, dict):
            for v in item.values():
                collect(v)

    collect(value)
    if not strings:
        return value, 0
    result = scanner.scan(_SEPARATOR.join(strings), target=target)
    clean = iter(result.text.split(_SEPARATOR))

    def rebuild(item: Any) -> Any:
        if isinstance(item, str):
            return next(clean)
        if isinstance(item, list):
            return [rebuild(i) for i in item]
        if isinstance(item, dict):
            return {k: rebuild(v) for k, v in item.items()}
        return item

    return rebuild(value), sum(result.hits.values())
