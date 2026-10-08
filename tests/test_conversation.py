"""A task is a conversation: turns, guarantees triggered by changes, models per level."""

from __future__ import annotations

from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import ErrorKind, ExecutionResult, UsageBook

from .conftest import FakeRuntime, ScriptedUI, done, git


def turn(summary="ok", write=None, session="s-1", **final):
    def run(req):
        if write:
            (req.cwd / write).write_text(f"{write}\n")
        return ExecutionResult(ok=True, final=done(summary=summary, **final), duration=1, session_id=session)
    return run


def harness(repo, cfg, ui, rts, tmp_path):
    cfg["routing"]["worktree_min_level"] = 3  # shipped default: L2 conversations work in your checkout
    return Harness(repo, cfg, ui, {r.name: r for r in rts}, usage=UsageBook(tmp_path / "u.json"),
                   metrics_path=tmp_path / "m.jsonl")


async def test_answer_only_turn_has_no_ceremony(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "false"}           # would fail if it ran
    rt = FakeRuntime("codex", [turn("Este repo es un servicio de pagos.")])
    ui = ScriptedUI()
    h = harness(repo, cfg, ui, [rt], tmp_path)
    task = await h.run_task("¿Qué hace este repo?")
    assert task.status == "DONE" and ui.questions == []
    assert "Este repo es un servicio de pagos." in ui.text()
    assert h.last_report is None                  # no verification for a pure answer
    assert not (repo / ".boost-ai/state/memory").exists()


async def test_turns_share_one_session_and_task(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [turn("hola"), turn("hecho", write="a.py")])
    ui = ScriptedUI(["No"])
    h = harness(repo, cfg, ui, [rt], tmp_path)
    await h.run_task("Hola")
    task = await h.run_task("Agrega a.py")
    assert task.id == "T-0001" and task.turns == 2 and task.messages == ["Hola", "Agrega a.py"]
    second = rt.requests[1]
    assert second.resume_session == "s-1" and "The user says:\nAgrega a.py" in second.prompt
    assert "Hola" not in second.prompt            # delta only


async def test_only_agent_files_are_staged(repo, cfg, tmp_path):
    (repo / "mine.txt").write_text("my own work in progress\n")
    rt = FakeRuntime("codex", [turn(write="agent.py")])
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add agent.py")
    assert "Stage the 1 files changed by the agent? (1 other uncommitted files" in ui.questions[0][0]
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["agent.py"]
    assert "?? mine.txt" in git(repo, "status", "--porcelain")
    assert task.pending == []


async def test_declined_changes_are_offered_again(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [turn(write="one.py"), turn("solo respondo"), turn(write="two.py")])
    ui = ScriptedUI(["No", "Yes", "<default>", "Commit"])
    h = harness(repo, cfg, ui, [rt], tmp_path)
    await h.run_task("crea one.py")
    assert h.task.pending == ["one.py"]
    await h.run_task("¿qué hiciste?")              # no change → no commit question
    assert len(ui.questions) == 1
    await h.run_task("crea two.py")
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").split()) == ["one.py", "two.py"]
    assert h.task.pending == []


async def test_level_rises_and_risky_turn_gets_review(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [turn("ok"), turn(write="auth.py")])
    claude = FakeRuntime("claude", [lambda req: ExecutionResult(
        ok=True, duration=1, final={"verdict": "approve", "summary": "fine", "findings": []})])
    ui = ScriptedUI(["No"])
    h = harness(repo, cfg, ui, [codex, claude], tmp_path)
    await h.run_task("Explícame la estructura del proyecto")
    assert h.task.level == 2 and claude.requests == []
    await h.run_task("Ahora implementa rotación de refresh tokens en el login")
    assert h.task.level == 3 and "security" in h.task.risks
    assert claude.requests and claude.requests[0].read_only


async def test_model_per_level_and_escalation_to_stronger_model(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f good.txt"}
    cfg["runtimes"]["codex"]["models"] = {"L2": "small-model", "L4": "big-model"}
    rt = FakeRuntime("codex", [turn(write="bad1"), turn(write="bad2"), turn(write="good.txt")])
    ui = ScriptedUI(["Use big-model", "No"])
    await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert [r.model for r in rt.requests] == ["small-model", "small-model", "big-model"]
    assert ui.questions[0][1][0] == "Use big-model"


async def test_fresh_session_gets_the_conversation_history(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [turn("ok"), lambda req: ExecutionResult(
        ok=False, error_kind=ErrorKind.RATE_LIMIT, error="usage limit", duration=1)])
    claude = FakeRuntime("claude", [turn("hecho", session="c-1")])
    ui = ScriptedUI(["Switch to Claude Code"])
    h = harness(repo, cfg, ui, [codex, claude], tmp_path)
    await h.run_task("Primero revisa el README")
    await h.run_task("Ahora resume lo que encontraste")
    prompt = claude.requests[0].prompt
    assert claude.requests[0].resume_session is None
    assert "Earlier requests in this conversation\n- Primero revisa el README" in prompt
    assert "## The user says\nAhora resume lo que encontraste" in prompt


async def test_new_conversation_starts_a_new_task(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [turn("uno"), turn("dos", session="s-2")])
    ui = ScriptedUI()
    h = harness(repo, cfg, ui, [rt], tmp_path)
    await h.run_task("primera")
    h.reset()
    task = await h.run_task("segunda")
    assert task.id == "T-0002" and rt.requests[1].resume_session is None


async def test_dirty_worktree_is_kept_on_reset(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [turn(write="auth.py")])
    ui = ScriptedUI(["No"])
    h = harness(repo, cfg, ui, [rt], tmp_path)
    await h.run_task("Implement refresh token rotation for login")   # L3 → worktree
    wt = repo / ".boost-ai" / "worktrees" / "T-0001"
    assert wt.is_dir()
    h.reset()
    assert wt.is_dir() and (wt / "auth.py").exists()                  # uncommitted work is never lost
