"""Conversational TUI. Implements the orchestrator's UI protocol."""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from pathlib import Path

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Collapsible, Input, Markdown, Static, TextArea
from textual.worker import Worker

from .. import config, gitops, indicator, intake, metrics, rules, skills
from ..orchestrator import Harness
from ..runtimes import build_runtimes
from .attachments import Chip, Tray, extract_paths, pick_files
from .branches import BranchPicker

LOGO = (
    "█▄▄ █▀█ █▀█ █▀▀ ▀█▀     ▄▀█ █\n"
    "█▄█ █▄█ █▄█ ▄▄█  █  ▄▄▄ █▀█ █"
)
PROMPT_PLACEHOLDER = "Describe a task...  (/help)"
# Characters some terminals draw wider than Textual measures (East Asian ambiguous/fullwidth).
_AMBIGUOUS = str.maketrans({"…": "...", "“": '"', "”": '"', "‘": "'", "’": "'", "＋": "+", "—": "-", "–": "-"})


def ascii_safe(text: str) -> str:
    """For text inside framed single-row widgets, where one wide glyph breaks the border."""
    return text.translate(_AMBIGUOUS)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GREEN, MUTED, AMBER = "#6fcf97", "#8e8e93", "#e5c07b"
HELP = """\
Describe a development task in plain language. Attach any files with + / Ctrl+O, by dropping them
onto the terminal or pasting their path; ✕ or Backspace (empty prompt) removes them.
Run a shell command yourself with !command (e.g. !git switch -c feature/x). Commands:
/new      new conversation  /history  past tasks      /open T-0003  reopen a task
/status   current task      /diff     full diff       /tests        last verification
/logs     log location      /stats    metrics         /stop         stop the task
/quit     exit              /attach [path]  attach files
/skills   available skills  /skills recommend  ask Claude Code Setup which skills this project needs
/skill add <owner/repo> [<category> <name…|all>]  install into harness/skills/<category>/ (no
          category: list them)   /skill remove <name…>
/rules    rules by category /rule [category:] <text>  add a non-negotiable rule
          (.boost-ai/rules/<category>/project.md; default category: general)
/interview <topic>  the agent interviews you (rounds of questions with recommendations) and
          writes a spec in .boost-ai/specs/ that you can plan right away
/autocommit [on|off]  commit each finished change without asking (also Ctrl+T or click Commits);
          pushing is never automatic
Ctrl+C: pause (stops the agent; its changes stay and your next message continues) · Ctrl+C twice: exit
Ctrl+B or click the branch: switch or create a branch
clear (or Ctrl+L): clear the screen; the conversation continues (/new starts a new one)"""


class BranchValue(Static):
    """The branch in the status bar: its own widget, so the dropdown opens right under it."""

    def on_click(self) -> None:
        self.app.action_branch_picker()


PASTE_MIN_CHARS = 800   # a single-line paste this long also becomes a token
_PASTE_TOKEN = re.compile(r"\[Pasted text #(\d+) · [^\]]*\]")


