"""Find figures in an answer text: "37,218", "$37.2K", "37.2 thousand", "1.8M", "25%" (spec 21, B2.6)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_SCALE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

_NUMBER = re.compile(
    r"""
    (?P<sign>[-−])?\s?
    (?P<cur>[$€£])?\s?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    (?:\s?(?P<scale>thousand|million|billion|mn|bn|[kKmMbB])(?![A-Za-z]))?
    (?:\s?(?P<pct>%|percent\b|per\ cent\b))?
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Figure:
    value: float
    percent: bool
    money: bool
    raw: str
    digits: int  # digits before the decimal point, without the scale


def extract_figures(text: str) -> list[Figure]:
    figures: list[Figure] = []
    for m in _NUMBER.finditer(text):
        raw_num = m.group("num").replace(",", "")
        try:
            value = float(raw_num)
        except ValueError:
            continue
        scale = m.group("scale")
        if scale:
            value *= _SCALE[scale.lower()]
        if m.group("sign"):
            value = -value
        int_part = raw_num.split(".")[0]
        figures.append(
            Figure(
                value=value,
                percent=bool(m.group("pct")),
                money=bool(m.group("cur")),
                raw=m.group(0).strip(),
                digits=len(int_part.lstrip("0")) if scale is None else 99,
            )
        )
    return figures


def close(a: float, b: float, rel: float = 0.01, abs_tol: float = 0.0) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_tol)


def contains_figure(
    text: str, value: float, *, kind: str = "number", rel: float = 0.01
) -> bool:
    """True when the text contains the value within `rel`. A ratio also matches as a percent."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    for fig in extract_figures(text):
        if kind == "ratio":
            if fig.percent and close(fig.value, value * 100, rel, abs_tol=0.051):
                return True
            if not fig.percent and close(fig.value, value, rel, abs_tol=0.0005):
                return True
            continue
        if kind == "count":
            if close(fig.value, value, rel=rel, abs_tol=0.5):
                return True
            continue
        if close(fig.value, value, rel=rel, abs_tol=0.005):
            return True
    return False


def is_material_figure(fig: Figure) -> bool:
    """A figure that can show business data: 3 or more digits, money or a percent (spec 22, B6)."""
    return fig.percent or fig.money or fig.digits >= 3


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in (s.strip() for s in parts) if p]
