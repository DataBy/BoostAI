"""The harness loop: triage → route → plan → execute → verify → (retry/escalate) → review → commit → memory.

A task is a conversation. Every user message is a turn handled by the routed agent,
which decides what the request needs (answer, commands, git, code). The harness adds
guarantees, not rules: when a turn changes files it verifies, escalates, reviews risky
work and asks the user to commit; dangerous commands are always the user's call. Medium+
work and attached specs are planned read-only first; nothing changes until the user approves.

UI-agnostic: talks to the user only through the `UI` protocol, so the same flow
runs in the TUI and in tests with a scripted UI.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
from pathlib import Path
from typing import Protocol

from . import gitops, graft, hook, metrics, routing, safety, skills, verify
from .alerts import Alerts
from .context import (
    approved_message,
    build_continue_prompt,
    build_interview_prompt,
    build_prompt,
    build_review_prompt,
)
from .memory import ImplementationRecord, MemoryBackend, MicroSpecRef, backend_from_config
from .runtimes import label
from .runtimes.base import (
    INTERVIEW_SCHEMA,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    ErrorKind,
    ExecutionRequest,
    ExecutionResult,
    Runtime,
    Usage,
    UsageBook,
)
from .task import IMAGE_EXTENSIONS, RICH_EXTENSIONS, Task, TaskStore, find_attachments, now_iso
from .triage import LEVEL_NAMES, assess

MAX_CLARIFICATIONS = 3
TRIAGE_ATTACHMENT_CHARS = 20_000  # attached specs count for triage: a short "do this" + a big .md is not L0
STOP_TASK = "Stop task"


class UI(Protocol):
    def say(self, text: str, kind: str = "info") -> None: ...        # info|success|warning|error|detail
    def progress(self, text: str) -> None: ...
    def status(self, **fields: str) -> None: ...
    async def choose(self, question: str, options: list[str]) -> str: ...
    async def ask_text(self, question: str, placeholder: str = "", default: str = "",
                       multiline: bool = False, options: list[str] | None = None) -> str: ...


class _Recorder:
    """Passes UI calls through and keeps the current task's transcript."""

    def __init__(self, ui: UI, harness: Harness):
        self._ui, self._h = ui, harness

    def __getattr__(self, name):
        return getattr(self._ui, name)

    def say(self, text: str, kind: str = "info") -> None:
        self._ui.say(text, kind)
        self._h._record(kind, text)

    async def choose(self, question: str, options: list[str]) -> str:
        self._h._record("question", question)
        answer = await self._ui.choose(question, options)
        self._h._record("user", answer)
        return answer

    async def ask_text(self, question: str, *args, **kwargs) -> str:
        self._h._record("question", question)
        answer = await self._ui.ask_text(question, *args, **kwargs)
        self._h._record("user", answer)
        return answer


