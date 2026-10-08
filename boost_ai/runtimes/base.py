"""Common runtime interface. Nothing outside runtimes/ knows provider details."""

from __future__ import annotations

import enum
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import data_dir
from ..redact import redact
from ..task import now_iso


class Usage(enum.StrEnum):
    AVAILABLE = "AVAILABLE"
    NORMAL = "NORMAL"
    CONSERVE = "CONSERVE"
    CRITICAL = "CRITICAL"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class ErrorKind(enum.StrEnum):
    RATE_LIMIT = "rate_limit"      # quota / usage limit / 429
    AUTH = "auth"                  # not logged in, missing key
    UNAVAILABLE = "unavailable"    # binary missing, network down
    TIMEOUT = "timeout"
    CRASH = "crash"                # anything else


# Structured final answer every runtime is asked for. Feeds the commit message
# and the memory record without an extra LLM call.
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "commit_message": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
        "architecture_notes": {"type": "array", "items": {"type": "string"}},
        "blocker": {"type": "string"},
        "blocker_options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "commit_message", "decisions", "problems",
                 "architecture_notes", "blocker", "blocker_options"],
    "additionalProperties": False,
}


# Read-only planning run before any change: the user approves, edits or stops the plan.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "answer", "blocked"]},
        "plan": {"type": "string"},
        "tasks": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "blocker": {"type": "string"},
        "blocker_options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "plan", "tasks", "skills", "blocker", "blocker_options"],
    "additionalProperties": False,
}


# Read-only interview (/interview): rounds of questions until a shared understanding, then a spec.
INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["questions", "done"]},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "recommended": {"type": "string"}},
            "required": ["question", "recommended"],
            "additionalProperties": False,
        }},
        "title": {"type": "string"},
        "spec": {"type": "string"},
    },
    "required": ["status", "questions", "title", "spec"],
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "changes_requested"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "file": {"type": "string"},
                "issue": {"type": "string"},
            },
            "required": ["severity", "file", "issue"],
            "additionalProperties": False,
        }},
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}


@dataclass
class ExecutionRequest:
    prompt: str
    cwd: Path
    log_path: Path
    model: str | None = None
    images: list[Path] = field(default_factory=list)
    timeout: int = 1800
    env: dict[str, str] | None = None
    approval_dir: Path | None = None   # where the safety hook asks the user for approval
    read_only: bool = False            # review mode: the runtime must not modify files
    read_dirs: list[Path] = field(default_factory=list)  # extra directories it may read (global skills)
    schema: dict | None = None         # structured final answer; defaults to RESULT_SCHEMA
    resume_session: str | None = None  # continue this runtime session (prompt is only the delta)
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # name → {command, args}, e.g. graft


@dataclass
class UsageReport:
    """Usage evidence reported by the provider itself (exact, not estimated)."""
    state: Usage
    percent: float | None = None       # 0-100 of the binding window
    window: str | None = None          # e.g. "5h", "7d"


def usage_from_utilization(utilization: float | None, rejected: bool = False) -> Usage:
    if rejected or (utilization is not None and utilization >= 1.0):
        return Usage.EXHAUSTED
    if utilization is None:
        return Usage.AVAILABLE
    if utilization >= 0.9:
        return Usage.CRITICAL
    if utilization >= 0.75:
        return Usage.CONSERVE
    if utilization >= 0.5:
        return Usage.NORMAL
    return Usage.AVAILABLE


@dataclass
class ExecutionResult:
    ok: bool
    final: dict | None = None          # parsed structured answer (RESULT_SCHEMA or request.schema)
    text: str = ""                     # last agent message, raw
    input_tokens: int | None = None
    output_tokens: int | None = None
    exit_code: int | None = None
    error_kind: ErrorKind | None = None
    error: str = ""                    # human-readable reason
    last_output: str = ""              # tail of relevant output for error display
    duration: float = 0.0
    usage: UsageReport | None = None   # provider-reported quota state, when available
    model: str | None = None           # the concrete model that ran, when the runtime reports it
    session_id: str | None = None      # resumable session/thread id
    cached_tokens: int | None = None   # part of input_tokens served from the provider cache
    cost: float | None = None          # provider-reported cost (USD), when available


ProgressFn = Callable[[str], None]


class Runtime(ABC):
    name: str = "runtime"
    label: str = "Runtime"
    vision: bool = True   # can read images/PDFs natively (False for text-only models)

    @abstractmethod
    def installed(self) -> bool: ...

    @abstractmethod
    async def execute(self, request: ExecutionRequest, progress: ProgressFn) -> ExecutionResult: ...


class UsageBook:
    """Last known usage state per runtime, from evidence only.

    Exact when the provider reports it (e.g. Claude's utilization windows);
    otherwise inferred from errors/successes and labelled as an estimate."""

    EXHAUSTED_TTL = 2 * 3600

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "usage.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, runtime: str) -> tuple[Usage, bool]:
        """Return (state, is_estimate)."""
        entry = self._read().get(runtime)
        if not entry:
            return Usage.UNKNOWN, False
        state = Usage(entry["state"])
        # Provider windows reset; stale evidence of exhaustion stops blocking routing.
        if state is Usage.EXHAUSTED and _age_seconds(entry.get("at")) > self.EXHAUSTED_TTL:
            return Usage.UNKNOWN, False
        return state, entry.get("estimate", True)

    def record(self, runtime: str, state: Usage, estimate: bool = True, note: str = "",
               percent: float | None = None, window: str | None = None) -> None:
        data = self._read()
        data[runtime] = {"state": state.value, "estimate": estimate, "note": note, "at": now_iso(),
                         "percent": percent, "window": window}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def display(self, runtime: str) -> str:
        state, estimate = self.get(runtime)
        entry = self._read().get(runtime) or {}
        if not estimate and entry.get("percent") is not None and state is not Usage.UNKNOWN:
            window = f" · {entry['window']}" if entry.get("window") else ""
            return f"{entry['percent']:.0f}%{window} {state.value}"
        if not estimate and entry.get("window") and state is not Usage.UNKNOWN:
            return f"{entry['window']} left · {state.value}"  # e.g. an exact API balance
        return f"{state.value} (est.)" if estimate and state is not Usage.UNKNOWN else state.value


RATE_LIMIT_PATTERNS = ("rate limit", "rate_limit", "usage limit", "quota", "429", "too many requests",
                       "limit reached", "exceeded your", "credit balance")
AUTH_PATTERNS = ("not logged in", "unauthorized", "401", "authentication", "login required",
                 "api key", "please log in", "invalid_api_key")


def short(text: str, n: int = 90) -> str:
    """One-line, length-capped rendering for progress messages."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def classify_error(text: str) -> ErrorKind:
    t = text.lower()
    if any(p in t for p in RATE_LIMIT_PATTERNS):
        return ErrorKind.RATE_LIMIT
    if any(p in t for p in AUTH_PATTERNS):
        return ErrorKind.AUTH
    return ErrorKind.CRASH


async def stream_process(argv: list[str], cwd: Path, stdin: str, log_path: Path,
                         on_line: Callable[[str], None], timeout: int,
                         env: dict[str, str] | None = None) -> tuple[int | None, str, bool]:
    """Run argv, feed stdin, call on_line per stdout line, tee everything to log_path.

    Returns (exit_code, stderr_tail, timed_out). Kills the whole process group if
    cancelled (e.g. the user pressed /stop) or on timeout.
    """
    import asyncio
    import os
    import signal

    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024, env=env,
    )
    stderr_chunks: list[str] = []

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def pump(log) -> None:
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(stdin.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def read_err() -> None:
            async for raw in proc.stderr:
                line = raw.decode(errors="replace")
                stderr_chunks.append(line)
                log.write("[stderr] " + redact(line))

        err_task = asyncio.create_task(read_err())
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            log.write(redact(line))
            log.flush()
            on_line(line.rstrip("\n"))
        await err_task
        await proc.wait()

    with log_path.open("a") as log:
        try:
            await asyncio.wait_for(pump(log), timeout)
        except TimeoutError:
            kill()
            await proc.wait()
            return proc.returncode, "".join(stderr_chunks)[-2000:], True
        except asyncio.CancelledError:
            kill()
            raise
    return proc.returncode, "".join(stderr_chunks)[-2000:], False


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")
