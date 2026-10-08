"""Codex adapter against a fake `codex` executable (no quota used)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from boost_ai.runtimes.base import ErrorKind, ExecutionRequest
from boost_ai.runtimes.codex import CodexRuntime

FAKE = """#!{python}
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
open({argv_log!r}, "w").write(json.dumps({{"args": args, "prompt": prompt}}))
events = {events}
for e in events:
    print(json.dumps(e), flush=True)
if {final!r} is not None:
    out = args[args.index("-o") + 1]
    open(out, "w").write({final!r})
sys.exit({code})
"""


def fake_codex(tmp_path: Path, events: list, final: str | None, code: int = 0) -> tuple[str, Path]:
    argv_log = tmp_path / "argv.json"
    script = tmp_path / "codex"
    script.write_text(FAKE.format(python=sys.executable, argv_log=str(argv_log), events=repr(events),
                                  final=final, code=code))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


def req(tmp_path) -> ExecutionRequest:
    return ExecutionRequest(prompt="do the thing", cwd=tmp_path, log_path=tmp_path / "logs" / "a1.jsonl",
                            env=dict(os.environ))


FINAL = {"status": "done", "summary": "ok", "commit_message": "fix: x", "decisions": [], "problems": [],
         "architecture_notes": [], "blocker": ""}


async def test_success_parses_final_tokens_and_progress(tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q",
                                            "aggregated_output": "1 passed", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "file_change",
                                            "changes": [{"path": str(tmp_path / "a.py"), "kind": "add"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(FINAL)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 80}},
    ]
    binary, argv_log = fake_codex(tmp_path, events, json.dumps(FINAL))
    progress: list[str] = []
    result = await CodexRuntime({"sandbox": "workspace-write", "model": "gpt-x"}, binary=binary) \
        .execute(req(tmp_path), progress.append)
    assert result.ok and result.final == FINAL
    assert (result.input_tokens, result.output_tokens) == (1200, 80)
    assert any("pytest -q" in p for p in progress) and "edited  a.py" in progress
    call = json.loads(argv_log.read_text())
    assert call["prompt"] == "do the thing"
    args = call["args"]
    assert args[:2] == ["exec", "--json"] and "workspace-write" in args
    assert args[args.index("-m") + 1] == "gpt-x"
    assert "danger-full-access" not in args
    assert (tmp_path / "logs" / "a1.jsonl").read_text().count("\n") >= len(events)


async def test_rate_limit_is_classified(tmp_path):
    events = [{"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}]
    binary, _ = fake_codex(tmp_path, events, None, code=1)
    result = await CodexRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and result.error_kind is ErrorKind.RATE_LIMIT
    assert "usage limit" in result.error


async def test_crash_reports_exit_status(tmp_path):
    binary, _ = fake_codex(tmp_path, [], None, code=2)
    result = await CodexRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and result.error_kind is ErrorKind.CRASH
    assert "status 2" in result.error


async def test_missing_structured_result(tmp_path):
    binary, _ = fake_codex(tmp_path, [{"type": "item.completed",
                                       "item": {"type": "agent_message", "text": "not json"}}], None)
    result = await CodexRuntime(binary=binary).execute(req(tmp_path), lambda _: None)
    assert not result.ok and "structured result" in result.error


async def test_not_installed(tmp_path):
    result = await CodexRuntime(binary="definitely-not-codex").execute(req(tmp_path), lambda _: None)
    assert result.error_kind is ErrorKind.UNAVAILABLE


@pytest.mark.integration
async def test_real_codex_smoke(tmp_path):
    """Consumes a small amount of real Codex quota. Run with: pytest -m integration"""
    r = ExecutionRequest(prompt="Do not change any files. Return status done with summary 'ok'.",
                         cwd=tmp_path, log_path=tmp_path / "log.jsonl")
    result = await CodexRuntime({"sandbox": "read-only"}).execute(r, lambda _: None)
    assert result.ok and result.final["status"] == "done"
