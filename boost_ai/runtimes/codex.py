"""Codex CLI adapter: `codex exec --json`, using the local CLI login (no API key)."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .base import (
    RESULT_SCHEMA,
    ErrorKind,
    ExecutionRequest,
    ExecutionResult,
    ProgressFn,
    Runtime,
    classify_error,
    short,
    stream_process,
)


class CodexRuntime(Runtime):
    name = "codex"
    label = "Codex"

    def __init__(self, cfg: dict | None = None, binary: str = "codex"):
        self.cfg = cfg or {}
        self.binary = binary

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def argv(self, req: ExecutionRequest, schema_path, final_path) -> list[str]:
        sandbox = "read-only" if req.read_only else self.cfg.get("sandbox", "workspace-write")
        if req.resume_session:
            # `resume` has no -C/-s; cwd comes from the process and the sandbox from config.
            argv = [self.binary, "exec", "resume", req.resume_session, "--json", "--skip-git-repo-check",
                    "-c", f'sandbox_mode="{sandbox}"']
        else:
            argv = [self.binary, "exec", "--json", "--skip-git-repo-check", "-C", str(req.cwd), "-s", sandbox]
        argv += ["--output-schema", str(schema_path), "-o", str(final_path)]
        for name, server in req.mcp_servers.items():  # read-only context servers: no per-call approval
            argv += ["-c", f"mcp_servers.{name}.command={json.dumps(server['command'])}",
                     "-c", f"mcp_servers.{name}.args={json.dumps(server.get('args', []))}",
                     "-c", f'mcp_servers.{name}.default_tools_approval_mode="approve"']
        model = req.model or self.cfg.get("model")
        if model:
            argv += ["-m", model]
        for image in req.images:
            argv += ["-i", str(image)]
        return argv + ["-"]  # prompt on stdin

    async def execute(self, req: ExecutionRequest, progress: ProgressFn) -> ExecutionResult:
        if not self.installed():
            return ExecutionResult(ok=False, error_kind=ErrorKind.UNAVAILABLE,
                                   error="The `codex` CLI is not installed or not on PATH.")
        schema_path = req.log_path.with_suffix(".schema.json")
        final_path = req.log_path.with_suffix(".final.json")
        req.log_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(json.dumps(req.schema or RESULT_SCHEMA))

        state = _ParseState(str(req.cwd))
        started = time.monotonic()
        code, stderr, timed_out = await stream_process(
            self.argv(req, schema_path, final_path), req.cwd, req.prompt, req.log_path,
            lambda line: state.feed(line, progress), req.timeout, req.env,
        )
        result = ExecutionResult(ok=False, exit_code=code, text=state.last_message,
                                 input_tokens=state.input_tokens or None,
                                 output_tokens=state.output_tokens or None,
                                 cached_tokens=state.cached_tokens or None,
                                 session_id=state.thread_id, model=session_model(state.thread_id),
                                 duration=time.monotonic() - started)
        tail = "\n".join(state.tail[-15:] + stderr.splitlines()[-10:])
        result.last_output = tail

        if timed_out:
            result.error_kind, result.error = ErrorKind.TIMEOUT, f"Codex did not finish within {req.timeout}s."
            return result
        if state.errors or code != 0:
            reason = state.errors[-1] if state.errors else f"The CLI exited with status {code}."
            result.error_kind = classify_error(reason + "\n" + stderr)
            result.error = reason
            return result

        final_text = final_path.read_text() if final_path.is_file() else state.last_message
        try:
            result.final = json.loads(final_text)
        except json.JSONDecodeError:
            result.error_kind = ErrorKind.CRASH
            result.error = "Codex finished but did not return the expected structured result."
            return result
        result.ok = True
        return result


def session_model(thread_id: str | None) -> str | None:
    """Codex does not put the model in its --json events, but records it in the session
    log ($CODEX_HOME/sessions/**/rollout-*-<thread>.jsonl, `turn_context.model`)."""
    if not thread_id:
        return None
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    for path in sorted(home.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"), reverse=True)[:1]:
        model = None
        try:
            with path.open() as f:
                for line in f:
                    if '"turn_context"' in line:
                        model = (json.loads(line).get("payload") or {}).get("model") or model
        except (OSError, json.JSONDecodeError):
            return model
        return model
    return None


class _ParseState:
    """Incremental parser for `codex exec --json` events."""

    def __init__(self, cwd: str = "") -> None:
        self.cwd = cwd.rstrip("/") + "/" if cwd else ""
        self.last_message = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.thread_id: str | None = None
        self.errors: list[str] = []
        self.tail: list[str] = []

    def feed(self, line: str, progress: ProgressFn) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.tail.append(line[:300])
            return
        kind = ev.get("type", "")
        item = ev.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            self.thread_id = ev.get("thread_id")
        elif kind == "item.started" and itype == "command_execution":
            progress(f"running  {short(item.get('command', ''))}")
        elif kind == "item.completed" and itype == "command_execution":
            out = (item.get("aggregated_output") or "").strip().splitlines()
            self.tail = (self.tail + [f"$ {short(item.get('command', ''))}"] + out[-5:])[-40:]
        elif kind == "item.completed" and itype == "file_change":
            paths = [c.get("path", "?").removeprefix(self.cwd) for c in item.get("changes", [])]
            progress("edited  " + ", ".join(short(p, 60) for p in paths[:4]))
        elif kind == "item.completed" and itype == "agent_message":
            self.last_message = item.get("text", "")
        elif kind == "item.completed" and itype == "reasoning":
            progress("thinking")
        elif kind == "turn.completed":
            usage = ev.get("usage") or {}
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            self.cached_tokens += int(usage.get("cached_input_tokens") or 0)
        elif kind == "turn.failed":
            self.errors.append(((ev.get("error") or {}).get("message")) or "Codex turn failed.")
        elif kind == "error":
            self.errors.append(ev.get("message") or "Codex reported an error.")

