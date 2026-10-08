"""The plan step: read-only plan → the user approves, revises or stops → implementation."""

from __future__ import annotations

from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import PLAN_SCHEMA, ExecutionResult, UsageBook

from .conftest import FakeRuntime, ScriptedUI, done, git


def planned(plan="Paginate with cursors", skills=None, status="ready", tasks=None, **extra):
    final = {"status": status, "plan": plan, "tasks": tasks or [], "skills": skills or [], "blocker": "",
             "blocker_options": [], **extra}
    return lambda req: ExecutionResult(ok=True, final=final, duration=1, session_id="s-1")


def implements(filename="feature.py", message="feat(core): add thing"):
    def step(req):
        (req.cwd / filename).write_text("x\n")
        return ExecutionResult(ok=True, final=done(commit_message=message), duration=1, session_id="s-1")
    return step


def harness(repo, cfg, ui, rt, tmp_path):
    return Harness(repo, cfg, ui, {rt.name: rt}, usage=UsageBook(tmp_path / "usage.json"),
                   metrics_path=tmp_path / "metrics.jsonl")


def add_skill(harness, name):
    skill = harness / "skills" / "api" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} know-how\n---\nbody\n")


async def test_plan_is_approved_before_any_change(repo, cfg, tmp_path):
    add_skill(tmp_path / "harness", "pagination")
    add_skill(tmp_path / "harness", "unrelated")
    rt = FakeRuntime("codex", [planned(skills=["pagination", "made-up"]), implements()])
    ui = ScriptedUI(["Approve", "No"])
    task = await harness(repo, cfg, ui, rt, tmp_path).run_task("Add pagination to the list endpoint")

    plan_req, impl_req = rt.requests
    assert plan_req.read_only and plan_req.schema == PLAN_SCHEMA and "Plan first" in plan_req.prompt
    assert "unrelated" in plan_req.prompt                          # the planner sees every skill…
    assert ui.questions[0][0] == "Approve this plan?"
    assert "Paginate with cursors" in ui.text() and "**Skills:** `pagination`" in ui.text()  # unknown dropped
    assert any(kind == "markdown" and "# Plan" in text for kind, text in ui.said)   # rendered, not plain
    assert not impl_req.read_only and impl_req.resume_session == "s-1"              # same session, explored once
    assert "I approve your plan" in impl_req.prompt and "Paginate with cursors" in impl_req.prompt
    assert "pagination/SKILL.md" in impl_req.prompt and "unrelated" not in impl_req.prompt
    assert task.status == "DONE"


async def test_request_changes_revises_in_the_same_session(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned("v1"), planned("v2"), implements()])
    ui = ScriptedUI(["Request changes", "use cursor pagination", "Approve", "No"])
    await harness(repo, cfg, ui, rt, tmp_path).run_task("Add pagination to the list endpoint")
    revise = rt.requests[1]
    assert revise.read_only and revise.resume_session == "s-1"
    assert "use cursor pagination" in revise.prompt and "Do not implement anything yet" in revise.prompt
    assert "v2" in rt.requests[2].prompt


async def test_stop_at_plan_changes_nothing(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned()])
    ui = ScriptedUI(["Stop"])
    task = await harness(repo, cfg, ui, rt, tmp_path).run_task("Add pagination to the list endpoint")
    assert task.status == "STOPPED" and len(rt.requests) == 1


async def test_question_is_answered_by_the_plan_run(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned("It paginates in api.py", status="answer")])
    ui = ScriptedUI([])
    task = await harness(repo, cfg, ui, rt, tmp_path).run_task("How does the list endpoint paginate results?")
    assert task.status == "DONE" and "It paginates in api.py" in ui.text() and not ui.questions


async def test_blocked_plan_asks_then_replans(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned(status="blocked", blocker="Offset or cursor?"), planned(), implements()])
    ui = ScriptedUI(["cursor", "Approve", "No"])
    await harness(repo, cfg, ui, rt, tmp_path).run_task("Add pagination to the list endpoint")
    assert "Q: Offset or cursor?\nA: cursor" in rt.requests[1].prompt
    assert rt.requests[1].read_only


async def test_attached_spec_raises_level_and_gets_a_plan(repo, cfg, tmp_path):
    cfg["plan"] = {"min_level": 2, "attachments": True}
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n" + "The service must do many things. " * 80)
    rt = FakeRuntime("codex", [planned(), implements()])
    ui = ScriptedUI(["Approve", "No"])
    task = await harness(repo, cfg, ui, rt, tmp_path).run_task("implementa esto", files=[spec])
    assert task.level >= 2                       # the short message alone would be trivial-looking
    assert rt.requests[0].read_only and "spec.md" in rt.requests[0].prompt


async def test_small_turns_skip_the_plan(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [implements("README.md")])
    ui = ScriptedUI(["No"])
    await harness(repo, cfg, ui, rt, tmp_path).run_task("Fix typo in README")
    assert not rt.requests[0].read_only


