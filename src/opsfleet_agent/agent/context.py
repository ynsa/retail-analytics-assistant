"""Prompt files and prompt layers (spec 31, B2.7 and B3).

`PromptLibrary` reads the files in `agent/prompts/` once at the start. It reads `config/persona.md`
at each turn, so a change of the tone needs no restart. No prompt text is in the Python code.

The system instruction has four layers, highest priority first:

1. `safety.md`: the safety rules.
2. The persona, under the heading "Tone guidance" (at most 2,000 characters).
3. The user format: the pinned format request.
4. The task prompt of the node.

The contents of a request hold the context, the data blocks and the user message, never rules.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, Template

PROMPT_DIR = Path(__file__).parent / "prompts"
# The prompts whose text must never appear in an answer (the prompt-leak check, B2.7 #6).
PROTECTED_PROMPTS = (
    "safety",
    "route",
    "write_sql",
    "write_answer",
    "schema_answer",
    "plan_report",
    "write_report",
    "fix_figures",
)
LEAK_RUN_WORDS = 12
# A marker in the first line of each protected prompt (an HTML comment that the model sees). An
# answer with this string shows a prompt leak. The evaluation check H-SEC looks for it (spec 22).
PROMPT_CANARY = "canary-7f3a9c"
_WORD = re.compile(r"[a-z0-9']+")
_DEFINITION = re.compile(r"^###\s+`?(\w+)`?\s*$")
_FIELD = re.compile(r"^-\s+\*\*(\w+):\*\*\s*(.+)$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Persona:
    text: str
    cut: bool  # the file had more characters than allowed
    missing: bool = False


@dataclass(frozen=True)
class Definition:
    key: str
    meaning: str
    footer: str  # the short sentence for the footer
    sql: str


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def parse_definitions(text: str) -> dict[str, Definition]:
    """`### key` headings, each with `- **Meaning:**`, `- **Footer:**` and `- **SQL:**` lines."""
    found: dict[str, Definition] = {}
    key: str | None = None
    fields: dict[str, str] = {}

    def close() -> None:
        if key:
            found[key] = Definition(
                key=key,
                meaning=fields.get("meaning", ""),
                footer=fields.get("footer", ""),
                sql=fields.get("sql", ""),
            )

    for line in text.splitlines():
        heading = _DEFINITION.match(line.strip())
        if heading:
            close()
            key, fields = heading.group(1), {}
            continue
        item = _FIELD.match(line.strip())
        if key and item:
            fields[item.group(1).lower()] = item.group(2).strip()
    close()
    return found


class PromptLibrary:
    def __init__(
        self,
        prompt_dir: Path = PROMPT_DIR,
        persona_path: Path | None = None,
        persona_max_chars: int = 2000,
    ) -> None:
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._texts = {
            p.stem: p.read_text(encoding="utf-8")
            for p in sorted(prompt_dir.glob("*.md"))
        }
        self._templates = {
            name: self._env.from_string(t) for name, t in self._texts.items()
        }
        replies_file = prompt_dir / "replies.yaml"
        self._replies_text = replies_file.read_text(encoding="utf-8")
        self.replies: dict[str, Any] = yaml.safe_load(self._replies_text) or {}
        self.definitions = parse_definitions(self._texts.get("definitions", ""))
        self.persona_path = persona_path
        self.persona_max_chars = persona_max_chars
        self._protected = [
            _words(self._texts[name])
            for name in PROTECTED_PROMPTS
            if name in self._texts
        ]

    # --- templates -------------------------------------------------------------------------------
    def get(self, name: str) -> Template:
        return self._templates[name]

    def text(self, name: str) -> str:
        return self._texts[name]

    def render(self, template: str, /, **values: Any) -> str:
        return self._templates[template].render(**values).strip()

    def reply_value(self, path: str) -> Any:
        """A raw value from `replies.yaml`, for example a list of texts."""
        node: Any = self.replies
        for part in path.split("."):
            node = node[part]
        return node

    def render_string(self, text: str, /, **values: Any) -> str:
        return self._env.from_string(text).render(**values).strip()

    def reply(self, path: str, /, **values: Any) -> str:
        """A prepared text from `replies.yaml`, for example `reply("unsafe.prompt_request")`."""
        return self.render_string(str(self.reply_value(path)), **values)

    def version(self, *names: str) -> str:
        """The first 8 characters of the SHA-256 of the prompt files used (span attribute)."""
        digest = hashlib.sha256()
        for name in names:
            digest.update(self._texts.get(name, self._replies_text).encode("utf-8"))
        return digest.hexdigest()[:8]

    # --- persona ---------------------------------------------------------------------------------
    def read_persona(self) -> Persona:
        """Read at each turn: a change of the file applies to the next answer."""
        if self.persona_path is None or not self.persona_path.exists():
            return Persona(text="", cut=False, missing=True)
        text = _COMMENT.sub("", self.persona_path.read_text(encoding="utf-8")).strip()
        if len(text) > self.persona_max_chars:
            return Persona(text=text[: self.persona_max_chars], cut=True)
        return Persona(text=text, cut=False)

    # --- layers ----------------------------------------------------------------------------------
    def system(
        self, task: str, /, persona: str = "", user_format: str = "", **values: Any
    ) -> str:
        """The system instruction: safety > persona > user format > task."""
        parts = [self.render("safety")]
        if persona:
            parts.append(f"## Tone guidance\n\n{persona}")
        if user_format:
            parts.append(f"## User format\n\n{user_format}")
        parts.append(self.render(task, **values))
        return "\n\n".join(parts)

    def format_text(self, format_request: str | None) -> str:
        if not format_request:
            return ""
        return str(self.replies.get("formats", {}).get(format_request, ""))

    # --- prompt-leak check -----------------------------------------------------------------------
    def leaks_prompt(self, answer: str, run: int = LEAK_RUN_WORDS) -> bool:
        """True when the answer has a run of `run` or more words of a protected prompt."""
        words = _words(answer)
        if len(words) < run:
            return False
        grams = {tuple(words[i : i + run]) for i in range(len(words) - run + 1)}
        for source in self._protected:
            for i in range(len(source) - run + 1):
                if tuple(source[i : i + run]) in grams:
                    return True
        return False