class PromptInput(Input):
    """The prompt. Textual's Input keeps only the first line of a paste; here a multi-line or long
    paste becomes a `[Pasted text #n · N chars · L lines]` token and the full text is sent on submit."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pastes: dict[int, str] = {}

    def _on_paste(self, event: events.Paste) -> None:
        if self.paste(event.text):
            event.prevent_default()  # skip Input's own handler (it would keep only the first line)
            event.stop()

    def paste(self, text: str) -> bool:
        """Insert a paste; True if handled here (a token, or dropped file paths joined on one line)."""
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if "\n" not in text and len(text) < PASTE_MIN_CHARS:
            return False
        rest, files = extract_paths(text)
        if files and not rest.strip():  # several dropped files: one line, the tray picks them up
            self.insert_text_at_cursor(" ".join(shlex.quote(str(f)) for f in files))
            return True
        n = max(self.pastes, default=0) + 1
        self.pastes[n] = text
        lines = text.count("\n") + 1
        self.insert_text_at_cursor(f"[Pasted text #{n} · {len(text):,} chars · {lines} lines]")
        return True

    def take(self, value: str) -> tuple[str, list[tuple[str, str]]]:
        """(value with every token replaced by its full text, [(token, text)] used). Clears the pastes."""
        used: list[tuple[str, str]] = []

        def expand(m: re.Match) -> str:
            body = self.pastes.get(int(m.group(1)))
            if body is None:
                return m.group(0)
            used.append((m.group(0), body))
            return body

        full = _PASTE_TOKEN.sub(expand, value)
        self.pastes = {}
        return full, used


class CommitsValue(Static):
    """Commits mode in the status bar: click (or Ctrl+T) to switch between ask and auto."""

    def on_click(self) -> None:
        self.app.action_toggle_auto_commit()


class StatusBar(Grid):
    FIELDS = (("Project", "project", "Branch", "branch"), ("Task", "task", "Status", "status"),
              ("Model", "model", "Level", "level"), ("Tests", "tests", "Usage", "usage"),
              ("Commits", "commits", "Progress", "progress"))
    CLICKABLE = {"branch": BranchValue, "commits": CommitsValue}

    def __init__(self) -> None:
        super().__init__(id="statusbar")
        self.values: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        for row in self.FIELDS:
            for label, key in (row[:2], row[2:]):
                yield Static(label, classes="sb-label")
                value = self.CLICKABLE.get(key, Static)
                yield value("—", id=f"sb-{key}", classes="sb-value")

    def set(self, **fields: str) -> None:
        self.values.update({k: v for k, v in fields.items() if v is not None})
        for _, k1, _, k2 in self.FIELDS:
            for key in (k1, k2):
                try:
                    self.query_one(f"#sb-{key}", Static).update(self._value(key))
                except NoMatches:  # not mounted yet; on_mount renders the stored values
                    pass

    def on_mount(self) -> None:
        self.set()

    def _value(self, key: str) -> Text:
        value = self.values.get(key) or "—"
        if key == "branch":
            return Text(f"{value} ▾")
        if key == "commits":
            return Text(f"{value} ▾", style=AMBER if value == "auto" else "")
        if key == "status":
            color = {"RUNNING": GREEN, "VERIFYING": GREEN, "REVIEWING": GREEN, "PLANNING": GREEN,
                     "INTERVIEWING": GREEN, "DONE": GREEN, "NEEDS_YOU": AMBER, "REVIEW": AMBER,
                     "FAILED": "#e88388", "STOPPED": AMBER}.get(value)
            if color:
                return Text(f"● {value.replace('_', ' ')}", style=color)
        return Text(value)


class BoostApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "BOOST_AI"
    EXIT_WINDOW = 2.0  # seconds: a second Ctrl+C within it exits
    BINDINGS = [Binding("ctrl+c", "interrupt", "Pause / exit", show=False, priority=True),
                Binding("ctrl+s", "save_text", "Save", show=False),
                Binding("ctrl+b", "branch_picker", "Branches", show=False, priority=True),
                Binding("ctrl+l", "clear_screen", "Clear", show=False, priority=True),
                Binding("ctrl+o", "attach", "Attach", show=False, priority=True),
                Binding("ctrl+t", "toggle_auto_commit", "Auto-commit", show=False, priority=True),
                Binding("backspace", "remove_last_attachment", show=False, priority=True)]

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.harness: Harness | None = None
        self.worker: Worker | None = None
        self._pending: asyncio.Future | None = None
        self._pending_options: list[str] = []
        self._pending_widget: Vertical | None = None
        self._pending_textarea: TextArea | None = None
        self._activity = ""
        self._spin = 0
        self._last_interrupt = 0.0

    # ── layout ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static(LOGO, id="logo")
        yield StatusBar()
        yield VerticalScroll(id="log", can_focus=False)  # clicking the conversation never steals the prompt
        yield Static("", id="activity")
        yield Tray()
        with Horizontal(id="composer"):
            # ASCII only inside the framed row: wide/ambiguous glyphs (＋, …) shift the right border.
            attach = Button("+", id="attach-btn", tooltip="Attach files (Ctrl+O)")
            attach.can_focus = False  # clicking + keeps focus (and drops/pastes) on the prompt
            yield attach
            yield PromptInput(placeholder=PROMPT_PLACEHOLDER, id="prompt", select_on_focus=False)

    def on_mount(self) -> None:
        self.query_one(StatusBar).set(project=self.root.name, branch=gitops.current_branch(self.root),
                                      task="—", status="IDLE", model="—", level="—", tests="—", usage="—")
        self.set_interval(0.12, self._tick)
        self.query_one("#prompt", Input).focus()

        cfg = config.load(self.root)
        runtimes = build_runtimes(cfg)
        self.harness = Harness(self.root, cfg, self, runtimes)
        self.harness.publish()
        indicator.take_request(self.root)  # drop stale requests from a previous session
        self.indicator_proc = indicator.launch(self.root, cfg)
        if not (self.root / ".boost-ai" / "config.yaml").exists():
            self.say("No project config found. Run `boost-ai init` to detect test/lint commands.", "detail")
        if not runtimes:
            self.say("No runtime CLI found (codex). Run `boost-ai doctor`.", "warning")
        else:
            if gitops.head(self.root) is None:
                self.say(intake.NO_COMMITS_WARNING, "warning")
            last = next(iter(self.harness.store.all()), None)
            if last:
                self.say(f"Last task: {last.id} · {last.status} · {last.title}   (/history, /open {last.id})",
                         "detail")
            self.say("Ready. What should we work on?", "info")

    # ── UI protocol (used by the orchestrator) ──────────────────────────────
    def say(self, text: str, kind: str = "info") -> None:
        if kind == "markdown":  # plans and agent replies: headings, bold, tables, checklists
            self._mount(Markdown(text, classes="msg markdown"))
        else:
            self._mount(Static(Text(text), classes=f"msg {kind}"))

    def action_interrupt(self) -> None:
        """Ctrl+C pauses: stops the running agent (its changes stay, the conversation continues with
        your next message) or clears the prompt. A second Ctrl+C within EXIT_WINDOW exits BOOST_AI."""
        now = time.monotonic()
        if now - self._last_interrupt < self.EXIT_WINDOW:
            if self.worker and self.worker.is_running:
                self.worker.cancel()
            self.exit()
            return
        self._last_interrupt = now
        prompt = self.query_one(PromptInput)
        if self.worker and self.worker.is_running:
            self.worker.cancel()
            hint = "Paused. Write a message to continue the conversation."
        elif prompt.value:
            prompt.value, prompt.pastes = "", {}
            hint = "Prompt cleared."
        else:
            hint = "Nothing is running."
        self.notify(f"{hint} Press Ctrl+C again to exit.", timeout=self.EXIT_WINDOW)

    def action_toggle_auto_commit(self) -> None:
        if self.harness is None:
            return
        self.harness.auto_commit = not self.harness.auto_commit
        self.harness.publish()
        self.say("Auto-commit ON: each finished change (each plan task) is committed with the agent's "
                 "message, without asking. Pushing is still only when you ask (!git push)."
                 if self.harness.auto_commit else "Auto-commit OFF: you approve every commit.", "detail")

    def progress(self, text: str) -> None:
        self._activity = text

    def status(self, **fields: str) -> None:
        try:
            self.query_one(StatusBar).set(**fields)
        except NoMatches:  # a background task reporting while the app shuts down
            return
        if fields.get("status") not in ("RUNNING", "PLANNING", "INTERVIEWING", "VERIFYING", "REVIEWING"):
            self._activity = ""

    async def choose(self, question: str, options: list[str]) -> str:
        buttons = [Button(opt, classes="choice") for opt in options]
        box = Vertical(Static(Text(question), classes="q-text"),
                       Horizontal(*buttons, classes="choices"), classes="question")
        self._pending_options = options
        answer = await self._wait(box, focus=buttons[0])
        return answer

    async def ask_text(self, question: str, placeholder: str = "", default: str = "",
                       multiline: bool = False, options: list[str] | None = None) -> str:
        """Free-text answer; `options` adds one-click buttons (typing stays possible)."""
        if multiline:
            area = TextArea(default, soft_wrap=True, show_line_numbers=False)
            box = Vertical(Static(Text(question), classes="q-text"), area,
                           Horizontal(Button("Save", classes="save"), classes="choices"), classes="question")
            self._pending_textarea = area
            self._pending_options = []
            return await self._wait(box, focus=area)
        parts = [Static(Text(question), classes="q-text")]
        if options:
            parts.append(Horizontal(*[Button(o, classes="choice") for o in options], classes="choices"))
        box = Vertical(*parts, classes="question")
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = ascii_safe(placeholder or "Type your answer...")
        if default:
            prompt.value = default
        self._pending_options = []
        try:
            return await self._wait(box, focus=prompt)
        finally:
            prompt.placeholder = PROMPT_PLACEHOLDER

    # ── question plumbing ───────────────────────────────────────────────────
    async def _wait(self, box: Vertical, focus) -> str:
        self._pending = asyncio.get_running_loop().create_future()
        self._pending_widget = box
        self._mount(box)
        self.call_after_refresh(focus.focus)  # widget must be mounted before it can take focus
        try:
            answer = await self._pending
        finally:
            self._close_question()
        self.say(f"› {answer}" if "\n" not in answer else "› (message saved)", "user")
        return answer

    def _resolve(self, answer: str) -> None:
        if self._pending and not self._pending.done():
            self._pending.set_result(answer)

    def _close_question(self) -> None:
        if self._pending_widget is not None:
            self._pending_widget.remove()
        self._pending = None
        self._pending_widget = None
        self._pending_textarea = None
        self._pending_options = []
        self.query_one("#prompt", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, Chip):          # ✕ on an attachment
            self.query_one(Tray).remove(event.button.path)
            self.query_one("#prompt", Input).focus()
            return
        if event.button.id == "attach-btn":
            self.action_attach()
            return
        if "save" in event.button.classes:
            self.action_save_text()
        else:
            self._resolve(str(event.button.label))

    def action_save_text(self) -> None:
        if self._pending_textarea is not None:
            self._resolve(self._pending_textarea.text.strip())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":  # only the main prompt talks to the harness
            return
        shown = event.value.strip()
        text, pasted = self.query_one(PromptInput).take(shown)
        text = text.strip()
        event.input.value = ""
        if self._pending is not None:
            if self._pending_textarea is not None:
                return
            if self._pending_options:
                match = self._match_option(text)
                if match is None:
                    self.say(f"Choose one of: {', '.join(self._pending_options)}", "detail")
                    return
                self._resolve(match)
            else:
                self._resolve(text)
            return
        tray = self.query_one(Tray)
        if not text and not tray.files:
            return
        if text.lower() in ("clear", "/clear", "cls"):  # like a terminal: clears the screen only
            self._clear()
            return
        if text.startswith("/"):
            self._command(text)
            return
        if self.worker and self.worker.is_running:
            self.say("A task is already running. Ctrl+C (or /stop) pauses it.", "detail")
            return
        assert self.harness
        if command := intake.bang_command(text):  # `!command`: you run it, no model involved
            self.say(f"› {text}", "user")
            self.worker = self.run_worker(self.harness.run_command(command), group="task", exit_on_error=False)
            return
        files = tray.take_all()
        text = text or "Please look at the attached files."
        self.say(f"› {shown or text}" + (f"\n  📎 {', '.join(p.name for p in files)}" if files else ""), "user")
        for title, body in pasted:  # the full pasted text, folded; click to read it all
            self._mount(Collapsible(Static(Text(body)), title=title, collapsed=True, classes="msg pasted"))
        self.worker = self.run_worker(self.harness.run_task(text, files=files), group="task", exit_on_error=False)

    # ── attachments ─────────────────────────────────────────────────────────
    def on_input_changed(self, event: Input.Changed) -> None:
        """A dropped or pasted file path becomes a chip right away."""
        value = event.value
        is_command = value.startswith("!") or re.match(r"^/[a-z]+(\s|$)", value)  # /attach x, not /home/x
        if event.input.id != "prompt" or self._pending is not None or is_command:
            return
        rest, files = extract_paths(event.value)
        if files:
            self.query_one(Tray).add(files)
            event.input.value = rest

    def on_paste(self, event: events.Paste) -> None:
        """Drops/pastes that reach the app (focus not on the prompt) still land in the tray or prompt."""
        if self._pending_textarea is not None or isinstance(self.screen, BranchPicker):
            return
        rest, files = extract_paths(event.text)
        prompt = self.query_one(PromptInput)
        if files and self._pending is None:
            self.query_one(Tray).add(files)
        elif rest.strip() and not prompt.paste(event.text):
            prompt.insert_text_at_cursor(rest.strip())
        prompt.focus()
        event.stop()

    def on_key(self, event: events.Key) -> None:
        """Typing while focus is elsewhere goes to the prompt (Warp-style)."""
        focused = self.focused
        if isinstance(focused, (Input, TextArea)) or isinstance(self.screen, BranchPicker):
            return
        if event.is_printable and event.character and not (event.character == " " and isinstance(focused, Button)):
            prompt = self.query_one("#prompt", Input)
            prompt.value += event.character
            prompt.cursor_position = len(prompt.value)
            prompt.focus()
            event.stop()
            event.prevent_default()

    def action_attach(self) -> None:
        if self._pending is not None:
            return
        self.run_worker(self._pick_and_attach(), group="picker", exclusive=True, exit_on_error=False)

    async def _pick_and_attach(self) -> None:
        files = await pick_files()
        if files is None:
            self.say("No file picker available here. Drop a file onto the terminal, paste its path, "
                     "or use /attach <path>.", "detail")
        else:
            self.query_one(Tray).add(files)
        self.query_one("#prompt", Input).focus()

    def action_remove_last_attachment(self) -> None:
        self.query_one(Tray).pop()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "remove_last_attachment":  # Backspace only when the prompt is empty and files are staged
            prompt = self.query_one("#prompt", Input)
            return self.focused is prompt and not prompt.value and bool(self.query_one(Tray).files)
        return True

    def _match_option(self, text: str) -> str | None:
        t = text.lower()
        if t.isdigit() and 1 <= int(t) <= len(self._pending_options):
            return self._pending_options[int(t) - 1]
        for opt in self._pending_options:
            if opt.lower() == t or opt.lower().startswith(t) and t:
                return opt
        return None

    # ── slash commands ──────────────────────────────────────────────────────
    def _command(self, text: str) -> None:
        cmd = text.split()[0].lower()
        h = self.harness
        task = h.task if h else None
        workdir = Path(task.workdir) if task and task.workdir else self.root
        busy = bool(self.worker and self.worker.is_running)
        if cmd in ("/quit", "/exit"):
            self.exit()
        elif busy and (cmd in ("/new", "/history", "/open", "/skill", "/interview")
                       or text.split() == ["/skills", "recommend"]):
            self.say("A task is running. Ctrl+C (or /stop) pauses it first.", "detail")
        elif cmd == "/attach":
            raw = text[len("/attach"):].strip()
            if not raw:
                self.action_attach()
                return
            _, files = extract_paths(" ".join(p if p.startswith(("/", "~", "'", '"', "file:")) else
                                              str(self.root / p) for p in shlex.split(raw)))
            added = self.query_one(Tray).add(files)
            if not added:
                self.say("No such file. Use an absolute path, ~/…, or a path relative to the project.", "detail")
        elif cmd == "/new":
            self._clear()
            assert h
            h.reset()
            self.say("New conversation. What should we work on?", "info")
        elif cmd == "/history":
            assert h
            tasks = h.store.all()
            if not tasks:
                self.say("No tasks yet.", "detail")
            else:
                rows = [f"{t.id}  {t.created_at[5:16].replace('T', ' ')}  {t.status:<9} "
                        f"{(t.commit or '·······')[:7]}  {t.title}" for t in tasks[:20]]
                more = f"\n… {len(tasks) - 20} older" if len(tasks) > 20 else ""
                self.say("\n".join(rows) + more + "\n\n/open <id> to read one.", "detail")
        elif cmd == "/open":
            assert h
            self._open(text.split()[1].upper() if len(text.split()) > 1 else "")
        elif cmd == "/help":
            self.say(HELP, "detail")
        elif cmd == "/stop":
            if self.worker and self.worker.is_running:
                self.worker.cancel()
            else:
                self.say("Nothing is running.", "detail")
        elif cmd == "/status":
            self.say(f"{task.id} · {task.status} · {task.title}" if task else "No task yet.", "detail")
        elif cmd == "/diff":
            diff = gitops.diff(workdir) if workdir.exists() else ""
            if not diff.strip():
                self.say("No changes.", "detail")
            else:
                lines = diff.splitlines()
                more = f"\n… {len(lines) - 400} more lines" if len(lines) > 400 else ""
                self._mount(Static(Syntax("\n".join(lines[:400]) + more, "diff", theme="ansi_dark",
                                          background_color="#1c1c1e"), classes="msg"))
        elif cmd == "/tests":
            report = h.last_report if h else None
            self.say(report.text() if report else "No verification has run yet.", "detail")
        elif cmd == "/logs":
            where = self.root / ".boost-ai" / "state" / "logs" / (task.id if task else "")
            self.say(f"Logs: {where}", "detail")
        elif cmd == "/skills" and text.split()[1:] == ["recommend"]:
            assert h
            found = skills.discover(self.root)
            if skills.RECOMMENDER not in {s.name for s in found}:
                self.say("Claude Code Setup's recommender is not installed. Install it with:\n"
                         f"  /skill add {skills.RECOMMENDER_SOURCE} project-setup {skills.RECOMMENDER}", "detail")
                return
            request = skills.recommend_request(found)
            self.say(f"› {request}", "user")
            self.worker = self.run_worker(h.run_task(request), group="task", exit_on_error=False)
        elif cmd == "/skill":
            assert h
            args = shlex.split(text)[1:]
            if args[:1] == ["add"] and (len(args) == 2 or len(args) >= 4):
                command = skills.add_command(args[1], args[2] if len(args) > 2 else None, args[3:])
            elif len(args) >= 2 and args[0] == "remove":
                command = skills.remove_command(args[1:])
            else:
                self.say("Usage: /skill add <owner/repo> [<category> <name…|all>]   ·   /skill remove <name…>",
                         "detail")
                return
            self.say(f"› {text}", "user")
            self.worker = self.run_worker(h.run_command(command), group="task", exit_on_error=False)
        elif cmd == "/interview":
            assert h
            topic = text[len("/interview"):].strip()
            if not topic:
                self.say("Usage: /interview <what you want to build or decide>", "detail")
                return
            self._clear()
            self.say(f"› {text}", "user")
            self.worker = self.run_worker(h.interview(topic), group="task", exit_on_error=False)
        elif cmd == "/autocommit":
            assert h
            arg = (text.split()[1:] or [""])[0].lower()
            if {"on": True, "off": False}.get(arg, not h.auto_commit) != h.auto_commit:
                self.action_toggle_auto_commit()
            else:
                self.say(f"Auto-commit is already {arg}.", "detail")
        elif cmd == "/rule":
            rule = text[len("/rule"):].strip()
            if not rule:
                self.say("Usage: /rule [category:] <text>   e.g. /rule git: Commits follow Conventional Commits",
                         "detail")
                return
            head, sep, rest = rule.partition(":")
            category = rules.DEFAULT_CATEGORY
            if sep and rest.strip() and head.strip().replace("-", "").isalnum():  # "git: …" names the category
                category, rule = head.strip().lower(), rest
            path = rules.add(self.root, rule, category)
            self.say(f"Rule added to {path.relative_to(self.root)}. Every plan, change and review must follow it "
                     "(new conversations; use /new to apply it now).", "success")
        elif cmd == "/rules":
            found = rules.discover(self.root)
            self.say(rules.text(self.root) + "\n\nFiles: " + ", ".join(
                str(p).replace(str(Path.home()), "~") for _, p in found) if found else
                "No rules yet. /rule [category:] <text> adds one to .boost-ai/rules/<category>/project.md; any "
                "<category>/*.md there (or in harness/rules/ for every project) is a rule set.", "detail")
        elif cmd == "/skills":
            assert h
            found = skills.discover(self.root)
            lines, category = [], None
            for s in found:
                if s.category != category:
                    category = s.category
                    lines.append(f"{'' if not lines else chr(10)}[{category}]")
                lines.append(f"  {s.name} ({s.scope})  {s.description}")
            self.say("\n".join(lines) + f"\n\nHarness layer: {skills.harness_dir()}" if found else
                     "No skills. Add <category>/<name>/SKILL.md under harness/skills/ (every project) or "
                     ".boost-ai/skills/ (this project).", "detail")
        elif cmd == "/stats":
            self.say(format_stats(metrics.summarize(metrics.load())), "detail")
        else:
            self.say(f"Unknown command {escape(cmd)}. /help lists commands.", "detail")

    # ── branches (Warp-style dropdown) ──────────────────────────────────────
    def action_branch_picker(self) -> None:
        if self.worker and self.worker.is_running:
            self.say("An agent is working; switch branches when it finishes (or /stop).", "detail")
            return
        if self._pending is not None or isinstance(self.screen, BranchPicker):
            return
        cwd = self._branch_cwd()
        anchor = self.query_one("#sb-branch").region  # open right under the branch name
        self.push_screen(BranchPicker(gitops.branches(cwd), gitops.current_branch(cwd),
                                      anchor=(anchor.x, anchor.bottom)), self._branch_chosen)

    def _branch_chosen(self, choice: tuple[str, str] | None) -> None:
        if not choice:
            self.query_one("#prompt", Input).focus()
            return
        kind, name = choice
        assert self.harness
        command = f"git switch {'-c ' if kind == 'create' else ''}{shlex.quote(name)}"
        self.worker = self.run_worker(self.harness.run_command(command, cwd=self._branch_cwd()),
                                      group="task", exit_on_error=False)
        self.query_one("#prompt", Input).focus()

    def _branch_cwd(self) -> Path:
        return self.root  # branches are the user's; worktrees have none of their own

    # ── history ─────────────────────────────────────────────────────────────
    def _clear(self) -> None:
        """Clear what is visible (Warp-style). The conversation, its agent session and history
        are untouched; a pending question stays so no decision is lost."""
        log = self.query_one("#log", VerticalScroll)
        for child in list(log.children):
            if child is not self._pending_widget:
                child.remove()

    def action_clear_screen(self) -> None:
        self._clear()

    def _open(self, task_id: str) -> None:
        assert self.harness
        store = self.harness.store
        if not task_id or not (store.dir / f"{task_id}.json").is_file():
            self.say("Usage: /open T-0003 (see /history).", "detail")
            return
        task = store.load(task_id)
        self._clear()
        self.harness.reset()
        when = task.created_at[:16].replace("T", " ")
        self.say(f"── {task.id} · {task.status} · {when} ──", "detail")
        entries = store.transcript(task_id)
        if not entries:
            self.say(f"› {task.text}", "user")
            self.say("No transcript was recorded for this task.", "detail")
        for e in entries:
            kind, text = e.get("kind", "info"), e.get("text", "")
            if kind == "user":
                self.say(f"› {text}" if "\n" not in text else "› (multi-line answer)", "user")
            elif kind == "question":
                self.say(text, "info")
            else:
                self.say(text, kind)
        footer = [f"Branch {task.branch}" if task.branch else "", f"commit {task.commit}" if task.commit else ""]
        self.say(" · ".join(x for x in footer if x) or "Not committed.", "detail")
        self.say("This is a past task (read-only). Type a new task, or /new.", "detail")

    # ── helpers ─────────────────────────────────────────────────────────────
    def _mount(self, widget) -> None:
        try:
            log = self.query_one("#log", VerticalScroll)
        except NoMatches:  # shutting down
            return
        log.mount(widget)
        log.scroll_end(animate=False)

    def on_unmount(self) -> None:
        proc = getattr(self, "indicator_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _check_indicator_requests(self) -> None:
        action = indicator.take_request(self.root)
        if action == "stop":
            if self.worker and self.worker.is_running:
                self.say("Stop requested from the top-bar indicator.", "detail")
                self.worker.cancel()
        elif action == "quit":
            if self.worker and self.worker.is_running:
                self.worker.cancel()
            self.exit()

    def _tick(self) -> None:
        try:
            activity = self.query_one("#activity", Static)
        except NoMatches:  # the app is shutting down; timers can still fire once
            return
        self._check_indicator_requests()
        if self._activity and self.worker and self.worker.is_running and self._pending is None:
            self._spin = (self._spin + 1) % len(SPINNER)
            activity.update(Text.assemble((SPINNER[self._spin] + " ", GREEN), (self._activity, MUTED)))
        else:
            activity.update("")


def format_stats(rows: list[dict]) -> str:
    if not rows:
        return "No metrics recorded yet."
    out = [f"{'level':<6}{'runtime':<12}{'runs':>5}{'ok':>7}{'avg s':>8}{'avg tok':>10}{'esc in':>8}"]
    for r in rows:
        tok = f"{r['avg_tokens']:,.0f}" if r["avg_tokens"] is not None else "—"
        out.append(f"{str(r['level']):<6}{str(r['runtime']):<12}{r['attempts']:>5}"
                   f"{r['success_rate']:>7.0%}{r['avg_duration']:>8.0f}{tok:>10}{r['escalations_in']:>8}")
    return "\n".join(out)
