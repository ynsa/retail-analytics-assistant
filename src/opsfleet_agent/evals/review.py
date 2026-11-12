"""Blind human labels for judged items (spec 23, A2.6; spec 20 BP-E5).

The person sees the question, the answer and the criteria, but not the verdict of the judge.
The sample takes judge passes and judge fails in turn, so both rates get labels.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable
from typing import Any

from opsfleet_agent.evals.storage import RunStore


def pick_items(
    items: list[dict[str, Any]], labelled: set[tuple[str, str]], n: int
) -> list[dict]:
    """Up to n unlabelled items, judge passes and fails in turn, in a stable random order."""
    fresh = [
        i
        for i in items
        if not i.get("error") and (i["case_id"], i["key"]) not in labelled
    ]

    def order(item: dict[str, Any]) -> str:
        return hashlib.sha256(f"{item['case_id']}/{item['key']}".encode()).hexdigest()

    passes = sorted((i for i in fresh if i["passed"]), key=order)
    fails = sorted((i for i in fresh if not i["passed"]), key=order)
    out: list[dict[str, Any]] = []
    while len(out) < n and (passes or fails):
        for bucket in (fails, passes):
            if bucket and len(out) < n:
                out.append(bucket.pop(0))
    return out


def blind_view(item: dict[str, Any]) -> str:
    criteria = "\n".join(f"  - {c['id']}" for c in item.get("criteria", []))
    return (
        f"Case {item['case_id']} ({item['key']}, rubric {item['rubric']})\n"
        f"Question: {item['question']}\n"
        f"Answer:\n{item['answer']}\n"
        f"Criteria (see evals/rubrics/{item['rubric']}.md):\n{criteria}\n"
    )


def review(
    store: RunStore,
    run_id: str,
    n: int,
    ask: Callable[[str], str],
    show: Callable[[str], None],
    reviewer: str = "owner",
) -> int:
    """Ask the person for pass or fail on up to n items. Returns the number of new labels."""
    items = store.judge_items(run_id)
    labelled = {(lb["case_id"], lb["key"]) for lb in store.labels(run_id)}
    count = 0
    for item in pick_items(items, labelled, n):
        show(blind_view(item))
        answer = ask(
            "Does the answer pass all gating criteria? [p]ass / [f]ail / [s]kip / [q]uit: "
        )
        answer = answer.strip().lower()[:1]
        if answer == "q":
            break
        if answer not in ("p", "f"):
            continue
        store.append_label(
            run_id,
            {
                "case_id": item["case_id"],
                "key": item["key"],
                "rubric": item["rubric"],
                "rubric_sha256": item.get("rubric_sha256"),
                "human_pass": answer == "p",
                "reviewer": reviewer,
                "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            },
        )
        count += 1
    return count
