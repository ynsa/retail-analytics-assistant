"""Brand-scope rewrite (spec 30, B2.3).

Each table reference becomes a code-made subquery from `scope_templates.sql`. The subquery keeps the
alias of the reference. A reference without an alias gets the table name as alias, so
`orders.status` still works. The brand names go in as the array parameter `@scope_brands`, so a
name with an apostrophe ("Levi's") needs no quoting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from google.cloud import bigquery
from sqlglot import exp

from opsfleet_agent.config import Role
from opsfleet_agent.identity import UserContext

SCOPE_PARAM = "scope_brands"
TEMPLATE_FILE = Path(__file__).with_name("scope_templates.sql")
_TEMPLATE_HEADER = re.compile(r"^-- template: (\w+)\.(\w+)\s*$", re.MULTILINE)
_SIMPLE_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})


class ScopeError(Exception):
    """The rewrite cannot place a scope subquery safely. The guard returns `unverifiable`."""


@dataclass(frozen=True)
class ScopeTemplates:
    by_key: dict[tuple[Role, str], exp.Select]
    bodies: frozenset[
        str
    ]  # the SQL text of each template, to find them again after the rewrite

    def get(self, role: Role, table: str) -> exp.Select | None:
        return self.by_key.get((role, table))

    def has(self, role: Role, table: str) -> bool:
        return (role, table) in self.by_key


def load_templates(dataset: str, path: Path = TEMPLATE_FILE) -> ScopeTemplates:
    text = path.read_text(encoding="utf-8")
    headers = list(_TEMPLATE_HEADER.finditer(text))
    by_key: dict[tuple[Role, str], exp.Select] = {}
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[match.end() : end].replace("{dataset}", dataset)
        tree = sqlglot.parse_one(body, read="bigquery")
        if not isinstance(tree, exp.Select):
            raise TypeError(f"scope template {match.group(0)!r} is not a SELECT")
        by_key[(Role(match.group(1)), match.group(2))] = tree
    bodies = frozenset(t.sql(dialect="bigquery") for t in by_key.values())
    return ScopeTemplates(by_key=by_key, bodies=bodies)


def is_base_table(table: exp.Table) -> bool:
    """After the guard's normalization, only real tables have a dataset part (CTE names do not)."""
    return bool(table.args.get("db"))


def scope_params(user: UserContext) -> list[bigquery.ArrayQueryParameter]:
    if user.role is Role.CEO:
        return []
    return [bigquery.ArrayQueryParameter(SCOPE_PARAM, "STRING", list(user.brands))]


def rewrite_scope(
    tree: exp.Expression, user: UserContext, templates: ScopeTemplates
) -> tuple[exp.Expression, list[bigquery.ArrayQueryParameter]]:
    """Replace each base table with its scope subquery. Works on a copy of `tree`."""
    tree = tree.copy()
    targets = [
        t
        for t in tree.find_all(exp.Table)
        if is_base_table(t) and templates.has(user.role, t.name)
    ]
    for table in targets:
        extra = {k for k, v in table.args.items() if v and k not in _SIMPLE_TABLE_ARGS}
        alias_node = table.args.get("alias")
        if extra or (alias_node is not None and alias_node.columns):
            raise ScopeError(f"table reference with {sorted(extra) or 'column alias'}")
        template = templates.get(user.role, table.name)
        assert template is not None
        alias = table.alias or table.name
        table.replace(
            exp.Subquery(
                this=template.copy(),
                alias=exp.TableAlias(this=exp.to_identifier(alias)),
            )
        )
    sql = tree.sql(dialect="bigquery")
    params = scope_params(user) if f"@{SCOPE_PARAM}" in sql else []
    return tree, params


def verify_scope(sql: str, user: UserContext, templates: ScopeTemplates) -> bool:
    """Parse the rewritten SQL again. Each base table with a template for the role must be inside
    a code-made scope subquery (spec 30, B2.2 step 18)."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return False
    for table in tree.find_all(exp.Table):
        if not is_base_table(table) or not templates.has(user.role, table.name):
            continue
        inside = any(
            isinstance(node, exp.Subquery)
            and node.this is not None
            and node.this.sql(dialect="bigquery") in templates.bodies
            for node in _ancestors(table)
        )
        if not inside:
            return False
    return True


def _ancestors(node: exp.Expression):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent
