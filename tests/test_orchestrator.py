"""End-to-end harness flow with scripted runtimes and a scripted user."""

from __future__ import annotations

import json

from boost_ai import gitops
from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import ErrorKind, Usage, UsageBook

from .conftest import FakeRuntime, ScriptedUI, done, fails, git, writes


def harness(repo, cfg, ui, runtimes, tmp_path, memory=None):
    return Harness(repo, cfg, ui, {r.name: r for r in runtimes}, usage=UsageBook(tmp_path / "usage.json"),
                   memory=memory, metrics_path=tmp_path / "metrics.jsonl")


async def test_happy_path_worktree_commit_memory_metrics(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f feature.py"}
    rt = FakeRuntime("codex", [writes("feature.py", final=done(
        commit_message="feat(core): add feature\n\nCo-Authored-By: Claude <x@y>", decisions=["kept it small"]))])
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    h = harness(repo, cfg, ui, [rt], tmp_path)

    task = await h.run_task("Add pagination to the list endpoint")

    assert task.status == "DONE" and task.level == 2 and task.worktree
    assert [q for q, _ in ui.questions] == ["Run git add . ?", "Commit message", "Create this commit?"]
    log = git(repo, "log", "main", "-1", "--format=%an%n%B")             # landed on the user's branch
    assert "feat(core): add feature" in log and "Co-Authored" not in log and "Test User" in log
    assert (repo / "feature.py").exists()                                  # fast-forwarded into the checkout
    assert "boost/" not in git(repo, "branch")                             # no harness branches
    assert (repo / ".boost-ai" / "worktrees" / "T-0001").exists()         # kept while the conversation lasts
    h.reset()                                                              # /new
    assert not (repo / ".boost-ai" / "worktrees" / "T-0001").exists()     # clean worktree removed, branch kept
    assert "feature.py" not in git(repo, "status", "--porcelain")
    page = (repo / ".boost-ai" / "state" / "memory" / repo.name / "T-0001" / "T-0001-MS01.md").read_text()
    assert "kept it small" in page and "feature.py" in page and task.commit in page
    row = json.loads((tmp_path / "metrics.jsonl").read_text().splitlines()[0])
    assert row["runtime"] == "codex" and row["success"] and row["input_tokens"] == 100
    assert "push" not in ui.text().lower().replace("nothing was pushed", "")


async def test_declining_git_add_creates_no_commit(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [writes("feature.py")])
    ui = ScriptedUI(["No"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert task.commit is None and task.status == "DONE"
    wt = repo / ".boost-ai" / "worktrees" / "T-0001"
    assert (wt / "feature.py").exists()                       # work preserved for inspection
    assert git(wt, "log", "--oneline").count("\n") == 1       # no commit
    assert "?? feature.py" in git(wt, "status", "--porcelain")  # not staged


async def test_cancel_commit_leaves_changes_staged(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [writes("feature.py")])
    ui = ScriptedUI(["Yes", "<default>", "Cancel"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    wt = repo / ".boost-ai" / "worktrees" / "T-0001"
    assert task.commit is None
    assert "A  feature.py" in git(wt, "status", "--porcelain")


async def test_edit_commit_message(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [writes("feature.py")])
    ui = ScriptedUI(["Yes", "<default>", "Edit", "fix(api): better message", "Commit"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert git(repo, "log", "main", "-1", "--format=%s").strip() == "fix(api): better message"
    assert task.commit


async def test_trivial_task_runs_in_place(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [writes("README.md", "hello world\n")])
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Fix typo in README")
    assert task.level == 0 and not task.worktree and task.branch == "main"
    assert git(repo, "log", "--oneline").count("\n") == 2


async def test_verification_retry_then_success(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f good.txt"}
    rt = FakeRuntime("codex", [writes("bad.txt"), writes("good.txt")])
    ui = ScriptedUI(["No"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert task.attempts == 2 and task.status == "DONE"
    assert "Feedback on the current changes" in rt.prompts[1] and "Verification failed" in rt.prompts[1]
    assert "test -f good.txt" in rt.prompts[1]


async def test_escalation_requires_user_and_switches(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "test -f good.txt"}
    codex = FakeRuntime("codex", [writes("bad.txt"), writes("bad2.txt")])
    claude = FakeRuntime("claude", [writes("good.txt")])
    ui = ScriptedUI(["Escalate to Claude Code", "No"])
    task = await harness(repo, cfg, ui, [codex, claude], tmp_path).run_task("Add pagination to the list endpoint")
    question, options = ui.questions[0]
    assert "failed verification 2 times" in question
    assert options == ["Escalate to Claude Code", "Retry Codex", "Stop"]
    assert task.runtime == "claude" and task.runtimes_tried == ["codex", "claude"] and task.status == "DONE"
    rows = [json.loads(x) for x in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    assert rows[-1]["escalated_from"] == "codex" and rows[-1]["success"]


async def test_stop_after_failed_verification(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "false"}
    rt = FakeRuntime("codex", [writes("a.txt"), writes("b.txt")])
    ui = ScriptedUI(["Stop"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert task.status == "STOPPED"
    assert ui.questions[0][1] == ["Retry Codex", "Stop"]   # no escalation target → not offered


async def test_rate_limit_never_switches_silently(repo, cfg, tmp_path):
    codex = FakeRuntime("codex", [fails(ErrorKind.RATE_LIMIT, "You've hit your usage limit.")])
    claude = FakeRuntime("claude", [writes("x.py")])
    ui = ScriptedUI(["Switch to Claude Code", "No"])
    usage = UsageBook(tmp_path / "usage.json")
    h = harness(repo, cfg, ui, [codex, claude], tmp_path)
    task = await h.run_task("Add pagination to the list endpoint")
    question, options = ui.questions[0]
    assert question == "Recommended fallback: Claude Code"
    assert "Retry" not in options and "Switch to Claude Code" in options and "Stop" in options
    assert "usage limit" in ui.text()
    assert usage.get("codex") == (Usage.EXHAUSTED, True)
    assert task.runtime == "claude" and task.status == "DONE"


async def test_crash_offers_retry_and_stop(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [fails(ErrorKind.CRASH, "The CLI exited with status 1.")])
    ui = ScriptedUI(["Inspect logs", "Stop"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert ui.questions[0][1] == ["Retry", "Inspect logs", "Stop"]
    assert "stopped unexpectedly" in ui.text() and "Logs:" in ui.text()
    assert task.status == "STOPPED"


async def test_blocked_runtime_interviews_user(repo, cfg, tmp_path):
    blocked = done(status="blocked", blocker="Should reused refresh tokens revoke the whole family?")
    rt = FakeRuntime("codex", [lambda req: __import__("boost_ai.runtimes.base", fromlist=["x"])
                               .ExecutionResult(ok=True, final=blocked, duration=1),
                               writes("auth.py")])
    ui = ScriptedUI(["Yes, revoke the family", "No"])
    task = await harness(repo, cfg, ui, [rt], tmp_path).run_task("Implement refresh token rotation")
    assert task.level == 3
    assert ui.questions[0][0] == "Should reused refresh tokens revoke the whole family?"
    assert "A: Yes, revoke the family" in rt.prompts[1]
    assert task.status == "DONE"


async def test_memory_failure_does_not_fail_task(repo, cfg, tmp_path):
    class Broken:
        async def write_implementation(self, record):
            raise OSError("disk full")

        async def search_context(self, query, limit=5):
            return []

    rt = FakeRuntime("codex", [writes("feature.py")])
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    task = await harness(repo, cfg, ui, [rt], tmp_path, memory=Broken()).run_task("Add pagination to list")
    assert task.status == "DONE" and task.commit
    assert any(k == "warning" and "Memory could not be saved" in t for k, t in ui.said)


async def test_no_runtime_available(repo, cfg, tmp_path):
    ui = ScriptedUI()
    task = await harness(repo, cfg, ui, [], tmp_path).run_task("Add pagination")
    assert task.status == "FAILED" and "boost-ai doctor" in ui.text()


async def test_unverified_change_is_reported(repo, cfg, tmp_path):
    cfg["commands"] = {}
    rt = FakeRuntime("codex", [writes("feature.py")])
    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, [rt], tmp_path).run_task("Add pagination to the list endpoint")
    assert any(k == "warning" and "NOT automatically verified" in t for k, t in ui.said)


async def test_runtime_commit_is_flagged(repo, cfg, tmp_path):
    def sneaky(req):
        (req.cwd / "f.py").write_text("x\n")
        gitops.add_all(req.cwd)
        gitops.commit(req.cwd, "sneaky")
        (req.cwd / "g.py").write_text("y\n")
        from boost_ai.runtimes.base import ExecutionResult
        return ExecutionResult(ok=True, final=done(), duration=1)

    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, [FakeRuntime("codex", [sneaky])], tmp_path).run_task("Add pagination to list")
    assert "created a git commit on its own" in ui.text()


async def test_conflicting_commit_is_kept_in_the_worktree_not_lost(repo, cfg, tmp_path):
    def step(req):
        (req.cwd / "README.md").write_text("from agent\n")
        (repo / "README.md").write_text("from user\n")                     # the user commits meanwhile
        git(repo, "commit", "-qam", "docs: mine")
        return ExecutionResult(ok=True, final=done(), duration=1)
    from boost_ai.runtimes.base import ExecutionResult
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    h = harness(repo, cfg, ui, [FakeRuntime("codex", [step])], tmp_path)
    task = await h.run_task("Add pagination to the list endpoint")
    assert task.unlanded and "!git cherry-pick" in ui.text()
    assert git(repo, "log", "-1", "--format=%s").strip() == "docs: mine" and not gitops.is_dirty(repo)
    h.reset()
    assert (repo / ".boost-ai" / "worktrees" / "T-0001").exists()        # kept: its commit is not landed
