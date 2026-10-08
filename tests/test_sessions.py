"""Session resume, blocker options and model display."""

from __future__ import annotations

from boost_ai.orchestrator import STOP_TASK, Harness
from boost_ai.runtimes.base import ErrorKind, ExecutionResult, UsageBook

from .conftest import FakeRuntime, ScriptedUI, done


def step(final=None, session="s-1", model="gpt-x", write=None, ok=True, kind=None):
    def run(req):
        if write:
            (req.cwd / write).write_text("x\n")
        if not ok:
            return ExecutionResult(ok=False, error_kind=kind, error="boom", duration=1, session_id=session)
        return ExecutionResult(ok=True, final=final or done(), duration=1, session_id=session, model=model,
                               input_tokens=30000, cached_tokens=15000, output_tokens=40)
    return run


def harness(repo, cfg, ui, rts, tmp_path):
    return Harness(repo, cfg, ui, {r.name: r for r in rts}, usage=UsageBook(tmp_path / "u.json"),
                   metrics_path=tmp_path / "m.jsonl")


BLOCKED = done(status="blocked", blocker="Delete old tokens immediately?", blocker_options=["Yes", "No"])


async def test_answer_resumes_session_with_delta_only(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [step(BLOCKED), step(write="a.py")])
    ui = ScriptedUI(["Yes", "No"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert ui.questions[0] == ("Delete old tokens immediately?", ["Yes", "No", STOP_TASK])
    first, second = rt.requests
    assert first.resume_session is None and second.resume_session == "s-1"
    assert "A: Yes" in second.prompt and "Add pagination" not in second.prompt   # delta only
    assert task.status == "DONE"
    assert "Codex (gpt-x) finished in" in ui.text() and "· 40 output tokens" in ui.text()
    assert "cached" not in ui.text() and "30,000" not in ui.text()   # only output tokens are shown
    assert "resumed" in ui.text()
    assert ui.fields["model"] == "Codex · gpt-x"


async def test_verification_retry_resumes(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f good.txt"}
    rt = FakeRuntime("codex", [step(write="bad.txt"), step(write="good.txt")])
    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert rt.requests[1].resume_session == "s-1"
    assert "Verification failed" in rt.requests[1].prompt and "Add pagination" not in rt.requests[1].prompt


async def test_failed_session_is_not_resumed(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [step(ok=False, kind=ErrorKind.CRASH), step(write="a.py")])
    ui = ScriptedUI(["Retry", "No"])
    await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert rt.requests[1].resume_session is None and "Add pagination" in rt.requests[1].prompt


async def test_stop_button_on_blocker(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [step(BLOCKED)])
    ui = ScriptedUI([STOP_TASK])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert task.status == "STOPPED" and len(rt.requests) == 1


async def test_escalation_starts_fresh_session(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f good.txt"}
    codex = FakeRuntime("codex", [step(write="b1"), step(write="b2")])
    claude = FakeRuntime("claude", [step(write="good.txt", session="c-1", model="opus-x")])
    ui = ScriptedUI(["Escalate to Claude Code", "No"])
    await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task("Add pagination to the list endpoint")
    assert claude.requests[0].resume_session is None and "Add pagination" in claude.requests[0].prompt
