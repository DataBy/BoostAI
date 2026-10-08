"""Antigravity adapter: `agy -p … --output-format stream-json`, using the local CLI login.

Headless `agy` only runs shell commands with every permission skipped, so each run
installs `.agents/hooks.json` in the workspace for its duration: the BOOST_AI hook
then gates *every* tool (allow-list, dangerous commands to the user, writes confined
to the workspace). The project's own hooks file, if any, is merged and restored.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
from contextlib import contextmanager

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

HOOK_NAME = "boost-ai-safety"
HOOK_TIMEOUT = 600
WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit")


def hook_command(req: ExecutionRequest) -> str:
    env = {"BOOST_AI_WORKSPACE": str(req.cwd), "BOOST_AI_READ_ONLY": "1" if req.read_only else "0"}
    if req.approval_dir is not None:
        env["BOOST_AI_APPROVAL_DIR"] = str(req.approval_dir)
    # Variables are inlined because hooks run via `sh -c`; this does not rely on env inheritance.
    assigns = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"{assigns} {shlex.quote(sys.executable)} -m boost_ai.hook"


@contextmanager
def installed_hook(req: ExecutionRequest):
    path = req.cwd / ".agents" / "hooks.json"
    original = path.read_bytes() if path.is_file() else None
    created_dir = not path.parent.exists()
    try:
        hooks = json.loads(original) if original else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON; refusing to modify it") from exc
    hooks[HOOK_NAME] = {"PreToolUse": [{"matcher": ".*", "hooks": [
        {"type": "command", "command": hook_command(req), "timeout": HOOK_TIMEOUT}]}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hooks, indent=2))
    try:
        yield
    finally:
        if original is None:
            path.unlink(missing_ok=True)
            if created_dir:
                try:
                    path.parent.rmdir()
                except OSError:
                    pass  # the agent created other files there; leave them
        else:
            path.write_bytes(original)


class AntigravityRuntime(Runtime):
    name = "antigravity"
    label = "Antigravity"

    def __init__(self, cfg: dict | None = None, binary: str = "agy"):
        self.cfg = cfg or {}
        self.binary = binary

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def argv(self, req: ExecutionRequest, prompt: str) -> list[str]:
        argv = [self.binary, "--output-format", "stream-json",
                "--json-schema", json.dumps(req.schema or RESULT_SCHEMA),
                "--dangerously-skip-permissions"]  # safe only because the hook gates every tool
        model = req.model or self.cfg.get("model")
        if model:
            argv += ["--model", model]
        if req.resume_session:
            argv += ["--conversation", req.resume_session]
        return argv + ["-p", prompt]

    async def execute(self, req: ExecutionRequest, progress: ProgressFn) -> ExecutionResult:
        if not self.installed():
            return ExecutionResult(ok=False, error_kind=ErrorKind.UNAVAILABLE,
                                   error="The `agy` CLI is not installed or not on PATH.")
        prompt = req.prompt  # attachments are listed in the prompt by the harness
        state = _ParseState(str(req.cwd))
        started = time.monotonic()
        try:
            with installed_hook(req):
                code, stderr, timed_out = await stream_process(
                    self.argv(req, prompt), req.cwd, "", req.log_path,
                    lambda line: state.feed(line, progress), req.timeout, req.env,
                )
        except RuntimeError as exc:
            return ExecutionResult(ok=False, error_kind=ErrorKind.CRASH, error=str(exc))

        result = ExecutionResult(ok=False, exit_code=code, text=state.response,
                                 input_tokens=state.input_tokens, output_tokens=state.output_tokens,
                                 cached_tokens=state.cached_tokens, model=state.model,
                                 session_id=state.conversation_id, duration=time.monotonic() - started)
        result.last_output = "\n".join(state.tail[-15:] + stderr.splitlines()[-10:])
        if timed_out:
            result.error_kind, result.error = ErrorKind.TIMEOUT, f"Antigravity did not finish within {req.timeout}s."
            return result
        if code != 0 or not state.got_result or state.status != "SUCCESS":
            reason = state.error or (stderr.strip().splitlines() or [f"The CLI exited with status {code}."])[-1]
            result.error_kind = classify_error(reason + "\n" + stderr)
            result.error = reason
            return result
        if not isinstance(state.structured, dict):
            result.error_kind = ErrorKind.CRASH
            result.error = "Antigravity finished but did not return the expected structured result."
            return result
        result.final = {k: v for k, v in state.structured.items() if k not in ("toolAction", "toolSummary")}
        result.ok = True
        return result


class _ParseState:
    """Incremental parser for `agy --output-format stream-json` events."""

    def __init__(self, cwd: str = "") -> None:
        self.cwd = cwd.rstrip("/") + "/" if cwd else ""
        self.got_result = False
        self.status = ""
        self.structured: dict | None = None
        self.response = ""
        self.error = ""
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cached_tokens: int | None = None
        self.model: str | None = None
        self.conversation_id: str | None = None
        self.tail: list[str] = []

    def feed(self, line: str, progress: ProgressFn) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.tail.append(line[:300])
            return
        kind = ev.get("event")
        if kind == "init":
            init = ev.get("init") or {}
            self.model = init.get("model") or self.model
            self.conversation_id = ev.get("conversation_id") or self.conversation_id
        elif kind == "step_update":
            step = ev.get("step_update") or {}
            if step.get("step_type") != "tool":
                return
            tool = step.get("tool_name", "")
            params = (step.get("tool_info") or {}).get("parameters") or {}
            if step.get("state") == "ACTIVE":
                if tool == "run_command":
                    progress(f"running  {short(params.get('CommandLine', ''))}")
                elif tool in WRITE_TOOLS:
                    target = next((str(v) for k, v in params.items() if k.lower().endswith(("file", "path"))), "?")
                    progress(f"edited  {short(target.removeprefix(self.cwd), 60)}")
            elif step.get("state") == "ERROR":
                self.tail = (self.tail + [f"{tool} failed: {short(json.dumps(params), 200)}"])[-40:]
        elif kind == "result":
            res = ev.get("result") or {}
            self.conversation_id = res.get("conversation_id") or self.conversation_id
            self.got_result = True
            self.status = str(res.get("status", ""))
            self.structured = res.get("structured_output")
            self.response = str(res.get("response") or "")
            if res.get("error"):
                self.error = str(res["error"])
            usage = res.get("usage") or {}
            self.input_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_tokens") or 0)
            self.cached_tokens = int(usage.get("cache_read_tokens") or 0) or None
            self.output_tokens = int(usage.get("output_tokens") or 0) + int(usage.get("thinking_tokens") or 0)
            if self.status and self.status != "SUCCESS" and not self.error:
                self.error = self.response or f"Antigravity finished with status {self.status}."
