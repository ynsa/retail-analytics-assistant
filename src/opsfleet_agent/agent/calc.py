"""Calculation placeholders and the figure check (spec 31, "Calculated figures").

The model never calculates a figure. For a derived figure in the text, it writes a placeholder such
as `{{calc:pct_change(r1.revenue@2026-10, r1.revenue@2026-09)}}`. Code computes the value from the
stored results. Then the figure check finds each number in the text. A number that does not come
from a result, a computed value, a date or the user's question is replaced.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from opsfleet_agent.agent.formatting import ColumnKind, ResultData, format_value

NOT_AVAILABLE = "[not available]"
OPERATIONS = ("diff", "pct_change", "ratio", "share", "sum", "avg")
SMALL_INTEGER_MAX = (
    12  # "top 5", "2 brands", "3 categories": counting words, not figures
)

PLACEHOLDER = re.compile(r"\{\{\s*calc\s*:\s*(\w+)\s*\((.*?)\)\s*\}\}", re.DOTALL)
_REF = re.compile(r"^\s*(r\d+)\.([A-Za-z_]\w*)\s*(?:@\s*(.+?))?\s*$")
_KEY_SPLIT = re.compile(r"\s*&\s*")

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE_SPANS = re.compile(
    rf"\b\d{{4}}-\d{{2}}(?:-\d{{2}})?\b"  # 2026-10-01, 2026-10
    rf"|\b\d{{1,2}}(?:\s*[–-]\s*\d{{1,2}})?\s+{_MONTH}(?:\s*[–-]\s*\d{{1,2}}\s+{_MONTH})?(?:,?\s+\d{{4}})?"
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:\s*[–-]\s*\d{{1,2}})?(?:,\s*\d{{4}})?"
    rf"|\b{_MONTH}\s+\d{{4}}\b"
    r"|\b(?:19|20)\d{2}\b"  # a year
)
_NUMBER = re.compile(
    r"(?<![\w.,$])(?P<sign>[-+−])?(?P<cur>\$)?(?P<sign2>[-−])?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suf>%|\s?(?:K|M|B|bn)(?![A-Za-z])|\s(?:thousand|million|billion)(?![A-Za-z]))?"
)
_SCALE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}


class CalcError(Exception):
    pass


@dataclass
class CalcOutcome:
    text: str
    computed: list[float] = field(default_factory=list)
    errors: int = 0


@dataclass
class FigureCheck:
    text: str
    checked: int = 0
    unverified: int = 0


# --- placeholders ---------------------------------------------------------------------------------


def _split_args(text: str) -> list[str]:
    return [a for a in (part.strip() for part in text.split(",")) if a]


def _key_cells(result: ResultData, row: list) -> list[str]:
    return [
        str(v).casefold()
        for v, kind in zip(row, result["kinds"], strict=True)
        if v is not None and kind in (ColumnKind.TEXT, ColumnKind.ID, ColumnKind.YEAR)
    ]


def _select_rows(result: ResultData, key: str | None) -> list[list]:
    if key is None:
        return result["rows"]
    parts = [
        p.strip().strip("'\"").casefold() for p in _KEY_SPLIT.split(key) if p.strip()
    ]

    def match(row: list, exact: bool) -> bool:
        cells = _key_cells(result, row)
        return all(
            any(c == p if exact else c.startswith(p) for c in cells) for p in parts
        )

    exact = [row for row in result["rows"] if match(row, True)]
    return exact or [row for row in result["rows"] if match(row, False)]


def _values(arg: str, results: dict[str, ResultData]) -> tuple[list[float], str]:
    """The values of one reference, and the column kind."""
    ref = _REF.match(arg)
    if not ref:
        raise CalcError(f"not a reference: {arg[:40]}")
    result = results.get(ref.group(1))
    if result is None:
        raise CalcError(f"unknown result {ref.group(1)}")
    column = ref.group(2).lower()
    names = [c.lower() for c in result["columns"]]
    if column not in names:
        raise CalcError(f"unknown column {ref.group(2)}")
    index = names.index(column)
    rows = _select_rows(result, ref.group(3))
    values = [row[index] for row in rows]
    if not values or any(
        not isinstance(v, (int, float)) or isinstance(v, bool) for v in values
    ):
        raise CalcError(f"no numeric value for {arg[:40]}")
    return [float(v) for v in values], result["kinds"][index]


def _one(arg: str, results: dict[str, ResultData]) -> tuple[float, str]:
    values, kind = _values(arg, results)
    if len(values) != 1:
        raise CalcError(f"{len(values)} rows match {arg[:40]}")
    return values[0], kind


def _divide(a: float, b: float) -> float:
    if b == 0:
        raise CalcError("division by zero")
    return a / b


def _pair(args: list[str], results: dict[str, ResultData]) -> tuple[float, float, str]:
    if len(args) != 2:
        raise CalcError("two references are necessary")
    (a, kind), (b, _) = _one(args[0], results), _one(args[1], results)
    return a, b, kind


def compute(
    op: str, args: list[str], results: dict[str, ResultData]
) -> tuple[float, str]:
    """The value and its display text."""
    if op == "diff":
        a, b, kind = _pair(args, results)
        value = a - b
        return value, format_value(value, kind, signed=True)
    if op == "pct_change":
        a, b, _ = _pair(args, results)
        value = _divide(a - b, abs(b)) * 100
        return value, format_value(value, ColumnKind.PERCENT, signed=True)
    if op == "ratio":
        a, b, _ = _pair(args, results)
        value = _divide(a, b)
        return value, format_value(value, ColumnKind.NUMBER)
    if op == "share":
        a, b, _ = _pair(args, results)
        value = _divide(a, b) * 100
        return value, format_value(value, ColumnKind.PERCENT)
    if op in ("sum", "avg"):
        if not args:
            raise CalcError("no references")
        values: list[float] = []
        kind = ColumnKind.NUMBER.value
        for i, arg in enumerate(args):
            found, arg_kind = _values(arg, results)
            values += found
            kind = arg_kind if i == 0 else kind
        value = sum(values) if op == "sum" else sum(values) / len(values)
        if op == "avg" and kind == ColumnKind.COUNT:
            kind = ColumnKind.NUMBER.value if not value.is_integer() else kind
        return value, format_value(value, kind)
    raise CalcError(f"unknown operation {op}")


def resolve_placeholders(
    text: str,
    results: Iterable[ResultData],
    on_error: Callable[[str], None] | None = None,
) -> CalcOutcome:
    """Replace each placeholder with its computed value, or with `[not available]`."""
    by_ref = {r["ref"]: r for r in results}
    outcome = CalcOutcome(text=text)

    def replace(match: re.Match[str]) -> str:
        op = match.group(1).lower()
        try:
            if op not in OPERATIONS:
                raise CalcError(f"unknown operation {op}")
            value, shown = compute(op, _split_args(match.group(2)), by_ref)
        except CalcError as exc:
            outcome.errors += 1
            if on_error:
                on_error(str(exc))
            return NOT_AVAILABLE
        outcome.computed.append(value)
        return shown

    outcome.text = PLACEHOLDER.sub(replace, text)
    return outcome


# --- figure check ---------------------------------------------------------------------------------


def numbers_in(text: str) -> list[float]:
    """All numbers in a text, for example the user's question or a note."""
    found = []
    for match in _NUMBER.finditer(text or ""):
        try:
            found.append(float(match.group("num").replace(",", "")))
        except ValueError:
            continue
    return found