async def test_each_task_is_committed_before_the_next(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned(tasks=["Add model", "Add endpoint"]),
                               implements("model.py", "feat(api): add model"),
                               implements("endpoint.py", "feat(api): add endpoint")])
    ui = ScriptedUI(["Approve", "Yes", "<default>", "Commit", "Yes", "<default>", "Commit"])
    h = harness(repo, cfg, ui, rt, tmp_path)
    await h.run_task("Add pagination to the list endpoint")
    log = git(repo, "log", "main", "--format=%s")
    assert log.splitlines()[:2] == ["feat(api): add endpoint", "feat(api): add model"]   # one commit per task
    assert "Task 1/2: Add model" in rt.requests[1].prompt and "Implement only this task" in rt.requests[1].prompt
    assert rt.requests[2].prompt.startswith("The user says:\nTask 2/2: Add endpoint")   # resumed, delta only
    assert "- ✓ Add model\n- ○ Add endpoint" in ui.text() and "- ✓ Add endpoint" in ui.text()
    assert not any("Push" in q for q, _ in ui.questions)                                 # never offered mid-plan
    assert "Pushing is up to you" in ui.text() and ui.fields["progress"] == "2/2 tasks"


async def test_auto_commit_commits_every_task_without_asking(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [planned(tasks=["Add model", "Add endpoint"]),
                               implements("model.py", "feat(api): add model"),
                               implements("endpoint.py", "feat(api): add endpoint")])
    ui = ScriptedUI(["Approve"])                       # the plan is the only question
    h = harness(repo, cfg, ui, rt, tmp_path)
    h.auto_commit = True
    task = await h.run_task("Add pagination to the list endpoint")
    assert [q for q, _ in ui.questions] == ["Approve this plan?"]
    assert git(repo, "log", "main", "--format=%s").splitlines()[:2] == ["feat(api): add endpoint",
                                                                          "feat(api): add model"]
    assert task.status == "DONE" and ui.fields["commits"] == "auto" and "2 commits" in ui.text()
    assert not git(repo, "remote").strip()             # nothing to push to, and nothing tried


async def test_stopping_a_task_stops_the_plan(repo, cfg, tmp_path):
    cfg["commands"] = {"test": "false"}
    rt = FakeRuntime("codex", [planned(tasks=["Add model", "Add endpoint"]),
                               implements("model.py"), implements("model.py")])
    ui = ScriptedUI(["Approve", "Stop"])
    h = harness(repo, cfg, ui, rt, tmp_path)
    h.auto_commit = True
    await h.run_task("Add pagination to the list endpoint")
    assert "Stopped at task 1/2" in ui.text()


def round_of(*questions, status="questions", spec="", title=""):
    final = {"status": status, "questions": [{"question": q, "recommended": "yes"} for q in questions],
             "title": title, "spec": spec}
    return lambda req: ExecutionResult(ok=True, final=final, duration=1, session_id="i-1")


async def test_interview_rounds_end_in_a_spec_without_touching_code(repo, cfg, tmp_path):
    from boost_ai.runtimes.base import INTERVIEW_SCHEMA
    rt = FakeRuntime("codex", [round_of("Offline first?", "Who logs in?"),
                               round_of("Sync conflicts: last write wins?"),
                               round_of(status="done", title="Offline sync", spec="## Goals\n- offline")])
    ui = ScriptedUI(["Answer", "1. yes\n2. only admins", "Accept all recommendations", "Later"])
    path = await harness(repo, cfg, ui, rt, tmp_path).interview("sincronización offline")
    first, second, third = rt.requests
    assert all(r.read_only and r.schema == INTERVIEW_SCHEMA for r in rt.requests)
    assert "grilling method" in first.prompt.lower() or "design tree" in first.prompt
    assert second.resume_session == "i-1" and "2. only admins" in second.prompt
    assert "Yes to all your recommendations" in third.prompt
    assert "❓ **Q2** Who logs in?" in ui.text()                       # rendered round
    assert path == repo / ".boost-ai" / "specs" / "T-0001-offline-sync.md"
    assert path.read_text().startswith("# Offline sync") and "- offline" in path.read_text()
    assert not git(repo, "status", "--porcelain", "--", ".", ":(exclude).boost-ai").strip()


async def test_interview_spec_can_be_planned_right_away(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [round_of(status="done", title="Offline sync", spec="## Goals\n- offline"),
                               planned(tasks=["Add queue"])])
    ui = ScriptedUI(["Plan it", "Stop"])
    h = harness(repo, cfg, ui, rt, tmp_path)
    await h.interview("sincronización offline")
    plan_req = rt.requests[1]
    assert plan_req.read_only and "T-0001-offline-sync.md" in plan_req.prompt   # the spec is attached
    assert h.task.id == "T-0002"                                               # its own conversation


async def test_write_the_spec_now_and_stop(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [round_of("Q?"), round_of(status="done", title="X", spec="body")])
    ui = ScriptedUI(["Write the spec now", "Later"])
    await harness(repo, cfg, ui, rt, tmp_path).interview("algo")
    assert "Stop asking. Write the spec now" in rt.requests[1].prompt
    rt2 = FakeRuntime("codex", [round_of("Q?")])
    ui2 = ScriptedUI(["Stop"])
    h2 = harness(repo, cfg, ui2, rt2, tmp_path)
    assert await h2.interview("otra") is None and h2.task.status == "STOPPED"
