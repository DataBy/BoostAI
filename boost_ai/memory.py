"""Long-term engineering memory behind a small backend interface.

Hierarchy:  Project → Task → MicroSpec → Implementation

V1 ships LocalMemoryBackend only (Markdown + YAML front matter under
.boost-ai/state/memory/). A LogseqMemoryBackend can be added later by
implementing MemoryBackend and registering it in `backend_from_config`; nothing
else in the harness changes. See docs/architecture-v1.md § Memory.

Memory is optional: failures here must never fail a task (the orchestrator
catches and reports them).
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .redact import redact


@dataclass
class MicroSpecRef:
    id: str
    title: str


@dataclass
class ImplementationRecord:
    project: str
    task_id: str
    task_title: str
    summary: str
    timestamp: str
    micro_specs: list[MicroSpecRef] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    architecture_notes: list[str] = field(default_factory=list)
    branch: str | None = None
    commit: str | None = None
    level: str | None = None
    runtime: str | None = None
    verification: str | None = None
    status: str = "implemented"


@dataclass
class MemoryHit:
    ref: str       # backend-specific locator (file path, page name, ...)
    title: str
    excerpt: str
    score: float


class MemoryBackend(ABC):
    @abstractmethod
    async def write_implementation(self, record: ImplementationRecord) -> str:
        """Persist the record; return a locator (path, page name) for display."""

    @abstractmethod
    async def search_context(self, query: str, limit: int = 5) -> list[MemoryHit]:
        """Return a few short, relevant excerpts — never whole documents."""


class LocalMemoryBackend(MemoryBackend):
    """<root>/<project>/<task_id>.md — one page per task, front matter = structured data.

    Body uses [[wiki links]] so pages map 1:1 onto Logseq pages later.
    """

    def __init__(self, root: Path):
        self.root = root

    async def write_implementation(self, record: ImplementationRecord) -> str:
        return await asyncio.to_thread(self._write, record)

    async def search_context(self, query: str, limit: int = 5) -> list[MemoryHit]:
        return await asyncio.to_thread(self._search, query, limit)

    def _write(self, record: ImplementationRecord) -> str:
        # Project → Task → MicroSpec: one page per micro-spec (one per conversation turn that changed code).
        name = record.micro_specs[0].id if record.micro_specs else record.task_id
        path = self.root / _slug(record.project) / _slug(record.task_id) / f"{_slug(name)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = asdict(record)
        path.write_text(redact("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
                               + "---\n\n" + render_markdown(record)))
        return str(path)

    def _search(self, query: str, limit: int) -> list[MemoryHit]:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        if not terms or not self.root.is_dir():
            return []
        hits = []
        for path in self.root.rglob("*.md"):
            text = path.read_text(errors="replace")
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if not score:
                continue
            body = text.split("\n---\n", 1)[-1]
            title = next((ln.lstrip("# ") for ln in body.splitlines() if ln.startswith("# ")), path.stem)
            line = next((ln for ln in body.splitlines() if any(t in ln.lower() for t in terms)), "")
            hits.append(MemoryHit(str(path), title, line.strip()[:240], float(score)))
        return sorted(hits, key=lambda h: -h.score)[:limit]


def render_markdown(r: ImplementationRecord) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- none"

    specs = "\n".join(f"- [[{m.id}]] {m.title}" for m in r.micro_specs) or "- none"
    return (
        f"# [[{r.task_id}]] {r.task_title}\n\n"
        f"Project: [[{r.project}]]\n"
        f"Status: {r.status}\n"
        f"Level: {r.level or '—'} · Runtime: {r.runtime or '—'}\n"
        f"Branch: {r.branch or '—'}\n"
        f"Commit: {r.commit or 'not committed'}\n"
        f"Timestamp: {r.timestamp}\n\n"
        f"## Micro-specs\n{specs}\n\n"
        f"## Implementation summary\n{r.summary or '—'}\n\n"
        f"## Decisions\n{bullets(r.decisions)}\n\n"
        f"## Files changed\n{bullets(r.files_changed)}\n\n"
        f"## Problems discovered\n{bullets(r.problems)}\n\n"
        f"## Architectural reasoning\n{bullets(r.architecture_notes)}\n\n"
        f"## Verification\n```\n{r.verification or 'not run'}\n```\n"
    )


def backend_from_config(cfg: dict, project_root: Path) -> MemoryBackend:
    kind = (cfg.get("memory") or {}).get("backend", "local")
    # Future: if kind == "logseq": return LogseqMemoryBackend(graph_path=...)
    if kind != "local":
        raise ValueError(f"Unknown memory backend '{kind}' (V1 supports: local)")
    return LocalMemoryBackend(project_root / ".boost-ai" / "state" / "memory")


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text).strip("-") or "unnamed"
