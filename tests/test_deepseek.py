"""DeepSeek as agent engine and as cheap triage — no network in these tests."""

from __future__ import annotations

import json
import os
import stat
import sys

from boost_ai import triage as triage_mod
from boost_ai.orchestrator import Harness
from boost_ai.runtimes import deepseek
from boost_ai.runtimes.base import ExecutionRequest, Usage, UsageBook
from boost_ai.runtimes.deepseek import DeepSeekRuntime

from .conftest import FakeRuntime, ScriptedUI, done, writes

FAKE_CLAUDE = """#!{python}
import json, os, sys
keys = ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "BOOST_AI_APPROVAL_DIR"]
open({log!r}, "w").write(json.dumps({{"args": sys.argv[1:], "env": {{k: os.environ.get(k) for k in keys}}}}))
final = {final!r}
print(json.dumps({{"type": "system", "subtype": "init", "model": os.environ.get("ANTHROPIC_MODEL")}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "x",
                   "structured_output": final, "total_cost_usd": 9.99,
                   "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
"""


async def test_agent_runs_claude_cli_against_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "my-anthropic-token")     # must not leak into the run
    monkeypatch.setattr(deepseek, "account_balance", lambda: 2.8)
    log = tmp_path / "call.json"
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log), final=done()))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    rt = DeepSeekRuntime({}, binary=str(script))
    req = ExecutionRequest(prompt="hi", cwd=tmp_path, log_path=tmp_path / "l.jsonl", env=dict(os.environ),
                           approval_dir=tmp_path / "ap")
    result = await rt.execute(req, lambda _: None)
    call = json.loads(log.read_text())
    env = call["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-test-1234567890" and env["ANTHROPIC_AUTH_TOKEN"] is None
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash" and env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-flash"
    assert env["BOOST_AI_APPROVAL_DIR"] == str(tmp_path / "ap")          # safety hook still wired
    assert "boost_ai.hook" in call["args"][call["args"].index("--settings") + 1]
    assert result.ok and result.model == "deepseek-flash"
    assert result.cost is None                                         # not Anthropic pricing
    assert result.usage.window == "$2.80" and result.usage.state is Usage.AVAILABLE


def test_not_installed_without_key(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert not DeepSeekRuntime().installed()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    assert DeepSeekRuntime().installed()


def test_balance_display(tmp_path):
    book = UsageBook(tmp_path / "u.json")
    book.record("deepseek", Usage.CONSERVE, estimate=False, window="$1.20")
    assert book.display("deepseek") == "$1.20 left · CONSERVE"
    assert deepseek._balance_state(0) is Usage.EXHAUSTED and deepseek._balance_state(0.3) is Usage.CRITICAL


# ── triage ─────────────────────────────────────────────────────────────────
CFG = {"triage": {"llm": "deepseek"}}


def test_llm_triage_cannot_go_below_risk_floor(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setattr(deepseek, "chat_json", lambda *a, **k: {"level": 0, "risks": [], "reason": "tiny"})
    t = triage_mod.llm_triage("change the password hashing to argon2", CFG)
    assert t.source == "llm" and t.level == 3 and "security" in t.risks


def test_llm_triage_can_lower_the_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setattr(deepseek, "chat_json", lambda *a, **k: {"level": 0, "risks": [], "reason": "greeting"})
    assert triage_mod.llm_triage("Hola, ¿cómo vas?", CFG).level == 0      # rules alone would say L2


def test_llm_triage_falls_back_on_failure(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")

    def boom(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(deepseek, "chat_json", boom)
    assert triage_mod.llm_triage("anything", CFG) is None
    monkeypatch.setattr(deepseek, "chat_json", lambda *a, **k: {"level": 9})
    assert triage_mod.llm_triage("anything", CFG) is None


def test_llm_triage_off_without_key_or_config():
    assert triage_mod.llm_triage("x", CFG) is None
    assert triage_mod.llm_triage("x", {"triage": {"llm": None}}) is None


async def test_conversation_moves_off_cheap_runtime_when_level_rises(repo, cfg, tmp_path, monkeypatch):
    cfg["routing"]["policy"].update({"L0": ["deepseek", "claude"], "L3": ["claude", "deepseek"][:1]})
    ds = FakeRuntime("deepseek", [lambda req: __import__("boost_ai.runtimes.base", fromlist=["x"]).ExecutionResult(
        ok=True, final=done(summary="hola"), duration=1, session_id="d-1")])
    claude = FakeRuntime("claude", [writes("auth.py")])
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"deepseek": ds, "claude": claude}, usage=UsageBook(tmp_path / "u.json"),
                metrics_path=tmp_path / "m")
    monkeypatch.setattr(triage_mod, "llm_triage", lambda text, c: triage_mod.Triage(level=0, source="llm")
                        if text == "Hola" else None)
    await h.run_task("Hola")
    assert h.task.runtime == "deepseek"
    await h.run_task("Implementa rotación de refresh tokens en el login")
    assert h.task.runtime == "claude" and h.task.level == 3
    assert "DeepSeek is not used for L3 work → continuing with Claude Code" in ui.text()
    assert "Earlier requests in this conversation\n- Hola" in claude.requests[0].prompt


def test_chat_json_retries_empty_content(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    replies = iter(["", '{"level": 1}'])
    monkeypatch.setattr(deepseek, "_request", lambda path, payload=None, timeout=15:
                        {"choices": [{"message": {"content": next(replies)}}]})
    assert deepseek.chat_json("s", "u") == {"level": 1}
