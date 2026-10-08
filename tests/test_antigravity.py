"""Antigravity adapter (fake `agy`), its tool gate, and hook install/restore."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from boost_ai import hook
from boost_ai.runtimes import build_runtimes
from boost_ai.runtimes.antigravity import HOOK_NAME, AntigravityRuntime, installed_hook
from boost_ai.runtimes.base import ErrorKind, ExecutionRequest

from .conftest import done


# ── tool gate ──────────────────────────────────────────────────────────────
def gate(tool: str, args: dict, workspace: Path, **env) -> tuple[bool, str]:
    return hook.decide_agy(tool, args, {"BOOST_AI_WORKSPACE": str(workspace), **env})


def test_gate_shell(tmp_path):
    assert gate("run_command", {"CommandLine": "pytest -q"}, tmp_path)[0]
    allowed, msg = gate("run_command", {"CommandLine": "rm -rf /"}, tmp_path)
    assert not allowed and "recursive force delete" in msg
    assert not gate("send_command_input", {"Input": "sudo reboot"}, tmp_path)[0]


def test_gate_writes_confined_to_workspace(tmp_path):
    assert gate("write_to_file", {"TargetFile": str(tmp_path / "src" / "a.py")}, tmp_path)[0]
    assert gate("replace_file_content", {"TargetFile": "src/a.py"}, tmp_path)[0]
    assert not gate("write_to_file", {"TargetFile": "/home/someone/.bashrc"}, tmp_path)[0]
    assert not gate("write_to_file", {"TargetFile": "../escape.py"}, tmp_path)[0]
    assert not gate("write_to_file", {"Content": "x"}, tmp_path)[0]           # no path → fail closed
    assert not gate("write_to_file", {"TargetFile": "a.py"}, tmp_path, BOOST_AI_READ_ONLY="1")[0]
    assert not hook.decide_agy("write_to_file", {"TargetFile": "a.py"}, {})[0]  # no workspace → closed


@pytest.mark.parametrize("tool", ["browser_click_element", "call_mcp_tool", "search_web", "invoke_subagent",
                                  "schedule", "send_message", "delete_knowledge", "something_new"])
def test_gate_denies_everything_else(tmp_path, tool):
    allowed, msg = gate(tool, {}, tmp_path)
    assert not allowed and tool in msg


@pytest.mark.parametrize("tool", ["view_file", "grep_search", "list_dir", "finish"])
def test_gate_allows_reading(tmp_path, tool):
    assert gate(tool, {}, tmp_path)[0]


def test_hook_agy_protocol(tmp_path):
    out = io.StringIO()
    payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": "git push --force"}}}
    assert hook.main(io.StringIO(json.dumps(payload)), {"BOOST_AI_WORKSPACE": str(tmp_path)}, out) == 0
    decision = json.loads(out.getvalue())
    assert decision["decision"] == "deny" and "force push" in decision["reason"]


# ── hook install / restore ─────────────────────────────────────────────────
def req(tmp_path, **kw) -> ExecutionRequest:
    return ExecutionRequest(prompt="do it", cwd=tmp_path, log_path=tmp_path.parent / "logs" / "a.jsonl",
                            env=dict(os.environ), **kw)


def test_hook_installed_and_removed(tmp_path):
    with installed_hook(req(tmp_path, approval_dir=tmp_path / "ap")):
        data = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
        cmd = data[HOOK_NAME]["PreToolUse"][0]["hooks"][0]["command"]
        assert "boost_ai.hook" in cmd and "BOOST_AI_WORKSPACE=" in cmd and "BOOST_AI_APPROVAL_DIR=" in cmd
    assert not (tmp_path / ".agents").exists()


def test_existing_project_hooks_are_merged_and_restored(tmp_path):
    (tmp_path / ".agents").mkdir()
    original = b'{"lint": {"PostToolUse": []}}\n'
    (tmp_path / ".agents" / "hooks.json").write_bytes(original)
    with pytest.raises(ValueError):
        with installed_hook(req(tmp_path)):
            data = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
            assert set(data) == {"lint", HOOK_NAME}
            raise ValueError("runtime crashed")
    assert (tmp_path / ".agents" / "hooks.json").read_bytes() == original


def test_invalid_project_hooks_are_not_touched(tmp_path):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "hooks.json").write_text("{broken")
    with pytest.raises(RuntimeError):
        with installed_hook(req(tmp_path)):
            pass
    assert (tmp_path / ".agents" / "hooks.json").read_text() == "{broken"


def test_read_only_flag_reaches_hook(tmp_path):
    with installed_hook(req(tmp_path, read_only=True)):
        assert "BOOST_AI_READ_ONLY=1" in (tmp_path / ".agents" / "hooks.json").read_text()


# ── adapter against a fake agy ─────────────────────────────────────────────
FAKE = """#!{python}
import json, os, sys
args = sys.argv[1:]
hooks = json.load(open(os.path.join(os.getcwd(), ".agents", "hooks.json")))
open({argv_log!r}, "w").write(json.dumps({{"args": args, "hooks": list(hooks)}}))
for e in {events!r}:
    print(json.dumps(e), flush=True)
