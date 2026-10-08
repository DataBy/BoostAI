"""Independent cross-model review for high-risk tasks."""

from __future__ import annotations

import json

from boost_ai import gitops
from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import REVIEW_SCHEMA, ExecutionResult, UsageBook

from .conftest import FakeRuntime, ScriptedUI, writes

TASK = "Implement refresh token rotation for login"   # → L3 security


def verdict(v, *findings):
    return lambda req: ExecutionResult(ok=True, duration=1, input_tokens=50, output_tokens=5, final={
        "verdict": v, "summary": "looked at it",
        "findings": [{"severity": s, "file": f, "issue": i} for s, f, i in findings]})


def harness(repo, cfg, ui, runtimes, tmp_path):
    return Harness(repo, cfg, ui, {r.name: r for r in runtimes}, usage=UsageBook(tmp_path / "u.json"),
                   metrics_path=tmp_path / "m.jsonl")


async def test_high_risk_gets_read_only_review(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [writes("auth.py")])
    claude = FakeRuntime("claude", [verdict("approve", ("low", "auth.py", "naming"))])
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    task = await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task(TASK)
    assert task.level == 3 and task.commit
    req = claude.requests[0]
    assert req.read_only and req.schema == REVIEW_SCHEMA
    assert "independent reviewer" in req.prompt and "did it" in req.prompt   # implementer summary passed
    assert not codex.requests[0].read_only
    assert "approved" in ui.text() and "[LOW] auth.py: naming" in ui.text()
    rows = [json.loads(x) for x in (tmp_path / "m.jsonl").read_text().splitlines()]
    assert [r["role"] for r in rows] == ["implement", "review"]


async def test_changes_requested_fix_loop(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f fixed.txt"}
    codex = FakeRuntime("codex", [writes("fixed.txt"), writes("fixed2.txt")])
    claude = FakeRuntime("claude", [verdict("changes_requested", ("high", "auth.py", "reuse not detected"))])
    ui = ScriptedUI(["Fix with Codex", "Yes", "<default>", "Commit"])
    task = await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task(TASK)
    assert ui.questions[0] == ("The reviewer requested changes.", ["Fix with Codex", "Accept as is", "Stop"])
    assert "reuse not detected" in codex.prompts[1] and "independent reviewer" in codex.prompts[1]
    assert task.commit and task.attempts == 2
    page = next((repo / ".boost-ai/state/memory").rglob("T-0001-MS*.md")).read_text()
    assert "Review (Claude Code): [HIGH] auth.py: reuse not detected" in page


async def test_accept_as_is(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [writes("auth.py")])
    claude = FakeRuntime("claude", [verdict("changes_requested", ("high", "a", "b"))])
    ui = ScriptedUI(["Accept as is", "No"])
    task = await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task(TASK)
    assert task.status == "DONE" and len(codex.requests) == 1


async def test_low_risk_has_no_review(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [writes("x.py")])
    claude = FakeRuntime("claude", [])
    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task("Add pagination to the list endpoint")
    assert claude.requests == []


async def test_review_skipped_without_second_runtime(repo, cfg, tmp_path):
    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, [FakeRuntime("codex", [writes("auth.py")])], tmp_path).run_task(TASK)
    assert "review skipped" in ui.text()


async def test_reviewer_modifying_files_is_flagged(repo, cfg, tmp_path):
    def sneaky(req):
        (req.cwd / "sneaky.py").write_text("x\n")
        return verdict("approve")(req)

    ui = ScriptedUI(["No"])
    codex = FakeRuntime("codex", [writes("auth.py")])
    await harness(repo, cfg, ui, [codex, FakeRuntime("claude", [sneaky])], tmp_path).run_task(TASK)
    assert "modified files during a read-only review" in ui.text()
    assert not gitops.is_dirty(repo)   # main checkout untouched (work happened in the worktree)
