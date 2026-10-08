"""Visible attachments: tray chips, add/remove, sent with the message."""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from boost_ai.tui import app as tui_app
from boost_ai.tui import attachments as att
from boost_ai.tui.attachments import Chip, Tray, extract_paths

from .conftest import FakeRuntime


def make_files(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    (d / "spec.pdf").write_bytes(b"%PDF-1.4")
    (d / "data set.csv").write_text("a,b\n")
    return d / "spec.pdf", d / "data set.csv"


def test_extract_paths_from_drop_and_paste(tmp_path):
    pdf, csv = make_files(tmp_path)
    rest, files = extract_paths(f"revisa '{pdf}' y {str(csv).replace(' ', chr(92) + ' ')} por favor")
    assert files == [pdf, csv] and rest == "revisa y por favor"
    assert extract_paths("sin rutas /no/existe.pdf") == ("sin rutas /no/existe.pdf", [])


async def settle(pilot, check, tries=60):
    for _ in range(tries):
        await pilot.pause(0.05)
        if check():
            return
    raise AssertionError("condition not reached")


def chips(app):
    return [c.path.name for c in app.query(Chip)]


async def test_tray_add_remove_and_send(repo, tmp_path, monkeypatch):
    pdf, csv = make_files(tmp_path)
    from boost_ai.runtimes.base import ExecutionResult

    from .conftest import done
    rt = FakeRuntime("codex", [lambda req: ExecutionResult(ok=True, final=done(summary="visto"), duration=1)])
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = f"'{pdf}'"                               # drop onto the terminal
        await pilot.pause(0.05)
        assert chips(app) == ["spec.pdf"] and prompt.value == ""
        prompt.value = str(csv)                                  # paste a bare absolute path
        await pilot.pause(0.05)
        assert chips(app) == ["spec.pdf", "data set.csv"]
        prompt.focus()
        await pilot.press("backspace")                           # empty prompt → removes the last chip
        assert chips(app) == ["spec.pdf"]
        prompt.value = "ab"
        await pilot.press("end", "backspace")                    # with text, Backspace edits the text
        assert prompt.value == "a" and chips(app) == ["spec.pdf"]
        prompt.value = ""
        app.query_one(Tray).add([csv])
        await pilot.pause(0.05)
        await pilot.click(Chip)                                  # ✕ on the first chip
        await pilot.pause(0.05)
        assert chips(app) == ["data set.csv"]
        prompt.value = "Analiza estos datos"
        await pilot.press("enter")
        await settle(pilot, lambda: rt.requests and not app.worker.is_running)
        assert chips(app) == [] and app.query_one(Tray).has_class("-empty")
        assert "## Attached files" in rt.requests[0].prompt and "data set.csv" in rt.requests[0].prompt


async def test_attach_command_and_picker(repo, tmp_path, monkeypatch):
    pdf, csv = make_files(tmp_path)
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})

    async def picked():
        return [csv]

    monkeypatch.setattr(tui_app, "pick_files", picked)
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = f"/attach {pdf}"
        await pilot.press("enter")
        assert chips(app) == ["spec.pdf"]
        await pilot.press("ctrl+o")                              # native picker (mocked)
        await settle(pilot, lambda: len(chips(app)) == 2)
        await pilot.click("#attach-btn")                         # ＋ button: same file again → no duplicate
        await pilot.pause(0.2)
        assert chips(app) == ["spec.pdf", "data set.csv"]


async def test_no_picker_explains_alternatives(repo, monkeypatch):
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})

    async def none():
        return None

    monkeypatch.setattr(tui_app, "pick_files", none)
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+o")
        await pilot.pause(0.2)
        assert "Drop a file onto the terminal" in "\n".join(str(w.render()) for w in app.query(".msg"))


async def test_zenity_cancel_returns_empty(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(att.shutil, "which", lambda name: "/usr/bin/zenity")

    class Proc:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await att.pick_files() == []


async def test_drop_lands_in_tray_whatever_was_clicked(repo, tmp_path, monkeypatch):
    """Regression: drag-and-drop arrives as a paste to the focused widget; it must never be lost."""
    from textual import events
    pdf, _ = make_files(tmp_path)
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})

    async def cancelled():
        return []

    monkeypatch.setattr(tui_app, "pick_files", cancelled)
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.click("#attach-btn")
        await pilot.click("#log")
        await pilot.pause(0.1)
        assert app.focused.id == "prompt"                       # ＋ and the conversation never steal focus
        app.focused.post_message(events.Paste(f"'{pdf}' "))
        await pilot.pause(0.1)
        assert chips(app) == ["spec.pdf"]
        app.screen.post_message(events.Paste(f"'{pdf}'"))         # a paste that reaches the screen/app
        await pilot.pause(0.1)
        assert chips(app) == ["spec.pdf"]                         # handled, no duplicate


async def test_typing_with_focus_elsewhere_goes_to_prompt(repo, monkeypatch):
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        app.set_focus(None)
        await pilot.press("h", "o", "l", "a")
        assert app.query_one("#prompt", Input).value == "hola" and app.focused.id == "prompt"
