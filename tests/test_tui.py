"""Drive the real TUI headlessly: task → approval buttons → commit editor → commit."""

from __future__ import annotations

from textual.widgets import Button, Input, TextArea

from boost_ai.tui import app as tui_app

from .conftest import FakeRuntime, git, writes


async def _wait_for(pilot, predicate, tries=100):
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


async def test_full_flow_in_tui(repo, monkeypatch):
    (repo / ".boost-ai").mkdir(exist_ok=True)
    (repo / ".boost-ai" / "config.yaml").write_text(
        "commands:\n  test: test -f feature.py\nnotifications: {enabled: false}\nsounds: {enabled: false}\n")
    rt = FakeRuntime("codex", [writes("feature.py")])
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})

    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        assert "█▄▄ █▀█ █▀█" in str(app.query_one("#logo").render())
        status = app.query_one(tui_app.StatusBar).values
        assert status["project"] == repo.name and status["status"] == "IDLE"

        app.query_one("#prompt", Input).value = "Add pagination to the list endpoint"
        await pilot.press("enter")

        def buttons():
            return [b for b in app.query(Button) if "choice" in b.classes]

        await _wait_for(pilot, lambda: [str(b.label) for b in buttons()] == ["Yes", "No"])
        assert status["level"] == "L2 MEDIUM" and status["model"] == "Codex"
        assert status["status"] == "NEEDS_YOU" and status["tests"] == "—"  # `test -f` reports no counts
        await pilot.click(buttons()[0])

        await _wait_for(pilot, lambda: bool(app.query(TextArea)))
        area = app.query_one(TextArea)
        area.text = "feat(api): paginate list endpoint"
        await pilot.press("ctrl+s")

        await _wait_for(pilot, lambda: [str(b.label) for b in buttons()] == ["Commit", "Edit", "Cancel"])
        # typed answers work too (prefix match)
        app.query_one("#prompt", Input).value = "comm"
        app.query_one("#prompt", Input).focus()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: status.get("status") == "DONE")

    # Shipped defaults: an L2 conversation works in your checkout (worktrees are for L3+).
    assert git(repo, "log", "-1", "--format=%s").strip() == "feat(api): paginate list endpoint"


async def test_slash_commands(repo, monkeypatch):
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        for cmd in ("/help", "/status", "/diff", "/tests", "/stats", "/nope"):
            app.query_one("#prompt", Input).value = cmd
            await pilot.press("enter")
        text = "\n".join(str(w.render()) for w in app.query(".msg"))
        assert "No runtime CLI found" in text and "Unknown command /nope" in text
        assert "No verification has run yet" in text and "No metrics recorded yet" in text


async def test_markdown_messages_and_auto_commit_toggle(repo, monkeypatch):
    from textual.widgets import Markdown
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": FakeRuntime("codex", [])})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        app.say("# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [ ] **Add model**", "markdown")
        await pilot.pause(0.05)
        assert app.query(Markdown)                                    # rendered, not raw text
        bar = app.query_one(tui_app.StatusBar).values
        assert bar["commits"] == "ask"
        await pilot.press("ctrl+t")
        await pilot.pause(0.05)
        assert app.harness.auto_commit and app.query_one(tui_app.StatusBar).values["commits"] == "auto"
        app._command("/autocommit off")
        assert not app.harness.auto_commit


async def test_multiline_paste_is_a_token_and_sent_in_full(repo, monkeypatch):
    from textual import events
    from textual.widgets import Collapsible

    from boost_ai.runtimes.base import ExecutionResult

    from .conftest import done
    rt = FakeRuntime("codex", [lambda req: ExecutionResult(ok=True, final=done(summary="ok"), duration=1)])
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})
    spec = "# METAPROMPT — APP DEMO\n\n## Contexto\n" + "línea de requisitos\n" * 40
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        prompt = app.query_one(tui_app.PromptInput)
        prompt.focus()
        prompt.insert_text_at_cursor("implementa esto: ")
        prompt.post_message(events.Paste(spec))
        await pilot.pause(0.1)
        assert prompt.value == f"implementa esto: [Pasted text #1 · {len(spec.strip()):,} chars · 43 lines]"
        await pilot.press("enter")
        await _wait_for(pilot, lambda: rt.prompts)
        assert "línea de requisitos\n" * 40 in rt.prompts[0]           # every line reached the agent
        folded = app.query(Collapsible)
        assert folded and "Pasted text #1" in str(folded.first().title)
        assert not prompt.pastes                                         # cleared for the next message


async def test_ctrl_c_pauses_then_twice_exits(repo, monkeypatch):
    import asyncio

    from boost_ai.runtimes.base import ExecutionResult

    from .conftest import done

    async def slow(req, progress):
        await asyncio.sleep(30)
        return ExecutionResult(ok=True, final=done(), duration=1)

    rt = FakeRuntime("codex", [])
    rt.execute = slow
    monkeypatch.setattr(tui_app, "build_runtimes", lambda cfg: {"codex": rt})
    app = tui_app.BoostApp(repo)
    async with app.run_test(size=(110, 40)) as pilot:
        prompt = app.query_one(tui_app.PromptInput)
        prompt.value = "Fix typo in README"
        await pilot.press("enter")
        await _wait_for(pilot, lambda: app.worker and app.worker.is_running)
        await pilot.press("ctrl+c")                                      # pause: the agent stops
        await _wait_for(pilot, lambda: not app.worker.is_running)
        assert app.is_running and app.harness.task.status == "STOPPED"
        app._last_interrupt = 0.0                                        # later: a lone Ctrl+C again
        prompt.value = "draft"
        await pilot.press("ctrl+c")
        assert prompt.value == "" and app.is_running                     # clears the prompt only
        await pilot.press("ctrl+c")                                      # second one within 2 s
        await pilot.pause(0.1)
        assert not app.is_running
