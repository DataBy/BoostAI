"""Claude adapter (fake `claude` executable), safety hook, approval round-trip."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from boost_ai import hook
from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import (
    ErrorKind,
    ExecutionRequest,
    ExecutionResult,
    Runtime,
    Usage,
    UsageBook,
    usage_from_utilization,
)
from boost_ai.runtimes.claude import ClaudeRuntime

from .conftest import ScriptedUI, done

FAKE = """#!{python}
import json, os, sys
args = sys.argv[1:]
open({argv_log!r}, "w").write(json.dumps({{"args": args, "prompt": sys.stdin.read(),
                                          "approval_dir": os.environ.get("BOOST_AI_APPROVAL_DIR")}}))
for e in {events!r}:
    print(json.dumps(e), flush=True)
sys.exit({code})
"""


def fake_claude(tmp_path: Path, events: list, code: int = 0) -> tuple[str, Path]:
    argv_log = tmp_path / "argv.json"
    script = tmp_path / "claude"
    script.write_text(FAKE.format(python=sys.executable, argv_log=str(argv_log), events=events, code=code))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


def req(tmp_path, **kw) -> ExecutionRequest:
    return ExecutionRequest(prompt="do it", cwd=tmp_path, log_path=tmp_path / "logs" / "a.jsonl",
                            env=dict(os.environ), **kw)


RATE = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.26}, "seven_day": {"utilization": 0.81}}}}


def result_event(structured=None, **kw):
    return {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(structured),
            "structured_output": structured, "total_cost_usd": 0.12,
            "usage": {"input_tokens": 4, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 500,
                      "output_tokens": 300}, **kw}


async def test_claude_success(tmp_path):
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(tmp_path / "src" / "a.py")}}]}},
        RATE,
        result_event(done()),
    ]
    binary, argv_log = fake_claude(tmp_path, events)
    progress: list[str] = []
    result = await ClaudeRuntime({"model": "sonnet"}, binary=binary).execute(
        req(tmp_path, approval_dir=tmp_path / "approvals"), progress.append)
    assert result.ok and result.final == done()
    assert (result.input_tokens, result.output_tokens, result.cost) == (1504, 300, 0.12)
    assert result.usage.state is Usage.CONSERVE and result.usage.percent == 81 and result.usage.window == "7d"
    assert "running  pytest -q" in progress and "edited  src/a.py" in progress
    call = json.loads(argv_log.read_text())
    args = call["args"]
    assert args[:2] == ["-p", "--output-format"] and "--json-schema" in args
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in args
    assert "--strict-mcp-config" in args and "--disable-slash-commands" in args
    assert args[args.index("--disallowedTools") + 1] == "Bash(git commit:*)"   # commits are BOOST_AI's flow
    assert "WebFetch" in args[args.index("--allowedTools") + 1]
    settings = json.loads(args[args.index("--settings") + 1])
    assert "boost_ai.hook" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert call["approval_dir"] == str(tmp_path / "approvals")
    assert call["prompt"] == "do it"


async def test_claude_rate_limited(tmp_path):
    events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "unifiedWindows": {
        "five_hour": {"utilization": 1.0}}}},
              {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "result": "Claude AI usage limit reached", "api_error_status": 429}]
    binary, _ = fake_claude(tmp_path, events, code=1)
    result = await ClaudeRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and result.error_kind is ErrorKind.RATE_LIMIT
    assert result.usage.state is Usage.EXHAUSTED


async def test_claude_no_result_is_crash(tmp_path):
    binary, _ = fake_claude(tmp_path, [{"type": "system", "subtype": "init"}], code=1)
    result = await ClaudeRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and result.error_kind is ErrorKind.CRASH and "status 1" in result.error


async def test_claude_missing_structured_output(tmp_path):
    binary, _ = fake_claude(tmp_path, [result_event(None)])
    result = await ClaudeRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and "structured result" in result.error


@pytest.mark.parametrize("util,rejected,state", [
    (0.1, False, Usage.AVAILABLE), (0.6, False, Usage.NORMAL), (0.8, False, Usage.CONSERVE),
    (0.95, False, Usage.CRITICAL), (1.0, False, Usage.EXHAUSTED), (0.2, True, Usage.EXHAUSTED),
    (None, False, Usage.AVAILABLE),
])
def test_usage_mapping(util, rejected, state):
    assert usage_from_utilization(util, rejected) is state


def test_exact_usage_display(tmp_path):
    book = UsageBook(tmp_path / "u.json")
    book.record("claude", Usage.NORMAL, estimate=False, percent=62.4, window="5h")
    assert book.display("claude") == "62% · 5h NORMAL"


# ── hook ───────────────────────────────────────────────────────────────────
def run_hook(command: str, env: dict) -> tuple[int, str]:
    stdin = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        return hook.main(stdin, env), err.getvalue()
    finally:
        sys.stderr = old


def test_hook_allows_safe_commands():
    assert run_hook("pytest -q", {}) == (0, "")


def test_hook_blocks_dangerous_without_harness():
    code, err = run_hook("rm -rf /tmp/x", {})
    assert code == 2 and "recursive force delete" in err and "needs human approval" in err


def test_hook_fails_closed_on_garbage():
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        assert hook.main(io.StringIO("not json"), {}) == 2
    finally:
        sys.stderr = old


def test_hook_subprocess_entrypoint(tmp_path):
    import subprocess
    p = subprocess.run([sys.executable, "-m", "boost_ai.hook"], input=json.dumps(
        {"tool_input": {"command": "git push --force"}}), capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if k != "BOOST_AI_APPROVAL_DIR"})
    assert p.returncode == 2 and "force push" in p.stderr


def test_approval_protocol(tmp_path):
    import threading

    def approver():
        for _ in range(100):
            if reqs := hook.pending(tmp_path):
                hook.respond(tmp_path, reqs[0].id, reqs[0].command == "rm -rf build")
                return
            __import__("time").sleep(0.02)

    t = threading.Thread(target=approver)
    t.start()
    assert hook.request_approval(tmp_path, "rm -rf build", ["recursive force delete"], wait=5, poll=0.02)
    t.join()
    assert hook.pending(tmp_path) == []
    assert not hook.request_approval(tmp_path, "rm -rf x", ["r"], wait=0.1, poll=0.02)  # timeout → deny


# ── orchestrator brings hook requests to the user ──────────────────────────
class AskingRuntime(Runtime):
    """Simulates an agent whose hook asks to run dangerous commands mid-run."""
    name = "claude"

    def __init__(self, commands: list[str]):
        self.commands = commands
        self.decisions: list[bool] = []

    def installed(self):
        return True

    async def execute(self, req, progress):
        for cmd in self.commands:
            reasons = [d.reason for d in __import__("boost_ai.safety", fromlist=["x"]).check(cmd)]
            self.decisions.append(await asyncio.to_thread(
                hook.request_approval, req.approval_dir, cmd, reasons, 5, 0.02))
        (req.cwd / "out.txt").write_text("x\n")
        return ExecutionResult(ok=True, final=done(), duration=1)


async def test_dangerous_command_needs_user(repo, cfg, tmp_path):
    cfg["routing"]["policy"]["L2"] = ["claude"]
    rt = AskingRuntime(["rm -rf build", "git push --force origin main"])
    ui = ScriptedUI(["Allow once", "Deny", "No"])
    h = Harness(repo, cfg, ui, {"claude": rt}, usage=UsageBook(tmp_path / "u.json"),
                metrics_path=tmp_path / "m.jsonl")
    await h.run_task("Add pagination to the list endpoint")
    assert rt.decisions == [True, False]
    (q1, o1), (q2, o2) = ui.questions[:2]
    assert "rm -rf build" in q1 and o1 == ["Deny", "Allow once"]
    assert "REWRITES REMOTE HISTORY" in q2 and o2 == ["Deny", "Allow force push"]
    assert "Allowed: rm -rf build" in ui.text() and "Denied: git push --force origin main" in ui.text()


def test_global_extensions_opt_in(tmp_path):
    args = ClaudeRuntime({"global_extensions": True}).argv(req(tmp_path))
    assert "--strict-mcp-config" not in args


def test_read_dirs_become_add_dir(tmp_path):
    args = ClaudeRuntime().argv(req(tmp_path, read_dirs=[tmp_path / "skills"]))
    assert args[args.index("--add-dir") + 1] == str(tmp_path / "skills")
