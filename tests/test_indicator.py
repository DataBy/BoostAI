from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Input

from boost_ai import indicator
from boost_ai.tui import app as tui_app

from .conftest import FakeRuntime

REAL_LAUNCH = indicator.launch  # captured at import, before the autouse fixture patches it


@pytest.mark.parametrize("state,label", [
    ({}, "BOOST_AI"),
    ({"status": "IDLE"}, "BOOST_AI"),
    ({"status": "RUNNING", "model": "Codex"}, "BOOST_AI · CODEX · RUNNING"),
    ({"status": "VERIFYING", "model": "Claude Code"}, "BOOST_AI · CLAUDE CODE · VERIFYING"),
    ({"status": "RUNNING", "model": "—"}, "BOOST_AI · RUNNING"),
    ({"status": "NEEDS_YOU", "model": "Codex"}, "BOOST_AI · NEEDS YOU"),
    ({"status": "DONE"}, "BOOST_AI · DONE"),
])
def test_labels(state, label):
    assert indicator.label_for(state) == label


def test_icons_and_menu_lines():
    assert indicator.icon_for({"status": "NEEDS_YOU"}) == "dialog-warning-symbolic"
    assert indicator.icon_for({}) == indicator.IDLE_ICON
    lines = indicator.info_lines({"project": "FASR", "task": "T-0003", "title": "x" * 60,
                                  "model": "Codex", "status": "NEEDS_YOU"})
    assert lines[0] == "Project: FASR" and lines[1].startswith("Task: T-0003 · ") and lines[1].endswith("…")
    assert lines[3] == "Status: NEEDS YOU"


def test_control_requests(tmp_path):
    assert indicator.take_request(tmp_path) is None
    indicator.request(tmp_path, "stop")
    assert indicator.take_request(tmp_path) == "stop"
    assert indicator.take_request(tmp_path) is None          # consumed
    indicator.request(tmp_path, "rm -rf /")
    assert indicator.take_request(tmp_path) is None          # unknown actions ignored


def test_launch_respects_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert REAL_LAUNCH(tmp_path, {"indicator": {"enabled": False}}) is None
    assert REAL_LAUNCH(tmp_path, {"indicator": {"python": str(tmp_path / "nope")}}) is None
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert REAL_LAUNCH(tmp_path, {}) is None                 # no graphical session


def test_script_runs_without_gi(tmp_path):
    """With a Python lacking PyGObject the indicator exits quietly (code 3)."""
    code = subprocess.run([sys.executable, "-I", indicator.__file__, "--root", str(tmp_path),
                           "--parent-pid", str(os.getpid())], capture_output=True, timeout=30).returncode
    assert code == 3


async def test_tui_obeys_stop_and_quit(repo, monkeypatch):
    import asyncio

    started = asyncio.Event()

    class Slow(FakeRuntime):
        async def execute(self, req, progress):
            started.set()
            await asyncio.sleep(30)

    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": Slow("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "Add pagination to the list endpoint"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 5)
        indicator.request(repo, "stop")
        for _ in range(50):
            await pilot.pause(0.05)
            if not app.worker.is_running:
                break
        assert not app.worker.is_running
        assert json.loads((repo / ".boost-ai/state/current.json").read_text())["status"] == "STOPPED"
        indicator.request(repo, "quit")
        for _ in range(50):
            await pilot.pause(0.05)
            if not app.is_running:
                break
    assert not Path(repo / ".boost-ai/state/control").exists()