def result_numbers(results: Iterable[ResultData]) -> list[float]:
    values: list[float] = []
    for result in results:
        values.append(float(result["row_count"]))
        for row in result["rows"]:
            for value, kind in zip(row, result["kinds"], strict=True):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append(float(value))
                    if kind == ColumnKind.RATE:
                        values.append(float(value) * 100)
    return values


def _decimals(num: str) -> int:
    return len(num.partition(".")[2])


def _traceable(num: str, scale: float, allowed: list[float]) -> bool:
    shown = float(num.replace(",", ""))
    places = _decimals(num)
    want = f"{shown:.{places}f}"
    return any(f"{abs(v) / scale:.{places}f}" == want for v in allowed)


def check_figures(
    text: str, allowed: Iterable[float], removed_text: str
) -> FigureCheck:
    """Replace each number that no allowed value explains. Dates and years are not figures."""
    allowed_list = [abs(v) for v in allowed]
    protected = [m.span() for m in _DATE_SPANS.finditer(text)]
    check = FigureCheck(text=text)

    def inside_date(start: int, end: int) -> bool:
        return any(a <= start and end <= b for a, b in protected)

    def replace(match: re.Match[str]) -> str:
        num, suffix = match.group("num"), (match.group("suf") or "").strip()
        if inside_date(match.start("num"), match.end("num")):
            return match.group(0)
        if not suffix and match.end() < len(text) and text[match.end()].isalpha():
            return match.group(0)  # "1st", "3x": not a figure
        plain = (
            not suffix and not match.group("cur") and "." not in num and "," not in num
        )
        if plain and int(num) <= SMALL_INTEGER_MAX:
            return match.group(0)
        check.checked += 1
        scale = 1.0 if suffix in ("", "%") else _SCALE[suffix.lower()]
        if _traceable(num, scale, allowed_list):
            return match.group(0)
        check.unverified += 1
        return removed_text

    check.text = _NUMBER.sub(replace, text)
    return check
