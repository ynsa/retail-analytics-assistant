"""Storage of the evaluation runs (spec 23, B2.8).

data/eval/runs/<run_id>/run.json, results.jsonl, records/<case_id>.json, judge.jsonl,
                        human_labels.jsonl, report.md
data/eval/runs/index.jsonl     one summary line for each run
data/eval/call_stats.json      calls and bytes of each case in the last runs (for the plan)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import statistics
import subprocess
from pathlib import Path
from typing import Any

from opsfleet_agent.config import PROJECT_ROOT
from opsfleet_agent.evals.records import CaseRecord

STATS_KEEP = 5


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _append(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, default=str) + "\n")


def file_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def git_state(root: Path = PROJECT_ROOT) -> tuple[str, bool]:
    """The short commit hash and whether the work tree has changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return "nogit", False


def model_short(model: str) -> str:
    """gemini-3.5-flash-lite -> flash-lite."""
    return re.sub(r"^gemini-[\d.]+-", "", model) or model


def make_run_id(now: dt.datetime, set_name: str, model: str, commit: str) -> str:
    return f"{now.strftime('%Y%m%dT%H%MZ')}-{set_name}-{model_short(model)}-{commit}"


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    # --- run.json --------------------------------------------------------------------------------
    def create(self, run_id: str, meta: dict[str, Any]) -> Path:
        path = self.run_dir(run_id)
        if path.exists():
            raise FileExistsError(f"Run {run_id} exists already")
        (path / "records").mkdir(parents=True)
        self.save_meta(run_id, meta)
        return path

    def meta(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            raise FileNotFoundError(f"No run {run_id} in {self.root}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        path = self.run_dir(run_id) / "run.json"
        path.write_text(
            json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8"
        )

    # --- results and records ----------------------------------------------------------------------
    def results(self, run_id: str) -> list[dict[str, Any]]:
        """The last result line of each case (a rescore appends a newer line)."""
        latest: dict[str, dict[str, Any]] = {}
        for line in _jsonl(self.run_dir(run_id) / "results.jsonl"):
            latest[line["case_id"]] = line
        return list(latest.values())

    def append_result(self, run_id: str, result: dict[str, Any]) -> None:
        _append(self.run_dir(run_id) / "results.jsonl", result)

    def save_record(self, run_id: str, record: CaseRecord) -> None:
        path = self.run_dir(run_id) / "records" / f"{record.case_id}.json"
        path.write_text(
            json.dumps(record.to_json(), indent=1, default=str), encoding="utf-8"
        )

    def record(self, run_id: str, case_id: str) -> CaseRecord | None:
        path = self.run_dir(run_id) / "records" / f"{case_id}.json"
        if not path.exists():
            return None
        return CaseRecord.from_json(json.loads(path.read_text(encoding="utf-8")))

    def append_judge(self, run_id: str, item: dict[str, Any]) -> None:
        _append(self.run_dir(run_id) / "judge.jsonl", item)

    def judge_items(self, run_id: str) -> list[dict[str, Any]]:
        return _jsonl(self.run_dir(run_id) / "judge.jsonl")

    def append_label(self, run_id: str, item: dict[str, Any]) -> None:
        _append(self.run_dir(run_id) / "human_labels.jsonl", item)

    def labels(self, run_id: str) -> list[dict[str, Any]]:
        return _jsonl(self.run_dir(run_id) / "human_labels.jsonl")

    # --- index -----------------------------------------------------------------------------------
    def append_index(self, item: dict[str, Any]) -> None:
        _append(self.root / "index.jsonl", item)

    def index(self) -> list[dict[str, Any]]:
        return _jsonl(self.root / "index.jsonl")

    def last_run_of_set(self, set_name: str, before: str) -> str | None:
        runs = [
            i["run_id"]
            for i in self.index()
            if i.get("set") == set_name and i["run_id"] < before
        ]
        runs = [r for r in runs if self.run_dir(r).exists()]
        return max(runs) if runs else None

    def all_run_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "run.json").exists())

    # --- retention -------------------------------------------------------------------------------
    def cleanup(self, keep_days: int, now: dt.datetime) -> list[str]:
        """Delete run folders older than `keep_days`, except runs with `keep: true`."""
        removed = []
        for run_id in self.all_run_ids():
            meta = self.meta(run_id)
            if meta.get("keep"):
                continue
            created = dt.datetime.fromisoformat(meta["created_at"])
            if (now - created).days > keep_days:
                shutil.rmtree(self.run_dir(run_id))
                removed.append(run_id)
        return removed


class CallStats:
    """The calls, tokens and bytes of each case in the last runs. The plan uses the medians."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, list[float]]] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def median(self, case_id: str, field: str) -> float | None:
        values = self.data.get(case_id, {}).get(field)
        return statistics.median(values) if values else None

    def add(self, case_id: str, **values: float) -> None:
        item = self.data.setdefault(case_id, {})
        for name, value in values.items():
            item[name] = ([*item.get(name, []), value])[-STATS_KEEP:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1) + "\n", encoding="utf-8")
