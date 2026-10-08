"""Transcripts, history, /new and /open, local intake in the TUI, redaction."""

from __future__ import annotations

import subprocess

from textual.widgets import Input

from boost_ai.orchestrator import Harness
from boost_ai.redact import redact
from boost_ai.runtimes.base import UsageBook
from boost_ai.task import TaskStore
from boost_ai.tui import app as tui_app

from .conftest import FakeRuntime, ScriptedUI, done, writes


def test_redact():
    assert redact("DEEPSEEK_API_KEY=sk-abcdef1234567890abcdef") == "DEEPSEEK_API_KEY=[REDACTED]"
    assert redact("Authorization: Bearer abcdefghijklmnop1234") == "Authorization: Bearer [REDACTED]"
    assert "ghp_" not in redact("token ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert redact("refresh tokens rotate on every use") == "refresh tokens rotate on every use"


async def test_transcript_records_conversation_redacted(repo, cfg, tmp_path):
    rt = FakeRuntime("codex", [writes("a.py", final=done(summary="used password=supersecret123"))])
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"codex": rt}, usage=UsageBook(tmp_path / "u.json"), metrics_path=tmp_path / "m")
    await h.run_task("Add pagination; key sk-abcdef1234567890abcdef")
    entries = TaskStore(repo).transcript("T-0001")
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "user" and "question" in kinds and kinds.count("user") == 2
    text = "\n".join(e["text"] for e in entries)
    assert "Run git add . ?" in text and "No" in text
    assert "sk-abcdef" not in text and "supersecret123" not in text and "[REDACTED]" in text


def text_of(app) -> str:
    return "\n".join(getattr(w, "source", None) or str(w.render()) for w in app.query(".msg"))  # Markdown: source


async def submit(pilot, app, text):
    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")
    await pilot.pause(0.05)


async def test_every_message_goes_to_the_agent_and_continues_the_conversation(repo, monkeypatch):
    from boost_ai.runtimes.base import ExecutionResult

    def reply(text):
        return lambda req: ExecutionResult(ok=True, final=done(summary=text), duration=1, session_id="s-1")

    rt = FakeRuntime("codex", [reply("¡Hola! ¿En qué trabajamos?"), reply("Rama test/001 creada.")])
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})
    app = tui_app.BoostApp(repo)
    async with app.run_test() as pilot:
        await submit(pilot, app, "Hola")
        for _ in range(40):
            await pilot.pause(0.05)
            if not app.worker.is_running:
                break
        await submit(pilot, app, "crea una rama test/001")
        for _ in range(40):
            await pilot.pause(0.05)
            if not app.worker.is_running:
                break
        assert "¡Hola! ¿En qué trabajamos?" in text_of(app) and "Rama test/001 creada." in text_of(app)
        assert [r.resume_session for r in rt.requests] == [None, "s-1"]       # one conversation, resumed
        assert app.harness.task.id == "T-0001" and app.harness.task.turns == 2
    assert not (repo / ".boost-ai/state/tasks/T-0002.json").exists()


async def test_no_commits_warning(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    app = tui_app.BoostApp(root)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert "no commits yet" in text_of(app) and "git commit" in text_of(app)


async def test_history_open_and_new(repo, cfg, monkeypatch, tmp_path):
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"codex": FakeRuntime("codex", [writes("a.py")])},
                usage=UsageBook(tmp_path / "u.json"), metrics_path=tmp_path / "m")
    await h.run_task("Add pagination to the list endpoint")

    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert "Last task: T-0001 · DONE · Add pagination" in text_of(app)
        await submit(pilot, app, "/history")
        assert "T-0001" in text_of(app) and "DONE" in text_of(app)
        await submit(pilot, app, "/open t-0001")
        shown = text_of(app)
        assert "── T-0001 · DONE" in shown and "› Add pagination to the list endpoint" in shown
        assert "Run git add . ?" in shown and "read-only" in shown
        await submit(pilot, app, "/open T-9999")
        assert "Usage: /open" in text_of(app)
        await submit(pilot, app, "/new")
        assert "New conversation" in text_of(app) and "T-0001" not in text_of(app)
        assert app.query_one(tui_app.StatusBar).values["task"] == "—"


async def test_clear_only_clears_the_screen(repo, monkeypatch):
    from boost_ai.runtimes.base import ExecutionResult

    rt = FakeRuntime("codex", [lambda req: ExecutionResult(ok=True, final=done(summary="uno"), duration=1,
                                                           session_id="s-1"),
                               lambda req: ExecutionResult(ok=True, final=done(summary="dos"), duration=1)])
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})
    app = tui_app.BoostApp(repo)
    async with app.run_test() as pilot:
        await submit(pilot, app, "primera pregunta")
        for _ in range(40):
            await pilot.pause(0.05)
            if not app.worker.is_running:
                break
        assert "uno" in text_of(app)
        await submit(pilot, app, "clear")
        assert text_of(app) == "" and app.harness.task.id == "T-0001"        # conversation intact
        await submit(pilot, app, "segunda")
        for _ in range(40):
            await pilot.pause(0.05)
            if not app.worker.is_running:
                break
        assert rt.requests[1].resume_session == "s-1"                        # same agent session
        await pilot.press("ctrl+l")
        await pilot.pause(0.05)
        assert text_of(app) == ""


async def test_clear_keeps_a_pending_question(repo, monkeypatch):
    import asyncio

    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test() as pilot:
        app.say("old output", "info")
        question = asyncio.ensure_future(app.choose("Run git add . ?", ["Yes", "No"]))
        await pilot.pause(0.1)
        await pilot.press("ctrl+l")
        await pilot.pause(0.05)
        assert "old output" not in text_of(app)
        assert app._pending_widget is not None and app._pending_widget.is_mounted
        app._resolve("No")
        assert await question == "No"
