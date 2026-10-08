"""Task model and its JSON persistence under .boost-ai/state/."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .redact import redact


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    text: str
    level: int = 2
    risks: list[str] = field(default_factory=list)
    status: str = "NEW"  # NEW TRIAGED INTERVIEWING PLANNING RUNNING VERIFYING REVIEW NEEDS_YOU DONE FAILED STOPPED
    runtime: str | None = None
    runtimes_tried: list[str] = field(default_factory=list)
    attempts: int = 0
    workdir: str | None = None
    branch: str | None = None  # the user's branch; a worktree's approved commits land on it
    worktree: bool = False
    tests: str | None = None  # e.g. "18 / 21"
    commit: str | None = None
    attachments: list[str] = field(default_factory=list)  # copies under .boost-ai/state/attachments/
    # A task is a conversation: every user message is a turn handled by the same agent session.
    turns: int = 0
    messages: list[str] = field(default_factory=list)       # user messages, in order
    sessions: dict[str, str] = field(default_factory=dict)  # runtime → resumable session id
    pending: list[str] = field(default_factory=list)        # files the agent changed, not committed yet
    unlanded: list[str] = field(default_factory=list)       # worktree commits not yet on the user's branch
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def title(self) -> str:
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        if len(first) <= 80:
            return first
        return first[:80].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
RICH_EXTENSIONS = IMAGE_EXTENSIONS + (".pdf",)   # need a multimodal model to be read natively
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_CANDIDATE = re.compile(r"""'([^'\n]+)'|"([^"\n]+)"|(file://\S+)|((?:\\ |\S)+)""")


def find_attachments(text: str, root: Path) -> list[Path]:
    """Existing files referenced in a message: dropped/pasted paths, quoted paths, file:// URIs,
    ~ and project-relative paths. Any type (PDF, CSV, logs, code, images…), up to 50 MB.
    Files git already tracks in the project are skipped: the agent can read them in place."""
    from urllib.parse import unquote

    found: list[Path] = []
    for m in _CANDIDATE.finditer(text):
        raw = next(g for g in m.groups() if g)
        raw = unquote(raw.removeprefix("file://")) if raw.startswith("file://") else raw
        raw = raw.rstrip(".,;:)").replace("\\ ", " ")
        if not raw or len(raw) > 4096:
            continue
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else root / path
        try:
            if not path.is_file() or path.stat().st_size > MAX_ATTACHMENT_BYTES:
                continue
        except OSError:
            continue
        resolved = path.resolve()
        if resolved in found or _tracked(resolved, root):
            continue
        found.append(resolved)
    return found


def find_images(text: str, root: Path) -> list[Path]:
    return [p for p in find_attachments(text, root) if p.suffix.lower() in IMAGE_EXTENSIONS]


def _tracked(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root.resolve())
    except ValueError:
        return False  # outside the project
    if rel.parts and rel.parts[0] == ".boost-ai":
        return True   # harness files are never attachments
    import subprocess
    proc = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(rel)], cwd=root,
                          capture_output=True, text=True)
    return proc.returncode == 0


class TaskStore:
    """One JSON file per task, plus current.json describing what is running now
    (read by `boost-ai status` and, later, the desktop indicator)."""

    def __init__(self, project_root: Path):
        self.dir = project_root / ".boost-ai" / "state" / "tasks"
        self.current_path = project_root / ".boost-ai" / "state" / "current.json"

    def next_id(self) -> str:
        nums = [int(m.group(1)) for p in self._files() if (m := re.fullmatch(r"T-(\d+)", p.stem))]
        return f"T-{max(nums, default=0) + 1:04d}"

    def new(self, text: str) -> Task:
        task = Task(id=self.next_id(), text=text)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        task.updated_at = now_iso()
        _atomic_write(self.dir / f"{task.id}.json", json.dumps(asdict(task), indent=2))

    def load(self, task_id: str) -> Task:
        data = json.loads((self.dir / f"{task_id}.json").read_text())
        known = Task.__dataclass_fields__
        return Task(**{k: v for k, v in data.items() if k in known})  # tolerate older/newer fields

    def all(self) -> list[Task]:
        """Every task, newest first."""
        tasks = []
        for path in self._files():
            try:
                tasks.append(self.load(path.stem))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return sorted(tasks, key=lambda t: t.id, reverse=True)

    def transcript_path(self, task_id: str) -> Path:
        return self.dir.parent / "logs" / task_id / "transcript.jsonl"

    def record(self, task_id: str, kind: str, text: str) -> None:
        """Append to the task's local operational transcript (secrets redacted)."""
        path = self.transcript_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"at": now_iso(), "kind": kind, "text": redact(text)}) + "\n")

    def transcript(self, task_id: str) -> list[dict]:
        try:
            lines = self.transcript_path(task_id).read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def set_current(self, data: dict) -> None:
        self.current_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.current_path, json.dumps({**data, "updated_at": now_iso()}, indent=2))

    def current(self) -> dict | None:
        try:
            return json.loads(self.current_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _files(self) -> list[Path]:
        return list(self.dir.glob("T-*.json")) if self.dir.is_dir() else []


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
