"""Append-only JSONL metrics (one line per runtime attempt) and simple aggregates."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .config import data_dir
from .task import now_iso

FIELDS = ("task_id", "micro_spec_id", "project", "level", "risks", "runtime", "model", "attempt",
          "start", "duration", "success", "error_kind", "input_tokens", "output_tokens",
          "estimated_cost", "tests_passed", "tests_failed", "escalated_from", "role")


def default_path() -> Path:
    return data_dir() / "metrics.jsonl"


def record(path: Path | None = None, **fields) -> None:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown metric fields: {sorted(unknown)}")
    row = {"ts": now_iso(), **{k: fields.get(k) for k in FIELDS}}
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def load(path: Path | None = None) -> list[dict]:
    path = path or default_path()
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    """Per (level, runtime): attempts, success rate, avg duration, avg tokens, escalations."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        runtime = r.get("runtime") if r.get("role") != "review" else f"{r.get('runtime')}/review"
        groups[(r.get("level"), runtime)].append(r)
    out = []
    for (level, runtime), rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        toks = [(r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in rs
                if r.get("input_tokens") is not None]
        out.append({
            "level": level, "runtime": runtime, "attempts": len(rs),
            "success_rate": sum(1 for r in rs if r.get("success")) / len(rs),
            "avg_duration": sum(r.get("duration") or 0 for r in rs) / len(rs),
            "avg_tokens": (sum(toks) / len(toks)) if toks else None,
            "escalations_in": sum(1 for r in rs if r.get("escalated_from")),
        })
    return out
