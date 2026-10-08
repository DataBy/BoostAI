"""Claude Code adapter: `claude -p --output-format stream-json`, using the local CLI login.

Every Bash command Claude wants to run passes through a PreToolUse hook
(`python -m boost_ai.hook`) which blocks dangerous commands or asks the user.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time

from .base import (
    RESULT_SCHEMA,
    ErrorKind,
    ExecutionRequest,
    ExecutionResult,
    ProgressFn,
    Runtime,
    UsageReport,
    classify_error,
    short,
    stream_process,
    usage_from_utilization,
)

ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "NotebookEdit", "TodoWrite",
                 "WebSearch", "WebFetch"]
EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
# Commits belong to BOOST_AI's approval flow (your message, no AI attribution). Everything
# else is allowed; pushes and destructive commands go to the user through the hook.
DISALLOWED_TOOLS = ["Bash(git commit:*)"]
WINDOW_NAMES = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d opus"}
HOOK_TIMEOUT = 600  # seconds the hook may wait for the user's decision


def hook_settings() -> str:
    command = f"{shlex.quote(sys.executable)} -m boost_ai.hook"
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]},
    ]}})


class ClaudeRuntime(Runtime):
    name = "claude"
    label = "Claude Code"

    def __init__(self, cfg: dict | None = None, binary: str = "claude"):
        self.cfg = cfg or {}
        self.binary = binary

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def argv(self, req: ExecutionRequest) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--json-schema", json.dumps(req.schema or RESULT_SCHEMA),
                "--settings", hook_settings(),
                "--permission-mode", "default" if req.read_only else "acceptEdits",
                "--allowedTools", ",".join([t for t in ALLOWED_TOOLS if not (req.read_only and t in EDIT_TOOLS)]
                                           + [f"mcp__{name}" for name in req.mcp_servers]),
                "--disallowedTools", ",".join(DISALLOWED_TOOLS + (EDIT_TOOLS if req.read_only else []))]
        if req.resume_session:  # sessions are persisted (no --no-session-persistence) so they can resume
            argv += ["--resume", req.resume_session]
        for directory in req.read_dirs:
            argv += ["--add-dir", str(directory)]
        if req.mcp_servers:  # only these MCP servers (plus your global ones with global_extensions)
            argv += ["--mcp-config", json.dumps({"mcpServers": req.mcp_servers})]
        if not self.cfg.get("global_extensions", False):
            argv += ["--strict-mcp-config", "--disable-slash-commands"]
        model = req.model or self.cfg.get("model")
        if model:
            argv += ["--model", model]
        return argv

    async def execute(self, req: ExecutionRequest, progress: ProgressFn) -> ExecutionResult:
        if not self.installed():
            return ExecutionResult(ok=False, error_kind=ErrorKind.UNAVAILABLE,
                                   error="The `claude` CLI is not installed or not on PATH.")
        prompt = req.prompt  # attachments are listed in the prompt by the harness
        env = dict(req.env if req.env is not None else os.environ)
        if req.approval_dir is not None:
            env["BOOST_AI_APPROVAL_DIR"] = str(req.approval_dir)

        state = _ParseState(str(req.cwd))
        started = time.monotonic()
        code, stderr, timed_out = await stream_process(
            self.argv(req), req.cwd, prompt, req.log_path,
            lambda line: state.feed(line, progress), req.timeout, env,
        )
        result = ExecutionResult(ok=False, exit_code=code, text=state.result_text,
                                 input_tokens=state.input_tokens, output_tokens=state.output_tokens,
                                 duration=time.monotonic() - started, usage=state.usage, cost=state.cost,
                                 model=state.model, session_id=state.session_id,
                                 cached_tokens=state.cached_tokens)
        result.last_output = "\n".join(state.tail[-15:] + stderr.splitlines()[-10:])

        if timed_out:
            result.error_kind, result.error = ErrorKind.TIMEOUT, f"Claude did not finish within {req.timeout}s."
            return result
        if state.rejected:
            result.error_kind = ErrorKind.RATE_LIMIT
            result.error = state.error or "Claude's usage limit has been reached."
            return result
        if state.error or code != 0 or not state.got_result:
            reason = state.error or (f"The CLI exited with status {code}." if code else
                                     "Claude ended without a result.")
            result.error_kind = classify_error(reason + "\n" + stderr)
            result.error = reason
            return result
        if not isinstance(state.structured, dict):
            result.error_kind = ErrorKind.CRASH
            result.error = "Claude finished but did not return the expected structured result."
            return result
        result.final = state.structured
        result.ok = True
        return result


class _ParseState:
    """Incremental parser for `claude -p --output-format stream-json` events."""

    def __init__(self, cwd: str = "") -> None:
        self.cwd = cwd.rstrip("/") + "/" if cwd else ""
        self.got_result = False
        self.structured: dict | None = None
        self.result_text = ""
        self.error = ""
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cost: float | None = None
        self.usage: UsageReport | None = None
        self.rejected = False
        self.model: str | None = None
        self.session_id: str | None = None
        self.cached_tokens: int | None = None
        self.tail: list[str] = []

    def feed(self, line: str, progress: ProgressFn) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.tail.append(line[:300])
            return
        kind = ev.get("type")
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        if kind == "system" and ev.get("subtype") == "init":
            self.model = ev.get("model") or self.model
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    self._tool(block.get("name", ""), block.get("input") or {}, progress)
                elif block.get("type") == "thinking":
                    progress("thinking")
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    text = block.get("content")
                    text = text if isinstance(text, str) else json.dumps(text)
                    self.tail = (self.tail + [text.strip()[:300]])[-40:]
        elif kind == "rate_limit_event":
            self._rate_limit(ev.get("rate_limit_info") or {})
        elif kind == "result":
            self.got_result = True
            self.structured = ev.get("structured_output")
            self.result_text = str(ev.get("result") or "")
            self.cost = ev.get("total_cost_usd")
            u = ev.get("usage") or {}
            self.input_tokens = sum(int(u.get(k) or 0) for k in
                                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            self.cached_tokens = int(u.get("cache_read_input_tokens") or 0) or None
            self.output_tokens = int(u.get("output_tokens") or 0)
            if ev.get("is_error") or ev.get("subtype") not in ("success", None):
                self.error = self.result_text or f"Claude reported an error ({ev.get('subtype')})."
                if ev.get("api_error_status") == 429:
                    self.rejected = True

    def _tool(self, name: str, inp: dict, progress: ProgressFn) -> None:
        if name == "Bash":
            progress(f"running  {short(inp.get('command', ''))}")
        elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = str(inp.get("file_path") or inp.get("notebook_path") or "?").removeprefix(self.cwd)
            progress(f"edited  {short(path, 60)}")
        elif name in ("Read", "Glob", "Grep"):
            target = inp.get("file_path") or inp.get("pattern") or ""
            progress(f"reading  {short(str(target).removeprefix(self.cwd), 60)}")

    def _rate_limit(self, info: dict) -> None:
        windows = info.get("unifiedWindows") or {}
        binding = max(windows.items(), key=lambda kv: kv[1].get("utilization") or 0, default=None)
        util = binding[1].get("utilization") if binding else None
        rejected = info.get("status") == "rejected"
        self.rejected = self.rejected or rejected
        self.usage = UsageReport(
            state=usage_from_utilization(util, rejected),
            percent=round(util * 100, 1) if util is not None else None,
            window=WINDOW_NAMES.get(binding[0], binding[0]) if binding else None,
        )
