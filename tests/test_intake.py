from __future__ import annotations

import pytest

from boost_ai.intake import bang_command
from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import UsageBook

from .conftest import ScriptedUI, git


@pytest.mark.parametrize("text,command", [("!git status", "git status"), ("  ! ls -la ", "ls -la"),
                                          ("!", None), ("git status", None), ("Hola", None),
                                          ("hazle push", None)])
def test_only_explicit_bang_bypasses_the_agent(text, command):
    assert bang_command(text) == command


def harness(repo, cfg, ui, tmp_path):
    return Harness(repo, cfg, ui, {}, usage=UsageBook(tmp_path / "u.json"), metrics_path=tmp_path / "m")


async def test_safe_command_runs_directly(repo, cfg, tmp_path):
    ui = ScriptedUI()
    code = await harness(repo, cfg, ui, tmp_path).run_command("git switch -c test/001")
    assert code == 0 and ui.questions == []
    assert git(repo, "branch", "--show-current").strip() == "test/001"
    assert ui.fields["branch"] == "test/001"                      # status bar refreshed


async def test_dangerous_command_is_confirmed(repo, cfg, tmp_path):
    ui = ScriptedUI(["Cancel"])
    assert await harness(repo, cfg, ui, tmp_path).run_command("git push origin main") is None
    assert "push to remote" in ui.questions[0][0] and ui.questions[0][1] == ["Cancel", "Run anyway"]


async def test_force_push_gets_strong_warning(repo, cfg, tmp_path):
    ui = ScriptedUI(["Cancel"])
    await harness(repo, cfg, ui, tmp_path).run_command("git push --force")
    assert "REWRITES REMOTE HISTORY" in ui.questions[0][0] and ui.questions[0][1] == ["Cancel", "Run force push"]


async def test_ambiguous_command_asks(repo, cfg, tmp_path):
    ui = ScriptedUI(["Cancel"])
    assert await harness(repo, cfg, ui, tmp_path).run_command("make test", confirm=True) is None
    assert ui.questions[0][1] == ["Run", "Cancel"]


async def test_failing_command_reports_exit_code(repo, cfg, tmp_path):
    ui = ScriptedUI()
    assert await harness(repo, cfg, ui, tmp_path).run_command("git switch no-such-branch") != 0
    assert "(exit status" in ui.text()


async def test_command_never_waits_for_input(repo, cfg, tmp_path):
    ui = ScriptedUI()
    code = await harness(repo, cfg, ui, tmp_path).run_command("cat")   # would block on stdin
    assert code == 0


async def test_push_sets_upstream_for_new_branch(repo, cfg, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "switch", "-q", "-c", "test/001")
    ui = ScriptedUI(["Run anyway"])
    assert await harness(repo, cfg, ui, tmp_path).run_command("git push") == 0
    assert "git push -u origin test/001" in ui.questions[0][0]
    assert "test/001" in git(remote, "branch")


async def test_push_without_remote_explains(repo, cfg, tmp_path):
    ui = ScriptedUI()
    assert await harness(repo, cfg, ui, tmp_path).run_command("git push") is None
    assert "no remote" in ui.text() and ui.questions == []
