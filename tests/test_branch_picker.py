"""Warp-style branch dropdown in the TUI."""

from __future__ import annotations

import asyncio

from textual.widgets import Input, OptionList

from boost_ai import gitops
from boost_ai.tui import app as tui_app
from boost_ai.tui.branches import BranchPicker

from .conftest import FakeRuntime, git


def labels(app) -> list[str]:
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def settle(pilot, check, tries=60):
    for _ in range(tries):
        await pilot.pause(0.05)
        if check():
            return
    raise AssertionError("condition not reached")


def make_app(repo, monkeypatch):
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    return tui_app.BoostApp(repo)


async def test_switch_branch_from_dropdown(repo, monkeypatch):
    git(repo, "branch", "feature/login")
    git(repo, "branch", "fix/typo")
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+b")
        await settle(pilot, lambda: isinstance(app.screen, BranchPicker))
        shown = labels(app)
        assert any(s.startswith("● main") for s in shown)
        assert any("feature/login" in s for s in shown) and any("fix/typo" in s for s in shown)
        await pilot.press(*"login")
        assert [s.strip() for s in labels(app)] == ["feature/login", '+ Create branch "login"']
        await pilot.press("enter")
        await settle(pilot, lambda: gitops.current_branch(repo) == "feature/login" and not app.worker.is_running)
        assert app.harness.task is None                     # the filter text never became a message
        assert app.query_one(tui_app.StatusBar).values["branch"] == "feature/login"


async def test_create_branch_from_dropdown(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+b")
        await settle(pilot, lambda: isinstance(app.screen, BranchPicker))
        await pilot.press(*"test/001")
        assert labels(app)[-1] == '+ Create branch "test/001"'
        await pilot.press("enter")
        await settle(pilot, lambda: gitops.current_branch(repo) == "test/001")


async def test_click_on_branch_opens_dropdown_and_escape_closes(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        bar = app.query_one(tui_app.StatusBar)
        for x in range(40, 100):  # find the branch cell on the first row of the bar
            await pilot.click(tui_app.StatusBar, offset=(x, 1))
            await pilot.pause(0.02)
            if isinstance(app.screen, BranchPicker):
                break
        assert isinstance(app.screen, BranchPicker), bar.values
        await pilot.press("escape")
        await settle(pilot, lambda: not isinstance(app.screen, BranchPicker))
        assert gitops.current_branch(repo) == "main"


async def test_remote_only_branches_are_listed(repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main:shared/feature")
    git(repo, "fetch", "-q", "origin")
    assert ("shared/feature", True) in gitops.branches(repo)
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+b")
        await settle(pilot, lambda: isinstance(app.screen, BranchPicker))
        assert any("shared/feature" in s and "remote" in s for s in labels(app))
        await pilot.press(*"shared")
        await pilot.press("enter")
        await settle(pilot, lambda: gitops.current_branch(repo) == "shared/feature")


async def test_dropdown_is_blocked_while_an_agent_works(repo, monkeypatch):
    started = asyncio.Event()

    class Slow(FakeRuntime):
        async def execute(self, req, progress):
            started.set()
            await asyncio.sleep(30)

    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": Slow("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        app.query_one("#prompt", Input).value = "Add pagination to the list endpoint"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 5)
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, BranchPicker)
        assert "switch branches when it finishes" in "\n".join(str(w.render()) for w in app.query(".msg"))
        app.worker.cancel()


async def test_click_outside_closes_dropdown(repo, monkeypatch):
    git(repo, "branch", "other")
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+b")
        await settle(pilot, lambda: isinstance(app.screen, BranchPicker))
        box = app.screen.query_one("#branch-picker").region
        await pilot.click(offset=(box.x + 6, box.y + 1))                     # inside (filter box): stays open
        await pilot.pause(0.1)
        assert isinstance(app.screen, BranchPicker)
        await pilot.click(offset=(5, 30))                                     # outside: closes
        await settle(pilot, lambda: not isinstance(app.screen, BranchPicker))
        assert gitops.current_branch(repo) == "main" and app.harness.task is None


async def test_click_on_branch_again_toggles_closed(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.click("#sb-branch")
        await settle(pilot, lambda: isinstance(app.screen, BranchPicker))
        branch = app.query_one("#sb-branch").region
        await pilot.click(offset=(branch.x + 1, branch.y))
        await settle(pilot, lambda: not isinstance(app.screen, BranchPicker))
