"""After an approved commit BOOST_AI asks whether to push; never pushes on its own."""

from __future__ import annotations

from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import UsageBook

from .conftest import FakeRuntime, ScriptedUI, git, writes


def setup_remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    return remote


def harness(repo, cfg, ui, tmp_path):
    cfg["routing"]["worktree_min_level"] = 3
    return Harness(repo, cfg, ui, {"codex": FakeRuntime("codex", [writes("a.py")])},
                   usage=UsageBook(tmp_path / "u.json"), metrics_path=tmp_path / "m.jsonl")


async def test_push_after_commit_when_user_agrees(repo, cfg, tmp_path):
    remote = setup_remote(repo, tmp_path)
    ui = ScriptedUI(["Yes", "<default>", "Commit", "Push"])
    await harness(repo, cfg, ui, tmp_path).run_task("Add pagination to the list endpoint")
    assert ui.questions[-1] == ("Push main to origin?", ["Push", "Not now"])
    assert git(remote, "log", "--oneline", "main").count("\n") == 2   # init + the new commit
    assert len([q for q, _ in ui.questions if "destructive" in q]) == 0  # no second confirmation


async def test_not_now_leaves_remote_untouched(repo, cfg, tmp_path):
    remote = setup_remote(repo, tmp_path)
    ui = ScriptedUI(["Yes", "<default>", "Commit", "Not now"])
    await harness(repo, cfg, ui, tmp_path).run_task("Add pagination to the list endpoint")
    assert git(remote, "branch") == ""
    assert "Not pushed" in ui.text()


async def test_existing_upstream_is_named(repo, cfg, tmp_path):
    setup_remote(repo, tmp_path)
    git(repo, "push", "-q", "-u", "origin", "main")
    ui = ScriptedUI(["Yes", "<default>", "Commit", "Not now"])
    await harness(repo, cfg, ui, tmp_path).run_task("Add pagination to the list endpoint")
    assert ui.questions[-1][0] == "Push main to its upstream?"


async def test_no_remote_no_question(repo, cfg, tmp_path):
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    await harness(repo, cfg, ui, tmp_path).run_task("Add pagination to the list endpoint")
    assert [q for q, _ in ui.questions][-1] == "Create this commit?"


async def test_offer_can_be_disabled(repo, cfg, tmp_path):
    setup_remote(repo, tmp_path)
    cfg["git"] = {"offer_push": False}
    ui = ScriptedUI(["Yes", "<default>", "Commit"])
    await harness(repo, cfg, ui, tmp_path).run_task("Add pagination to the list endpoint")
    assert [q for q, _ in ui.questions][-1] == "Create this commit?"
