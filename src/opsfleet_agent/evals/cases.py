"""Case sets of the evaluation (specs 21, 22 and 23).

Two kinds of cases:
- `AccuracyCase` (spec 21): questions with a reference SQL and a scoring rule.
- `ConversationCase` (spec 22 and the owner's session of spec 21 B2.2a): turns with code checks.

A set file has a header (`set`, `kind`) and a list of cases. A `selection` set (for example
`smoke.yaml`) names cases of other sets.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsfleet_agent.config import PROJECT_ROOT

SETS_DIR = PROJECT_ROOT / "evals" / "sets"

CaseStatus = Literal["draft", "ready", "needs_review"]
CompareMode = Literal["value", "rows", "rows_ordered", "top_k", "contains"]
ColumnKind = Literal[
    "text",
    "id",
    "date",
    "month",
    "quarter",
    "year",
    "money",
    "count",
    "ratio",
    "number",
]
Scorer = Literal[
    "execution_match",
    "answer_facts",
    "facts",
    "judge_answer",
    "report_structure",
    "judge_report",
    "file",
]
JUDGED_SCORERS = ("judge_answer", "judge_report")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnSpec(_Model):
    role: Literal["key", "value"]
    kind: ColumnKind = "text"


class Expected(_Model):
    mode: CompareMode
    columns: dict[str, ColumnSpec]
    k: int | None = None
    rel_tol: float | None = None  # default 0.5% (spec 21, B2.6)
    abs_tol: float | None = None  # default 0.01

    @model_validator(mode="after")
    def _check(self) -> Expected:
        if self.mode == "top_k" and not self.k:
            raise ValueError("mode top_k needs k")
        if not self.value_columns():
            raise ValueError("expected.columns needs at least one value column")
        return self

    def key_columns(self) -> list[str]:
        return [n for n, c in self.columns.items() if c.role == "key"]

    def value_columns(self) -> list[str]:
        return [n for n, c in self.columns.items() if c.role == "value"]


class FactSpec(_Model):
    """A figure from the snapshot that the answer text must contain (spec 21, B2.6)."""

    column: str
    where: dict[str, Any] | None = (
        None  # key column -> value; None for a one-row result
    )
    ref: str | None = None  # a named reference of a conversation case
    label: str | None = None


class RequiredSpec(_Model):
    """Words or figures for the `facts` scorer. `any` passes when one of the items is present."""

    any: list[str | float] = Field(default_factory=list)
    number: float | None = None
    tol: float = 0.01  # relative tolerance for `number`

    @model_validator(mode="before")
    @classmethod
    def _from_plain(cls, value: Any) -> Any:
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            return {"any": [value]}
        return value


class Reference(_Model):
    """A reference query. The SQL applies the brand scope with @all_brands and @brands."""

    sql: str
    expected: Expected
    user: str | None = None  # default: the user of the case
    params: dict[str, Any] = Field(default_factory=dict)  # more query parameters


class _CaseBase(_Model):
    id: str
    status: CaseStatus = "ready"
    user: str
    expected_calls: int = Field(ge=0)
    drafted_by: Literal["human", "llm", "claude"] = "claude"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    notes: str | None = None


class AccuracyCase(_CaseBase):
    kind: Literal["accuracy"] = "accuracy"
    turns: list[str] = Field(min_length=1)
    capability: str
    difficulty: Literal["easy", "medium", "hard"]
    query_types: list[str]
    period: dict[str, str] | None = None  # {from, to} or {relative: last_full_month}
    reference_sql: str | None = None
    expected: Expected | None = None
    params: dict[str, Any] = Field(
        default_factory=dict
    )  # more parameters, e.g. @focus_brands
    alt_references: list[Reference] = Field(
        default_factory=list
    )  # other correct result shapes
    facts: list[FactSpec] = Field(default_factory=list)
    required: list[RequiredSpec] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    accept_clarification: list[str] | None = (
        None  # words a clarifying question must contain
    )
    scoring: list[Scorer] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> AccuracyCase:
        needs_sql = {"execution_match", "answer_facts"} & set(self.scoring)
        if needs_sql and (self.reference_sql is None or self.expected is None):
            raise ValueError(
                f"{self.id}: {sorted(needs_sql)} needs reference_sql and expected"
            )
        if "facts" in self.scoring and not (self.required or self.forbidden):
            raise ValueError(
                f"{self.id}: the facts scorer needs `required` or `forbidden`"
            )
        return self

    @property
    def turn_texts(self) -> list[str]:
        return list(self.turns)

    def all_references(self) -> dict[str, Reference]:
        """The main reference and the alternatives. One match of any of them is enough."""
        if self.reference_sql is None or self.expected is None:
            return {}
        refs = {
            "main": Reference(
                sql=self.reference_sql, expected=self.expected, params=self.params
            )
        }
        for i, alt in enumerate(self.alt_references, start=1):
            refs[f"alt{i}"] = (
                alt if alt.params else alt.model_copy(update={"params": self.params})
            )
        return refs

    def judged_turns(self) -> int:
        return 1 if set(JUDGED_SCORERS) & set(self.scoring) else 0


CheckSpec = str | dict[str, Any]


class ConversationTurn(_Model):
    text: str
    checks: list[CheckSpec] = Field(default_factory=list)
    judge: list[str] = Field(default_factory=list)  # J- criteria of the safety rubric
    note: str | None = None


class ConversationCase(_CaseBase):
    kind: Literal["conversation"] = "conversation"
    category: str
    owasp: list[str] = Field(default_factory=list)
    fixture: (
        Literal["inject_row_text", "seed_reports", "seed_reports_injection"] | None
    ) = None
    fixture_args: dict[str, Any] = Field(default_factory=dict)
    turns: list[ConversationTurn] = Field(min_length=1)
    expected_behavior: str
    judge: list[str] = Field(default_factory=list)  # J- criteria for the last turn
    references: dict[str, Reference] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ConversationCase:
        if not any(t.checks or t.judge for t in self.turns) and not self.judge:
            raise ValueError(f"{self.id}: a conversation case needs at least one check")
        return self

    @property
    def turn_texts(self) -> list[str]:
        return [t.text for t in self.turns]

    def all_references(self) -> dict[str, Reference]:
        return dict(self.references)

    def judge_for_turn(self, index: int) -> list[str]:
        """The J- criteria of one turn. Case-level criteria apply to the last turn."""
        own = list(self.turns[index].judge)
        if index == len(self.turns) - 1:
            own += [j for j in self.judge if j not in own]
        return own

    def judged_turns(self) -> int:
        return sum(1 for i in range(len(self.turns)) if self.judge_for_turn(i))


Case = AccuracyCase | ConversationCase


class CaseSet(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    kind: Literal["accuracy", "conversation", "selection"]
    description: str = ""
    judge: bool = (
        True  # False: the set uses only the code checks (for example the smoke set)
    )
    cases: list[Case]
    path: Path
    sha256: str

    def by_id(self) -> dict[str, Case]:
        return {c.id: c for c in self.cases}

    def select(self, ids: list[str] | None) -> list[Case]:
        if not ids:
            return list(self.cases)
        index = self.by_id()
        unknown = [i for i in ids if i not in index]
        if unknown:
            raise KeyError(f"Unknown case IDs in set {self.name}: {', '.join(unknown)}")
        return [index[i] for i in ids]


def set_path(name_or_path: str | Path) -> Path:
    path = Path(name_or_path)
    if path.suffix in (".yaml", ".yml") and path.exists():
        return path
    return SETS_DIR / f"{name_or_path}.yaml"


def _parse_cases(kind: str, raw: list[dict[str, Any]], path: Path) -> list[Case]:
    model = AccuracyCase if kind == "accuracy" else ConversationCase
    cases = [model.model_validate(item) for item in raw]
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{path}: duplicate case IDs: {', '.join(dupes)}")
    return cases


def load_set(name_or_path: str | Path) -> CaseSet:
    """Load and check one set file. A `selection` set loads the named cases of other sets."""
    path = set_path(name_or_path)
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    kind = data.get("kind")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if kind == "selection":
        cases: list[Case] = []
        for set_name, ids in (data.get("include") or {}).items():
            cases += load_set(set_name).select(list(ids))
            digest = hashlib.sha256(
                (digest + load_set(set_name).sha256).encode("utf-8")
            ).hexdigest()
    elif kind in ("accuracy", "conversation"):
        cases = _parse_cases(kind, data.get("cases") or [], path)
    else:
        raise ValueError(f"{path}: unknown set kind {kind!r}")
    return CaseSet(
        name=data.get("set", path.stem),
        kind=kind,
        description=data.get("description", ""),
        judge=bool(data.get("judge", True)),
        cases=cases,
        path=path,
        sha256=digest,
    )


def scope_params(user: str, users: Any) -> dict[str, Any]:
    """The values of @all_brands and @brands for a user of `config/users.yaml`."""
    for entry in users.users:
        if entry.id == user:
            all_brands = entry.role.value == "ceo"
            return {
                "all_brands": all_brands,
                "brands": [] if all_brands else list(entry.brands),
            }
    raise KeyError(f"Unknown user {user!r} in config/users.yaml")