sys.exit({code})
"""


def fake_agy(bin_dir: Path, events: list, code: int = 0) -> tuple[str, Path]:
    bin_dir.mkdir(exist_ok=True)
    argv_log = bin_dir / "argv.json"
    script = bin_dir / "agy"
    script.write_text(FAKE.format(python=sys.executable, argv_log=str(argv_log), events=events, code=code))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


def step(state, tool, **params):
    return {"event": "step_update", "step_update": {"state": state, "step_type": "tool", "tool_name": tool,
                                                     "tool_info": {"parameters": params}}}


async def test_agy_success(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    final = {**done(), "toolAction": "x", "toolSummary": "y"}
    events = [{"event": "init", "init": {"model": "m"}},
              step("ACTIVE", "run_command", CommandLine="pytest -q"),
              step("ACTIVE", "write_to_file", TargetFile=str(ws / "a.py")),
              {"event": "result", "result": {"status": "SUCCESS", "response": json.dumps(final),
                                             "structured_output": final,
                                             "usage": {"input_tokens": 1000, "output_tokens": 50,
                                                       "thinking_tokens": 10, "cache_read_tokens": 200}}}]
    binary, argv_log = fake_agy(tmp_path / "bin", events)
    progress: list[str] = []
    result = await AntigravityRuntime({"model": "gemini-x"}, binary=binary).execute(req(ws), progress.append)
    assert result.ok and result.final == done()                       # agy's extra keys stripped
    assert (result.input_tokens, result.output_tokens) == (1200, 60)
    assert "running  pytest -q" in progress and "edited  a.py" in progress
    call = json.loads(argv_log.read_text())
    assert call["hooks"] == [HOOK_NAME]                                # hook active during the run
    assert call["args"][-2:] == ["-p", "do it"] and "--dangerously-skip-permissions" in call["args"]
    assert call["args"][call["args"].index("--model") + 1] == "gemini-x"
    assert not (ws / ".agents").exists()                               # cleaned up afterwards


async def test_agy_failure_status(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    events = [{"event": "result", "result": {"status": "ERROR", "error": "quota exceeded for model"}}]
    binary, _ = fake_agy(tmp_path / "bin", events, code=1)
    result = await AntigravityRuntime(binary=binary).execute(req(ws), lambda _: None)
    assert not result.ok and result.error_kind is ErrorKind.RATE_LIMIT
    assert not (ws / ".agents").exists()


async def test_agy_missing_structured_output(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    binary, _ = fake_agy(tmp_path / "bin", [{"event": "result", "result": {"status": "SUCCESS", "response": ""}}])
    result = await AntigravityRuntime(binary=binary).execute(req(ws), lambda _: None)
    assert not result.ok and "structured result" in result.error


def test_antigravity_is_opt_in(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert "antigravity" not in build_runtimes({"runtimes": {}})
    assert "antigravity" in build_runtimes({"runtimes": {"antigravity": {"enabled": True}}})