class Harness:
    def __init__(self, root: Path, cfg: dict, ui: UI, runtimes: dict[str, Runtime],
                 usage: UsageBook | None = None, alerts: Alerts | None = None,
                 memory: MemoryBackend | None = None, metrics_path: Path | None = None):
        self.root, self.cfg, self.runtimes = root, cfg, runtimes
        self.ui: UI = _Recorder(ui, self)
        self.usage = usage or UsageBook()
        self.alerts = alerts or Alerts(cfg)
        self.memory = memory or backend_from_config(cfg, root)
        self.metrics_path = metrics_path
        self.store = TaskStore(root)
        self.task: Task | None = None
        self.last_report: verify.Report | None = None
        self._attempt_start = now_iso()
        self.review_notes: list[str] = []
        self._models: dict[str, str] = {}      # runtime → concrete model last reported
        self._model_override: dict[str, str] = {}  # runtime → model chosen by escalation
        self._turn_files: list[Path] = []   # this message's attachments (copies)
        self._turn_before: dict[str, str] = {}
        self._approved: tuple[str, list[str], str] | None = None  # approved (plan, skills, current task)
        self._progress: list[tuple[str, bool]] = []  # the approved plan's tasks and whether each is done
        # Commit each finished change without asking (your standing approval; toggled in the UI).
        # Pushing is never automatic.
        self.auto_commit = bool((cfg.get("git") or {}).get("auto_commit", False))
        self._graft_job: asyncio.Task | None = None  # background refresh of graft's concept nodes
        ensure_project_dirs(root)

    # ── status ──────────────────────────────────────────────────────────────
    def publish(self, **extra: str) -> None:
        t = self.task
        fields = {
            "project": self.root.name,
            "branch": t.branch if t and t.worktree and t.branch else gitops.current_branch(self.root),
            "task": t.id if t else "—",
            "title": t.title if t else "",
            "status": t.status if t else "IDLE",
            "model": (f"{label(t.runtime)} · {self._models[t.runtime]}" if t.runtime in self._models
                      else label(t.runtime)) if t else "—",
            "level": f"L{t.level} {LEVEL_NAMES[t.level]}" if t and t.status != "NEW" else "—",
            "tests": (t.tests or "—") if t else "—",
            "usage": self.usage.display(t.runtime) if t and t.runtime else "—",
            "commits": "auto" if self.auto_commit else "ask",
            "progress": (f"{sum(d for _, d in self._progress)}/{len(self._progress)} tasks"
                         if t and self._progress else "—"),
            **extra,
        }
        self.ui.status(**fields)
        self.store.set_current(fields)

    def _set(self, **changes) -> None:
        assert self.task
        for k, v in changes.items():
            setattr(self.task, k, v)
        self.store.save(self.task)
        self.publish()

    async def _ask_choice(self, question: str, options: list[str], event: str = "needs_input") -> str:
        self.alerts.alert(event, "BOOST_AI needs you", question)
        prev = self.task.status if self.task else "IDLE"
        if self.task:
            self._set(status="NEEDS_YOU")
        answer = await self.ui.choose(question, options)
        if self.task:
            self._set(status=prev)
        return answer

    # ── main flow ───────────────────────────────────────────────────────────
    async def run_task(self, text: str, files: list[Path] | None = None) -> Task:
        """Handle one user message: start a conversation or continue the current one."""
        first = self.task is None
        if first:
            self.task = self.store.new(text)
            self._model_override = {}
        task = self.task
        assert task
        task.turns += 1
        task.messages.append(text)
        self.store.record(task.id, "user", text)
        self.last_report, self.review_notes, self._approved, self._progress = None, [], None, []
        self._attach(task, text, files or [])
        try:
            await (self._start(task) if first else self._turn(task, text))
        except asyncio.CancelledError:
            self._set(status="STOPPED")
            self.ui.say("Stopped. Changes so far are left in place for inspection.", "warning")
            raise
        except Exception as exc:  # last-resort: never leave the user with a bare traceback
            self._set(status="FAILED")
            self.ui.say(f"BOOST_AI hit an internal error: {exc}", "error")
            self.alerts.alert("warning", "BOOST_AI failed", str(exc))
        return task

    async def _start(self, task: Task) -> None:
        tri = await assess(self._triage_text(task.text), self.cfg)
        self._set(level=tri.level, risks=tri.risks, status="TRIAGED")
        risk = f" · risk: {', '.join(tri.risks)}" if tri.risks else ""
        self.ui.say(f"Level {tri.label}{risk}", "detail")

        route = routing.select(tri.level, self.cfg, set(self.runtimes), self._usage_map())
        if route.runtime is None:
            self._set(status="FAILED")
            self.ui.say("No runtime is available.\n" + "\n".join(route.skipped)
                        + "\nRun `boost-ai doctor` for details.", "error")
            self.alerts.alert("warning", "BOOST_AI: no runtime available")
            self.task = None  # nothing to continue
            return
        self.ui.say(f"Routing to {label(route.runtime)} — {route.reason}", "detail")
        self._set(runtime=route.runtime)
        if not await self._prepare_workspace(task):
            return
        await self._run_turn(task, task.text)

    async def _turn(self, task: Task, text: str) -> None:
        tri = await assess(self._triage_text(text), self.cfg)
        if tri.level > task.level:  # the conversation is as risky as its riskiest request
            self._set(level=tri.level, risks=sorted(set(task.risks) | set(tri.risks)))
            self.ui.say(f"Level raised to {tri.label}", "detail")
            policy = self.cfg["routing"]["policy"].get(f"L{task.level}", [])
            if task.runtime and task.runtime not in policy:
                route = routing.select(task.level, self.cfg, set(self.runtimes), self._usage_map())
                if route.runtime and route.runtime != task.runtime:
                    self.ui.say(f"{label(task.runtime)} is not used for L{task.level} work → continuing with "
                                f"{label(route.runtime)} (it gets the conversation so far).", "warning")
                    self._set(runtime=route.runtime)
        if not task.runtime or task.runtime not in self.runtimes:
            route = routing.select(task.level, self.cfg, set(self.runtimes), self._usage_map())
            if route.runtime is None:
                self.ui.say("No runtime is available. Run `boost-ai doctor`.", "error")
                return
            self._set(runtime=route.runtime)
        if task.workdir and not Path(task.workdir).is_dir():
            self.ui.say("This conversation's worktree no longer exists. Use /new.", "warning")
            return
        await self._run_turn(task, text)

    async def _run_turn(self, task: Task, text: str) -> None:
        self._ensure_capable_runtime(task)
        workdir = Path(task.workdir or self.root)
        self._prepare_graft(workdir)
        if not self._needs_plan(task):
            await self._implement(task, text, gitops.snapshot(workdir))
            return
        planned = await self._plan(task, text)
        if planned is None:
            return
        if planned.get("status") == "answer":  # nothing to change: the plan run already answered
            self.ui.say(planned.get("plan") or "", "markdown")
            self._set(status="DONE")
            return
        await self._run_plan(task, planned)

    async def _run_plan(self, task: Task, planned: dict) -> None:
        """Implement an approved plan one task at a time: each is verified, reviewed when risky and
        committed (asked, or automatic with auto-commit) before the next starts. Never pushes."""
        workdir = Path(task.workdir or self.root)
        plan, chosen = planned.get("plan") or "", planned.get("skills") or []
        titles = [str(t).strip() for t in planned.get("tasks") or [] if str(t).strip()] or [task.title]
        self._progress = [(t, False) for t in titles]
        commits_before = gitops.head(workdir)
        for i, title in enumerate(titles, 1):
            current = f"Task {i}/{len(titles)}: {title}"
            self._approved = (plan, chosen, current)
            message = (approved_message(self.root, self.cfg, plan, chosen, current) if i == 1 else
                       f"{current}\nImplement only this task now; the earlier ones are done and committed.")
            self.ui.say(f"▶ {current}", "detail")
            if not await self._implement(task, message, gitops.snapshot(workdir), offer_push=False):
                self.ui.say(f"Stopped at task {i}/{len(titles)}. Earlier tasks keep their commits.", "warning")
                return
            self._progress[i - 1] = (title, True)
            self.ui.say(self._checklist(), "markdown")
            self.publish()
        made = gitops.git(workdir, "rev-list", "--count", f"{commits_before}..HEAD", check=False).strip() \
            if commits_before else ""
        branch = task.branch if task.worktree and task.branch else gitops.current_branch(workdir)
        self.ui.say(f"Plan complete: {len(titles)} tasks" + (f", {made} commits on {branch}" if made else "")
                    + ". Pushing is up to you: !git push", "success")

    def _checklist(self) -> str:
        return "\n".join(f"- ✓ {title}" if done else f"- ○ {title}" for title, done in self._progress)

    async def _implement(self, task: Task, message: str, before: dict[str, str], offer_push: bool = True) -> bool:
        """Execute → verify → review (risky work) → commit. False if the user stopped it."""
        outcome = await self._execute_loop(task, message=message, before=before)
        if outcome is None:
            return False
        result, changed = outcome
        if changed:
            result = await self._review(task, result)
            if result is None:
                return False
        await self._finish(task, result, changed, offer_push)
        return True

    async def _prepare_workspace(self, task: Task) -> bool:
        has_commits = gitops.head(self.root) is not None
        dirty = has_commits and gitops.is_dirty(self.root)
        want_wt = task.level >= self.cfg["routing"].get("worktree_min_level", 3)
        branch = gitops.current_branch(self.root)
        if has_commits and want_wt and branch != "(detached)":
            path = gitops.create_worktree(self.root, task.id)
            self._set(workdir=str(path), branch=branch, worktree=True)
            self.ui.say(f"L{task.level} → isolated worktree {path.relative_to(self.root)} (no branch of its own); "
                        f"approved commits land on {branch}", "detail")
            if dirty:
                self.ui.say("Note: your uncommitted changes are not part of this worktree.", "detail")
            return True
        self._set(workdir=str(self.root), branch=gitops.current_branch(self.root), worktree=False)
        return True

    async def _execute_loop(self, task: Task, message: str = "", feedback: str = "",
                            before: dict[str, str] | None = None) -> tuple[ExecutionResult, list[str]] | None:
        """Run the turn until the agent is done (and, if it changed files, verification passes).
        Returns (result, files changed this turn) or None if stopped."""
        workdir = Path(task.workdir or self.root)
        if before is not None:
            self._turn_before = before
        failures: dict[str, int] = {}
        answers: list[tuple[str, str]] = []
        escalated_from: str | None = None
        new_answer: tuple[str, str] | None = None
        max_attempts = self.cfg["routing"].get("attempts_per_runtime", 2)

        while True:
            name = task.runtime
            assert name
            head_before = gitops.head(workdir)
            if name not in task.runtimes_tried:
                task.runtimes_tried.append(name)
            task.attempts += 1
            self._set(status="RUNNING")
            # The agent keeps one session per conversation: resume it and send only what is new.
            session = task.sessions.get(name)
            prompt = (build_continue_prompt(feedback, new_answer, message) if session
                      else build_prompt(task, self.root, self.cfg, feedback, answers,
                                        "" if self._approved else message, approved=self._approved))
            if message:  # the turn's request: say which files came with it
                prompt += self._attachments_section()
            message, new_answer = "", None
            result = await self._attempt(task, name, prompt, resume=session)
            if result.session_id:
                task.sessions[name] = result.session_id
                self.store.save(task)

            if not result.ok:
                if task.sessions.pop(name, None):  # never resume a session that just failed
                    self.store.save(task)
                self._metric(task, name, result, escalated_from)
                nxt = await self._handle_runtime_error(task, result, failures)
                if nxt is None:
                    return None
                if nxt != name:
                    escalated_from = name
                    self._set(runtime=nxt)
                message = task.messages[-1]  # a fresh session needs the request again
                continue

            final = result.final or {}
            if gitops.head(workdir) != head_before:
                self.ui.say(f"{label(name)} created a git commit on its own, which BOOST_AI does not allow. "
                            "Inspect the branch history before continuing.", "warning")
                self.alerts.alert("warning", "Runtime committed without approval")

            if final.get("status") == "blocked":
                self._metric(task, name, result, escalated_from)
                if len(answers) >= MAX_CLARIFICATIONS:
                    self.ui.say("The agent keeps asking for clarification; stopping here.", "warning")
                    self._set(status="FAILED")
                    return None
                question = final.get("blocker") or "The agent needs more information."
                options = [str(o) for o in (final.get("blocker_options") or []) if str(o).strip()][:4]
                self.alerts.alert("needs_input", "BOOST_AI needs you", question)
                self._set(status="NEEDS_YOU")
                answer = await self.ui.ask_text(question, placeholder="Or type your answer…",
                                                options=options + [STOP_TASK])
                if not answer.strip() or answer == STOP_TASK:
                    self._set(status="STOPPED")
                    self.ui.say("Stopped.", "detail")
                    return None
                answers.append((question, answer.strip()))
                new_answer, feedback = answers[-1], ""
                continue

            changed = gitops.changed_since(self._turn_before, gitops.snapshot(workdir))
            if not changed:  # an answer, a command, git: nothing to verify
                self._metric(task, name, result, escalated_from)
                return result, changed

            self._set(status="VERIFYING")
            self.ui.progress("verifying")
            report = await verify.run(self.cfg.get("commands") or {}, workdir, self._log_dir(task),
                                      self.cfg.get("verify_timeout", 900), verify.project_env(self.root))
            self.last_report = report
            self._set(tests=report.tests)
            self._metric(task, name, result, escalated_from, report)
            if report.ok or not report.configured:
                self.ui.say(report.text(), "success" if report.ok else "warning")
                return result, changed

            failures[name] = failures.get(name, 0) + 1
            feedback = "Verification failed:\n" + report.failures_for_model()
            self.ui.say(report.text(), "error")
            if failures[name] < max_attempts:
                self.ui.say(f"Verification failed. Retrying with {label(name)} using the failure details.", "detail")
                continue

            nxt = routing.escalate(name, self.cfg, set(self.runtimes), self._usage_map(), failures)
            stronger = self._stronger_model(task, name)
            options = ([f"Use {stronger}"] if stronger else []) + \
                      ([f"Escalate to {label(nxt)}"] if nxt else []) + [f"Retry {label(name)}", "Stop"]
            choice = await self._ask_choice(
                f"{label(name)} failed verification {failures[name]} times.", options, event="warning")
            if stronger and choice == f"Use {stronger}":
                self._model_override[name] = stronger
                failures[name] = 0
            elif choice.startswith("Escalate") and nxt:
                escalated_from = name
                self._set(runtime=nxt)
                message = task.messages[-1]
            elif choice.startswith("Retry"):
                failures[name] = 0
            else:
                self._set(status="STOPPED")
                return None

    def _model_for(self, task: Task, name: str) -> str | None:
        """Escalated model, else the model configured for the conversation's level, else default."""
        rcfg = (self.cfg.get("runtimes") or {}).get(name) or {}
        return (self._model_override.get(name) or (rcfg.get("models") or {}).get(f"L{task.level}")
                or rcfg.get("model"))

    def _stronger_model(self, task: Task, name: str) -> str | None:
        top = (((self.cfg.get("runtimes") or {}).get(name) or {}).get("models") or {}).get("L4")
        return top if top and top != self._model_for(task, name) else None

    async def _attempt(self, task: Task, name: str, prompt: str, resume: str | None = None,
                       mode: str = "attempt") -> ExecutionResult:
        """One runtime run. `mode`: attempt (may edit) | plan | interview | review (all read-only)."""
        runtime = self.runtimes[name]
        review = mode == "review"
        log = self._log_dir(task) / f"turn{task.turns}-{mode}-{task.attempts}-{name}.jsonl"
        doing = {"attempt": "working", "plan": "planning", "interview": "interviewing", "review": "reviewing"}
        self.ui.progress(f"{label(name)} {doing[mode]}")
        approvals = self._log_dir(task) / "approvals"
        req = ExecutionRequest(prompt=prompt, cwd=Path(task.workdir or self.root), log_path=log,
                               model=None if review else self._model_for(task, name),
                               timeout=int((self.cfg.get("runtimes", {}).get(name) or {}).get("timeout", 1800)),
                               env=verify.project_env(self.root), approval_dir=approvals,
                               read_only=mode != "attempt",
                               schema={"review": REVIEW_SCHEMA, "plan": PLAN_SCHEMA,
                                       "interview": INTERVIEW_SCHEMA}.get(mode),
                               images=[] if review else [p for p in self._turn_files
                                                         if p.suffix.lower() in IMAGE_EXTENSIONS],
                               resume_session=resume,
                               read_dirs=[*skills.global_dirs(),
                                          *[d for d in [self._attachments(task)] if d.is_dir()]],
                               mcp_servers={"graft": graft.mcp_server(Path(task.workdir or self.root))}
                               if graft.available(self.cfg) else {})
        self._attempt_start = now_iso()
        watcher = asyncio.create_task(self._watch_approvals(approvals, name))
        try:
            result = await runtime.execute(req, lambda text: self.ui.progress(f"{label(name)} · {text}"))
        finally:
            watcher.cancel()
        if result.usage is not None:
            u = result.usage
            self.usage.record(name, u.state, estimate=False, percent=u.percent, window=u.window)
            if u.state in (Usage.CONSERVE, Usage.CRITICAL):
                self.ui.say(f"{label(name)} usage is at {u.percent:.0f}% of its {u.window} window "
                            f"({u.state.value}).", "warning")
        elif result.ok:
            self.usage.record(name, Usage.AVAILABLE, estimate=True, note="last run succeeded")
        elif result.error_kind is ErrorKind.RATE_LIMIT:
            self.usage.record(name, Usage.EXHAUSTED, estimate=True, note=result.error[:200])
        if result.model:
            self._models[name] = result.model
        # Only output tokens are shown; input/cached tokens are still recorded in metrics (/stats).
        tokens = f" · {result.output_tokens:,} output tokens" if result.output_tokens is not None else ""
        who = f"{label(name)} ({result.model})" if result.model else label(name)
        how = " (resumed)" if resume else ""
        self.ui.say(f"{who} finished in {result.duration:.0f}s{how}{tokens}", "detail")
        self.publish()
        return result

    def _needs_plan(self, task: Task) -> bool:
        pcfg = self.cfg.get("plan") or {}
        min_level = pcfg.get("min_level", 2)
        return ((min_level is not None and task.level >= min_level)
                or (bool(self._turn_files) and pcfg.get("attachments", True)))

    # ── interview ───────────────────────────────────────────────────────────
    async def interview(self, topic: str) -> Path | None:
        """`/interview`: a read-only interview (the grilling method) in rounds of questions, each with
        a recommended answer, until a shared understanding. Ends in a spec under .boost-ai/specs/
        that can be planned right away. Returns the spec's path, None if stopped."""
        self.reset()
        task = self.task = self.store.new(f"Interview: {topic}")
        task.turns, task.messages = 1, [topic]
        self.store.record(task.id, "user", f"/interview {topic}")
        self._turn_files, self._progress = [], []
        try:
            return await self._interview(task, topic)
        except asyncio.CancelledError:
            self._set(status="STOPPED")
            raise
        except Exception as exc:  # never leave the user with a bare traceback
            self._set(status="FAILED")
            self.ui.say(f"BOOST_AI hit an internal error: {exc}", "error")
            return None

    async def _interview(self, task: Task, topic: str) -> Path | None:
        tri = await assess(topic, self.cfg)
        level = max(tri.level, 2)  # a good interviewer needs a strong model
        route = routing.select(level, self.cfg, set(self.runtimes), self._usage_map())
        if route.runtime is None:
            self._set(status="FAILED")
            self.ui.say("No runtime is available. Run `boost-ai doctor`.", "error")
            return None
        self._set(level=level, risks=tri.risks, runtime=route.runtime, workdir=str(self.root),
                  branch=gitops.current_branch(self.root))
        self.ui.say(f"Interview with {label(route.runtime)}. Answer each round; you can ask for the spec "
                    "at any time.", "detail")
        self._prepare_graft(self.root)
        name, answers, delta, rounds = route.runtime, [], "", 0
        while True:
            self._set(status="INTERVIEWING")
            session = task.sessions.get(name)
            prompt = (build_continue_prompt(message=delta) if session
                      else build_interview_prompt(task, self.root, self.cfg, topic, answers))
            result = await self._attempt(task, name, prompt, resume=session, mode="interview")
            self._metric(task, name, result, None, role="interview")
            if result.session_id:
                task.sessions[name] = result.session_id
                self.store.save(task)
            if not result.ok:
                task.sessions.pop(name, None)
                self.ui.say(f"{label(name)} could not continue the interview: {result.error}", "error")
                if await self._ask_choice("Interview failed.", ["Retry", "Stop"], event="warning") != "Retry":
                    self._set(status="STOPPED")
                    return None
                continue
            final = result.final or {}
            if final.get("status") == "done" or not final.get("questions"):
                return await self._save_spec(task, final)
            rounds += 1
            questions = final["questions"]
            self.ui.say(f"### Round {rounds}\n\n" + "\n\n---\n\n".join(
                f"❓ **Q{i}** {q.get('question', '').strip()}\n\n➡️ *Recommended:* {q.get('recommended', '').strip()}"
                for i, q in enumerate(questions, 1)), "markdown")
            asked = "\n".join(f"Q{i}: {q.get('question', '')}" for i, q in enumerate(questions, 1))
            choice = await self._ask_choice("Your answers?", ["Answer", "Accept all recommendations",
                                                                "Write the spec now", "Stop"])
            if choice == "Stop":
                self._set(status="STOPPED")
                return None
            if choice == "Answer":
                reply = await self.ui.ask_text(
                    "Answer by number; any question you leave out takes the recommendation.",
                    default="\n".join(f"{i}. " for i in range(1, len(questions) + 1)), multiline=True)
                delta = "My answers (unanswered ones take your recommendation):\n" + reply.strip()
            elif choice == "Accept all recommendations":
                delta = "Yes to all your recommendations."
            else:
                delta = ("Stop asking. Write the spec now with what we have settled; put anything still open "
                         "under open questions.")
            answers.append((asked, delta))

    async def _save_spec(self, task: Task, final: dict) -> Path | None:
        spec = (final.get("spec") or "").strip()
        if not spec:
            self.ui.say("The interview ended without a spec.", "warning")
            self._set(status="FAILED")
            return None
        title = (final.get("title") or task.title).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "spec"
        path = self.root / ".boost-ai" / "specs" / f"{task.id}-{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec + "\n" if spec.startswith("# ") else f"# {title}\n\n{spec}\n")  # one H1 title
        self.ui.say(path.read_text(), "markdown")
        self.ui.say(f"Spec saved: {path.relative_to(self.root)}", "success")
        self._set(status="DONE")
        if await self._ask_choice("Plan this spec now?", ["Plan it", "Later"]) == "Plan it":
            self.reset()
            await self.run_task(f"Implement the attached spec: {title}", files=[path])
        else:
            self.ui.say(f"Plan it later by attaching it: /attach {path.relative_to(self.root)}", "detail")
        return path

    async def _plan(self, task: Task, text: str) -> dict | None:
        """Read-only planning run by the conversation's runtime. The user approves it, asks for
        changes (the planner revises it in the same session) or stops. Returns the approved plan
        (or a direct answer when nothing needs changing), None if stopped."""
        name = task.runtime
        assert name
        answers: list[tuple[str, str]] = []
        delta, feedback, new_answer = text, "", None
        while True:
            self._set(status="PLANNING")
            session = task.sessions.get(name)
            prompt = (build_continue_prompt(feedback, new_answer, delta, plan=True) if session
                      else build_prompt(task, self.root, self.cfg, answers=answers, message=text, plan=True)
                      + self._attachments_section())
            result = await self._attempt(task, name, prompt, resume=session, mode="plan")
            self._metric(task, name, result, None, role="plan")
            if result.session_id:
                task.sessions[name] = result.session_id
                self.store.save(task)
            delta, feedback, new_answer = "", "", None
            if not result.ok:
                if task.sessions.pop(name, None):
                    self.store.save(task)
                self.ui.say(f"{label(name)} could not plan: {result.error}", "error")
                if await self._ask_choice("Planning failed.", ["Retry", "Stop"], event="warning") != "Retry":
                    self._set(status="STOPPED")
                    return None
                continue
            final = result.final or {}
            if final.get("status") == "answer":
                return final
            if final.get("status") == "blocked":
                if len(answers) >= MAX_CLARIFICATIONS:
                    self.ui.say("The agent keeps asking for clarification; stopping here.", "warning")
                    self._set(status="FAILED")
                    return None
                question = final.get("blocker") or "The agent needs more information."
                options = [str(o) for o in (final.get("blocker_options") or []) if str(o).strip()][:4]
                self.alerts.alert("needs_input", "BOOST_AI needs you", question)
                self._set(status="NEEDS_YOU")
                answer = await self.ui.ask_text(question, placeholder="Or type your answer…",
                                                options=options + [STOP_TASK])
                if not answer.strip() or answer == STOP_TASK:
                    self._set(status="STOPPED")
                    return None
                answers.append((question, answer.strip()))
                new_answer = answers[-1]
                continue

            known = {s.name for s in skills.discover(self.root)}
            final["skills"] = [s for s in final.get("skills") or [] if s in known]
            tasks = "\n".join(f"- ○ {t}" for t in final.get("tasks") or []) or "- ○ (one task)"
            uses = ", ".join(f"`{s}`" for s in final["skills"]) or "none"
            self.ui.say(f"# Plan\n\n{(final.get('plan') or '').strip()}\n\n## Tasks\n\n{tasks}\n\n"
                        f"**Skills:** {uses}", "markdown")
            choice = await self._ask_choice("Approve this plan?", ["Approve", "Request changes", "Stop"])
            if choice == "Approve":
                return final
            if choice == "Stop":
                self._set(status="STOPPED")
                return None
            change = await self.ui.ask_text("What should change in the plan?", multiline=True)
            if not change.strip():
                if await self._ask_choice("No changes given.", ["Approve", "Stop"]) == "Approve":
                    return final
                self._set(status="STOPPED")
                return None
            answers.append(("Changes requested to the plan", change.strip()))
            feedback = "The user wants these changes to your plan; revise it:\n" + change.strip()

    async def _watch_approvals(self, directory: Path, name: str) -> None:
        """Bring dangerous commands requested by the runtime's safety hook to the user."""
        handled: set[str] = set()
        while True:
            for req in hook.pending(directory):
                if req.id in handled:
                    continue
                handled.add(req.id)
                force = any(r.startswith("force push") for r in req.reasons)
                question = (f"⚠ {label(name)} wants to run a potentially destructive command:\n\n"
                            f"    {req.command}\n\nRisk: {'; '.join(req.reasons)}")
                if force:
                    question += "\n\nThis REWRITES REMOTE HISTORY and can destroy other people's work."
                allow = "Allow force push" if force else "Allow once"
                choice = await self._ask_choice(question, ["Deny", allow], event="warning")
                hook.respond(directory, req.id, choice == allow)
                self.ui.say(f"{'Allowed' if choice == allow else 'Denied'}: {req.command}", "detail")
            await asyncio.sleep(0.3)

    async def _review(self, task: Task, result: ExecutionResult) -> ExecutionResult | None:
        """Independent read-only review by a different runtime, for high-risk work only."""
        rcfg = self.cfg.get("review") or {}
        if task.level < rcfg.get("min_level", 3):
            return result
        usage = self._usage_map()
        reviewer = next((n for n in rcfg.get("runtimes", []) if n in self.runtimes and n != task.runtime
                         and usage.get(n) is not Usage.EXHAUSTED), None)
        if reviewer is None:
            self.ui.say("Independent review skipped: no second runtime is available.", "warning")
            return result
        workdir = Path(task.workdir or self.root)
        self.ui.say(f"L{task.level} → independent review by {label(reviewer)}", "detail")
        self._set(status="REVIEWING")
        before = gitops.diff(workdir)
        prompt = build_review_prompt(task, self.root, self.cfg, label(task.runtime), result.final or {},
                                     self.last_report.text() if self.last_report else "not run")
        review = await self._attempt(task, reviewer, prompt, mode="review")
        self._metric(task, reviewer, review, None, role="review")
        if gitops.diff(workdir) != before:
            self.ui.say(f"{label(reviewer)} modified files during a read-only review. Inspect /diff.", "warning")
            self.alerts.alert("warning", "Reviewer modified files")

        if not review.ok:
            self.ui.say(f"Review could not run: {review.error}", "warning")
            choice = await self._ask_choice("Continue without review?", ["Continue", "Stop"], event="warning")
            if choice != "Continue":
                self._set(status="STOPPED")
                return None
            return result

        verdict = review.final or {}
        findings = verdict.get("findings") or []
        lines = [f"[{f.get('severity', '?').upper()}] {f.get('file', '')}: {f.get('issue', '')}" for f in findings]
        approved = verdict.get("verdict") == "approve"
        self.ui.say(f"Review by {label(reviewer)}: {'approved' if approved else 'changes requested'}\n"
                    f"{verdict.get('summary', '')}" + ("\n" + "\n".join(lines) if lines else ""),
                    "success" if approved else "warning")
        self.review_notes = [f"Review ({label(reviewer)}): {ln}" for ln in lines]
        if approved:
            return result

        choice = await self._ask_choice("The reviewer requested changes.",
                                        [f"Fix with {label(task.runtime)}", "Accept as is", "Stop"])
        if choice == "Accept as is":
            return result
        if choice == "Stop":
            self._set(status="STOPPED")
            return None
        feedback = "An independent reviewer requested changes:\n" + "\n".join(lines)
        fixed = await self._execute_loop(task, feedback=feedback)
        if fixed is None:
            return None
        self.ui.say("Fixes verified. The review is not repeated automatically; check /diff.", "detail")
        return fixed[0]

    async def _handle_runtime_error(self, task: Task, result: ExecutionResult,
                                    failures: dict[str, int]) -> str | None:
        """Explain the failure and let the user decide. Returns runtime to use next, or None to stop."""
        name = task.runtime
        assert name
        if result.error_kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH, ErrorKind.UNAVAILABLE):
            failures[name] = 10**6  # unusable for this task
        else:
            failures[name] = failures.get(name, 0) + 1
        what = {
            ErrorKind.RATE_LIMIT: f"{label(name)} appears to have reached its current usage limit.",
            ErrorKind.AUTH: f"{label(name)} is not authenticated.",
            ErrorKind.UNAVAILABLE: f"{label(name)} is unavailable.",
            ErrorKind.TIMEOUT: f"{label(name)} timed out.",
        }.get(result.error_kind, f"{label(name)} stopped unexpectedly.")
        msg = f"{what}\n\nReason:\n{result.error}"
        if result.last_output.strip():
            msg += f"\n\nLast relevant output:\n{result.last_output.strip()[-1200:]}"
        self.ui.say(msg, "error")

        alt = routing.fallback(name, self.cfg, set(self.runtimes), self._usage_map(), failures)
        can_retry = failures[name] < 10**6
        while True:
            options = (["Retry"] if can_retry else []) + \
                      ([f"Switch to {label(alt)}"] if alt else []) + \
                      (["Choose runtime"] if len(self.runtimes) > 1 else []) + ["Inspect logs", "Stop"]
            question = (f"Recommended fallback: {label(alt)}" if alt and not can_retry
                        else "How do you want to continue?")
            choice = await self._ask_choice(question, options, event="warning")
            if choice == "Retry":
                return name
            if choice.startswith("Switch to") and alt:
                return alt
            if choice == "Choose runtime":
                pick = await self.ui.choose("Choose runtime", [label(n) for n in self.runtimes] + ["Cancel"])
                for n in self.runtimes:
                    if label(n) == pick:
                        return n
                continue
            if choice == "Inspect logs":
                self.ui.say(f"Logs: {self._log_dir(task)}", "detail")
                continue
            self._set(status="STOPPED")
            return None

    async def _finish(self, task: Task, result: ExecutionResult, changed: list[str], offer_push: bool = True) -> None:
        final = result.final or {}
        workdir = Path(task.workdir or self.root)
        changed = gitops.changed_since(self._turn_before, gitops.snapshot(workdir)) or changed
        if final.get("summary"):
            self.ui.say(final["summary"], "markdown")  # the agent's reply to the user
        for heading, key in (("Decisions", "decisions"), ("Problems found", "problems")):
            if final.get(key):
                self.ui.say(heading + ":\n" + "\n".join(f"• {x}" for x in final[key]), "detail")

        uncommitted = set(gitops.changed_files(workdir))
        pending = sorted((set(task.pending) | set(changed)) & uncommitted)
        self._set(pending=pending)
        commit = None
        if changed:
            self._set(status="REVIEW")
            self.ui.say(gitops.diff_stat(workdir) + "\n\nType /diff to see the full diff.", "detail")
            commit = await self._commit_flow(task, workdir, pending, final.get("commit_message") or "")
            if commit:
                self._set(pending=[], commit=commit)
                if task.worktree:
                    commit = self._land(task, commit)
                else:
                    self.ui.say(f"Committed {commit} on {gitops.current_branch(workdir)}.", "success")
                self._refresh_graft_nodes(workdir)
                if offer_push:
                    await self._offer_push(self.root if task.worktree else workdir)
            else:
                self.ui.say("Changes left uncommitted. They will be offered again with the next change.",
                            "detail")
            await self._remember(task, final, changed, commit)
            self.alerts.alert("completed", "BOOST_AI: changes ready", f"{task.id} {task.title}")
        self._set(status="DONE")

    def _land(self, task: Task, commit: str) -> str:
        """Bring a worktree commit onto the user's branch (fast-forward or cherry-pick). If it cannot
        be applied cleanly nothing is lost: it stays in the worktree and the user is told how to apply it."""
        assert task.branch
        try:
            landed = gitops.land(self.root, commit, task.branch)
        except gitops.GitError as exc:
            task.unlanded.append(commit)
            self.store.save(task)
            self.ui.say(f"Committed {commit} in the worktree, but it could not be applied to {task.branch} "
                        f"({str(exc).splitlines()[-1][:160]}). It is kept; apply it yourself with:\n"
                        f"  !git cherry-pick {commit}", "warning")
            return commit
        self._set(commit=landed)
        self.ui.say(f"Committed {landed} on {task.branch}.", "success")
        return landed

    async def _commit_flow(self, task: Task, workdir: Path, files: list[str], proposed: str) -> str | None:
        if self.auto_commit:  # standing approval from the user: the agent's files, the agent's message
            message = gitops.sanitize_message(proposed) or f"chore: {task.title}"
            try:
                gitops.add_paths(workdir, files)
                commit = gitops.commit(workdir, message)
                self.ui.say(f"Auto-commit: {message.splitlines()[0]}", "detail")
                return commit
            except gitops.GitError as exc:
                self.ui.say(f"Auto-commit failed ({exc}); asking instead.", "warning")
        others = set(gitops.changed_files(workdir)) - set(files)
        question = ("Run git add . ?" if not others else
                    f"Stage the {len(files)} files changed by the agent? "
                    f"({len(others)} other uncommitted files of yours are left alone)")
        if await self._ask_choice(question, ["Yes", "No"]) != "Yes":
            return None
        gitops.add_paths(workdir, files)
        message = gitops.sanitize_message(proposed) or f"chore: {task.title}"
        message = await self.ui.ask_text("Commit message", default=message, multiline=True)
        while True:
            choice = await self._ask_choice("Create this commit?", ["Commit", "Edit", "Cancel"])
            if choice == "Commit":
                try:
                    return gitops.commit(workdir, message)
                except gitops.GitError as exc:
                    self.ui.say(f"Commit failed: {exc}", "error")
                    continue
            if choice == "Edit":
                message = await self.ui.ask_text("Commit message", default=message, multiline=True)
                continue
            self.ui.say("Commit cancelled. Changes remain staged.", "detail")
            return None

    async def _remember(self, task: Task, final: dict, files: list[str], commit: str | None) -> None:
        record = ImplementationRecord(
            project=self.root.name, task_id=task.id, task_title=task.title,
            summary=final.get("summary", ""), timestamp=now_iso(),
            micro_specs=[MicroSpecRef(_ms_id(task), _short_title(task.messages[-1] if task.messages else task.text))],
            decisions=final.get("decisions") or [], files_changed=files,
            problems=(final.get("problems") or []) + self.review_notes,
            architecture_notes=final.get("architecture_notes") or [],
            branch=task.branch, commit=commit, level=f"L{task.level}", runtime=task.runtime,
            verification=self.last_report.text() if self.last_report else None,
            status="implemented" if commit else "uncommitted",
        )
        try:
            where = await self.memory.write_implementation(record)
            self.ui.say(f"Memory saved: {Path(where).relative_to(self.root)}", "detail")
        except Exception as exc:  # memory is optional; never fail the task
            self.ui.say(f"Memory could not be saved ({exc}). The implementation is unaffected.", "warning")

    # ── user shell commands ─────────────────────────────────────────────────
    async def run_command(self, command: str, confirm: bool = False, cwd: Path | None = None,
                          approved: bool = False) -> int | None:
        """Run a shell/git command the user asked for, in the project root. No model involved.
        Dangerous commands are always confirmed; `confirm` asks for the others too.
        Returns the exit code, or None if not run."""
        if command.strip() == "git push":
            command = self._push_command(cwd)
            if command is None:
                return None
        dangers = [] if approved else safety.check(command)  # `approved`: the user just said yes
        if dangers:
            force = safety.is_force_push(command)
            question = (f"⚠ This command is potentially destructive:\n\n    {command}\n\n"
                        f"Risk: {'; '.join(d.reason for d in dangers)}")
            if force:
                question += "\n\nThis REWRITES REMOTE HISTORY and can destroy other people's work."
            run = "Run force push" if force else "Run anyway"
            if await self._ask_choice(question, ["Cancel", run], event="warning") != run:
                self.ui.say("Not run.", "detail")
                return None
        elif confirm:
            if await self._ask_choice(f"Run this command in {self.root.name}?\n\n    {command}",
                                      ["Run", "Cancel"]) != "Run":
                self.ui.say("Not run. To give the agent a task instead, describe what you want changed.",
                            "detail")
                return None
        self.ui.progress(f"running  {command}")
        env = {**verify.project_env(self.root), "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
               "PAGER": "cat", "GIT_PAGER": "cat"}
        code, output = await verify.run_shell(command, cwd or self.root, 300, env)
        lines = output.rstrip().splitlines()
        shown = "\n".join(lines[-40:]) if lines else "(no output)"
        if len(lines) > 40:
            shown = f"… {len(lines) - 40} earlier lines\n" + shown
        self.ui.say(f"$ {command}\n{shown}" + ("" if code == 0 else f"\n(exit status {code})"),
                    "success" if code == 0 else "error")
        self.publish()  # e.g. the branch may have changed
        return code

    def _push_command(self, cwd: Path | None = None, quiet: bool = False) -> str | None:
        """`git push`, or `git push -u <remote> <branch>` for a branch with no upstream yet.
        None when there is no remote (explained unless `quiet`)."""
        cwd = cwd or self.root
        remotes = gitops.git(cwd, "remote", check=False).split()
        if not remotes:
            if not quiet:
                self.ui.say("This repository has no remote to push to. Add one first:\n"
                            "  !git remote add origin <url>", "warning")
            return None
        if gitops.git(cwd, "rev-parse", "--abbrev-ref", "@{upstream}", check=False).strip():
            return "git push"
        remote = "origin" if "origin" in remotes else remotes[0]
        return f"git push -u {remote} {shlex.quote(gitops.current_branch(cwd))}"

    async def _offer_push(self, workdir: Path) -> None:
        """After a commit: ask whether to push. Never automatic; the answer is the authorization."""
        if not (self.cfg.get("git") or {}).get("offer_push", True):
            return
        command = self._push_command(workdir, quiet=True)
        if command is None:
            return
        branch = gitops.current_branch(workdir)
        target = command.split()[3] if command.startswith("git push -u") else "its upstream"
        if await self._ask_choice(f"Push {branch} to {target}?", ["Push", "Not now"]) == "Push":
            await self.run_command(command, cwd=workdir, approved=True)
        else:
            self.ui.say("Not pushed. You can push later: !git push", "detail")

    # ── helpers ─────────────────────────────────────────────────────────────
    def _record(self, kind: str, text: str) -> None:
        if self.task is not None:
            try:
                self.store.record(self.task.id, kind, text)
            except OSError:
                pass  # the transcript is a convenience; never fail a task over it

    def reset(self) -> None:
        """Close the current conversation. Its transcript and memory stay; its worktree is removed
        unless it is dirty or holds commits not yet on the user's branch (nothing is ever lost)."""
        t = self.task
        if (t and t.worktree and t.workdir and Path(t.workdir).is_dir() and not t.unlanded
                and not gitops.is_dirty(Path(t.workdir))):
            try:
                gitops.remove_worktree(self.root, Path(t.workdir))
            except gitops.GitError:
                pass
        self.task, self.last_report, self.review_notes = None, None, []
        self._model_override = {}
        self.publish()

    def _attachments(self, task: Task) -> Path:
        return self.root / ".boost-ai" / "state" / "attachments" / task.id

    def _attach(self, task: Task, text: str, files: list[Path]) -> None:
        """Copy this message's files (attached in the tray, or referenced in the text) so the run
        is reproducible and readable from any worktree. Only this message's files go to the agent."""
        self._turn_files = []
        found = list(dict.fromkeys([Path(f).resolve() for f in files if Path(f).is_file()]
                                   + find_attachments(text, self.root)))
        if not found:
            return
        target = self._attachments(task)
        target.mkdir(parents=True, exist_ok=True)
        for src in found:
            dest = target / f"{len(task.attachments) + 1:02d}-{src.name}"
            shutil.copy2(src, dest)
            task.attachments.append(str(dest))
            self._turn_files.append(dest)
        self.store.save(task)
        self.ui.say("Attached: " + ", ".join(p.name.split("-", 1)[1] for p in self._turn_files), "detail")

    def _triage_text(self, text: str) -> str:
        """The message plus the text of this turn's attached files (images/PDFs excluded)."""
        budget, parts = TRIAGE_ATTACHMENT_CHARS, [text]
        for path in self._turn_files:
            if budget <= 0 or path.suffix.lower() in RICH_EXTENSIONS:
                continue
            try:
                body = path.read_text(errors="replace")[:budget]
            except OSError:
                continue
            parts.append(body)
            budget -= len(body)
        return "\n\n".join(parts)

    def _prepare_graft(self, workdir: Path) -> None:
        """Make sure graft has a graph for this directory (fresh worktrees need one). Never fatal."""
        if not graft.available(self.cfg):
            return
        self.ui.progress("graft: checking the code graph")
        if error := graft.ensure(workdir):
            self.ui.say(f"graft could not build the code graph ({error}); the agent explores without it.",
                        "warning")

    def _refresh_graft_nodes(self, workdir: Path) -> None:
        """After a commit, update graft's concept nodes in the background. Never blocks or fails a task."""
        env = graft.deep_env(self.cfg) if graft.available(self.cfg) else None
        if env is None or (self._graft_job and not self._graft_job.done()):
            return

        async def job() -> None:
            error = await graft.deep(workdir, env)
            if error:
                self.ui.say(f"graft could not update the concept nodes ({error}).", "warning")
            else:
                self.ui.say("graft concept nodes updated (graft viz to browse them).", "detail")

        self._graft_job = asyncio.create_task(job())

    def _attachments_section(self) -> str:
        if not self._turn_files:
            return ""
        return ("\n\n## Attached files\nThe user attached these files; read them with your tools as needed:\n"
                + "\n".join(f"- {p}" for p in self._turn_files))

    def _ensure_capable_runtime(self, task: Task) -> None:
        """Images/PDFs need a multimodal model: move off a text-only runtime (and say so)."""
        if not any(p.suffix.lower() in RICH_EXTENSIONS for p in self._turn_files):
            return
        current = self.runtimes.get(task.runtime or "")
        if current is None or current.vision:
            return
        policy = self.cfg["routing"]["policy"].get(f"L{task.level}", [])
        order = policy + [n for n in self.cfg["routing"].get("escalation", []) if n not in policy]
        alt = next((n for n in order if n in self.runtimes and self.runtimes[n].vision), None)
        if alt:
            self.ui.say(f"{label(task.runtime)} cannot read images or PDFs → this conversation continues "
                        f"with {label(alt)}.", "warning")
            self._set(runtime=alt)
        else:
            self.ui.say(f"{label(task.runtime)} cannot read images or PDFs and no multimodal runtime is "
                        "available; it will only see the file paths.", "warning")

    def _usage_map(self) -> dict[str, Usage]:
        return {n: self.usage.get(n)[0] for n in self.runtimes}

    def _log_dir(self, task: Task) -> Path:
        path = self.root / ".boost-ai" / "state" / "logs" / task.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _metric(self, task: Task, name: str, result: ExecutionResult, escalated_from: str | None,
                report: verify.Report | None = None, role: str = "implement") -> None:
        passed = sum(c.passed or 0 for c in report.checks) if report else None
        failed = sum(c.failed or 0 for c in report.checks) if report else None
        metrics.record(self.metrics_path, task_id=task.id, micro_spec_id=_ms_id(task), project=self.root.name,
                       level=f"L{task.level}", risks=task.risks, runtime=name, attempt=task.attempts,
                       model=result.model, start=self._attempt_start, duration=round(result.duration, 2),
                       success=result.ok and (report is None or report.ok or not report.configured)
                       and (result.final or {}).get("status") != "blocked"
                       and (result.final or {}).get("verdict") != "changes_requested",
                       error_kind=result.error_kind.value if result.error_kind else None,
                       input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                       estimated_cost=result.cost, tests_passed=passed, tests_failed=failed,
                       escalated_from=escalated_from, role=role)


def _ms_id(task: Task) -> str:
    # Each turn of the conversation is one micro-spec of the task.
    return f"{task.id}-MS{max(task.turns, 1):02d}"


def _short_title(text: str) -> str:
    return Task(id="", text=text).title


def ensure_project_dirs(root: Path) -> None:
    base = root / ".boost-ai"
    (base / "state").mkdir(parents=True, exist_ok=True)
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text("state/\nworktrees/\n")
