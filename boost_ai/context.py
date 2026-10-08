"""Prompt construction with progressive disclosure.

A fresh agent session gets: the user's request (plus earlier requests of the
conversation), the project rules in full, a capped AGENTS.md excerpt, a graft repo map,
skill metadata, the verification commands and how to work inside the harness. Resumed
sessions get only what is new. The agent explores the rest through graft.
"""

from __future__ import annotations

from pathlib import Path

from . import graft, rules, skills
from .task import Task
from .triage import LEVEL_NAMES

RULES = """\
How to work (you are the user's engineering agent; BOOST_AI is the harness around you):
- Do what the user asks: answer questions, run commands, use git, or change code. The user is
  talking to you; there is no other assistant. Keep code changes minimal and focused.
- Work inside the current directory. Explore as needed; do not assume structure you have not checked.
- Git: use it as the request needs (status, branches, switch, merge, push…). Do NOT create commits:
  whenever files change, BOOST_AI shows the user the diff and asks them to commit, using your
  `commit_message`. Pushes and destructive commands (rm -rf, reset --hard, dropping data, sudo…)
  are sent to the user for approval automatically; if one is blocked, the user said no: do not retry.
- Ask only when the request is ambiguous in a way that materially changes the result: set status
  "blocked" with a precise `blocker` question, and list short answers in `blocker_options` when the
  question has a few of them (max 4). Otherwise act.
- Final answer: the requested JSON object. `summary` is your reply to the user, in the user's
  language: the answer, or what you did and its outcome. If you changed files, `commit_message` is
  a Conventional Commit (`type(scope): subject`, optional bullet body) with no AI attribution;
  otherwise leave it empty. Use empty arrays/strings for fields that do not apply."""

PLAN_RULES = """\
Plan first (read-only): do NOT modify files and do not run commands that change state.
- Understand the request and every attached file fully; explore the code through graft.
- status "ready": `plan` is a concise Markdown plan in the user's language (it is rendered: use
  headings, **bold** and tables where they help): goal, key decisions, files to create or change,
  risks, and how it will be verified. It must comply with the project rules. Do not list the
  tasks inside `plan`.
  `tasks`: the ordered implementation tasks, one short line each. Each task is implemented,
  verified and committed on its own before the next starts, so make each a small, coherent,
  committable unit that leaves the project working.
  `skills`: names of the listed skills the implementation needs (empty if none); only those
  will be used.
- status "answer": the request is a question or needs no changes; `plan` is your reply.
- status "blocked": something ambiguous materially changes the plan; set `blocker` (a precise
  question) and `blocker_options` (max 4 short answers).
Use empty arrays/strings for fields that do not apply."""

GRAFT_RULES = (f"This repository is indexed by graft. Get code context from its MCP tools ({graft.TOOLS}) "
               "before grepping or reading files; open a source file only to read or edit the span graft named.")

INTERVIEW_RULES = """\
Interview the user (read-only: do NOT modify files) until you reach a shared understanding of the
topic above, then write the spec. Method (the grilling skill): map the decisions as a design tree
and work it in rounds. Each round asks the whole frontier (every decision whose prerequisites are
settled), each question with your recommended answer, worded so "yes" accepts it. Finding facts is
your job, never the user's: look them up in the code (through graft) instead of asking. The
decisions are the user's.
- status "questions": `questions` is this round (`question` may span paragraphs and list choices;
  `recommended` is your recommendation). Leave `title` and `spec` empty.
- status "done" when every branch is settled or the user tells you to stop: `title` is a short
  name and `spec` a complete Markdown spec in the user's language: context, goals and non-goals,
  decisions (with the reason for each), requirements, acceptance criteria, open questions and risks.
  It must comply with the project rules."""

SKILL_NOTE = ("Skills run inside BOOST_AI: the harness owns planning, commits, pushes, branches, PRs and "
              "worktrees, so skip any skill step that does those. \"Call the Skill tool with X\" means: read X's "
              "SKILL.md from this list. If a skill dispatches sub-agents, do that work yourself, in sequence.")

REVIEW_RULES = """\
You are an independent reviewer. Another agent implemented the task above; the changes are
uncommitted in the current directory. Inspect them with `git diff HEAD` and `git status`
(new files are untracked) and read surrounding code as needed.
- Do NOT modify any file. Do not run git commands that change state.
- Focus on correctness, security, data safety, missed requirements and violations of the project
  rules (a rule violation is at least "high"). Ignore style nits.
- verdict "changes_requested" only for problems that should block the commit
  (any critical/high finding). Otherwise "approve", listing minor findings if useful.
- Tests already passed; do not re-run long suites unless needed to confirm a finding."""


def build_review_prompt(task: Task, root: Path, cfg: dict, implementer: str, final: dict,
                        verification: str) -> str:
    parts = [f"# Review of task {task.id} (L{task.level} {LEVEL_NAMES[task.level]}"
             + (f", risk: {', '.join(task.risks)})" if task.risks else ")"),
             task.text.strip(),
             f"## Implementer ({implementer}) summary\n{final.get('summary', '')}"]
    if final.get("decisions"):
        parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in final["decisions"]))
    parts += _project_sections(root, cfg)
    parts.append("## Verification (already run by the harness)\n" + verification)
    parts.append(REVIEW_RULES)
    return "\n\n".join(parts)


def build_continue_prompt(feedback: str = "", answer: tuple[str, str] | None = None,
                          message: str = "", plan: bool = False) -> str:
    """Delta prompt for a resumed session: the agent already has the conversation and context."""
    parts = []
    if message:
        parts.append(f"The user says:\n{message.strip()}")
    if answer:
        parts.append(f"The user answered your question.\nQ: {answer[0]}\nA: {answer[1]}")
    if feedback:
        parts.append("## Feedback on the current changes\n" + feedback)
    if plan:
        parts.append("Do not implement anything yet: plan it.\n" + PLAN_RULES)
    else:
        parts.append("Act on it in the same working directory, following the same rules. "
                     "Finish with the same JSON object.")
    return "\n\n".join(parts)


def approved_message(root: Path, cfg: dict, plan: str, skill_names: list[str], current: str) -> str:
    """What a resumed planning session gets once the user approves: the first task, with the full rules."""
    parts = ["I approve your plan:\n\n" + plan.strip(),
             f"Implement the tasks one at a time; each is verified and committed before the next.\n"
             f"{current}\nImplement only this task now."]
    parts.append(_skills_section(root, skill_names))
    parts.append(_verification_section(cfg))
    parts.append(RULES)
    return "\n\n".join(p for p in parts if p)


def _project_sections(root: Path, cfg: dict, workdir: Path | None = None) -> list[str]:
    """Rules (in full, mandatory), the AGENTS.md excerpt and, when given a workdir, graft's map."""
    parts = []
    if mandatory := rules.text(root):
        parts.append("## Project rules (non-negotiable: every change must comply)\n" + mandatory)
    excerpt = agents_md_excerpt(root, cfg.get("context", {}).get("agents_md_max_chars", 4000))
    if excerpt:
        parts.append("## Project instructions (AGENTS.md)\n" + excerpt)
    if workdir is not None and graft.available(cfg):
        repo_map = graft.repo_map(workdir)
        parts.append("## Code context (graft)\n" + GRAFT_RULES + (f"\n\n{repo_map}" if repo_map else ""))
    return parts


def _skills_section(root: Path, only: list[str] | None = None) -> str:
    """Skill metadata (the agent reads a SKILL.md itself). `only`: the skills an approved plan chose."""
    available = skills.discover(root)
    if only is not None:
        chosen = [s for s in available if s.name in only]
        return ("## Skills\nThe approved plan uses these skills; read each SKILL.md before starting and "
                f"use no other skill. {SKILL_NOTE}\n" + skills.index(chosen)) if chosen else ""
    return ("## Skills\nRead a skill's SKILL.md only if it is relevant to this task. "
            f"{SKILL_NOTE}\n" + skills.index(available)) if available else ""


def _verification_section(cfg: dict) -> str:
    commands = {k: v for k, v in (cfg.get("commands") or {}).items() if v}
    if commands:
        return ("## Verification\nThe harness will run these after you finish; run them yourself first:\n"
                + "\n".join(f"- {k}: `{v}`" for k, v in commands.items()))
    return ("## Verification\nNo automated checks are configured. Verify your change against the "
            "task's acceptance criteria and report anything you could not verify in `problems`.")


def agents_md_excerpt(root: Path, max_chars: int) -> str:
    path = root / "AGENTS.md"
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n… (truncated; read AGENTS.md for more)"
    return text


def build_prompt(task: Task, root: Path, cfg: dict, feedback: str = "",
                 answers: list[tuple[str, str]] | None = None, message: str = "", plan: bool = False,
                 approved: tuple[str, list[str], str] | None = None) -> str:
    """Full prompt for a fresh agent session. Earlier messages of the conversation are included
    (they are short) so a new session, e.g. after escalation, knows what was already asked.
    `plan`: a read-only planning run. `approved`: (plan, skills, current task) the user approved."""
    current = (message or (task.messages[-1] if task.messages else task.text)).strip()
    parts = [f"# Conversation {task.id} (L{task.level} {LEVEL_NAMES[task.level]})"]
    earlier = [m for m in task.messages[:-1]][-10:] if task.messages else []
    if earlier:
        parts.append("## Earlier requests in this conversation\n" + "\n".join(f"- {m.strip()}" for m in earlier))
    parts.append("## The user says\n" + current)

    if answers:
        parts.append("## Clarifications from the user\n" +
                     "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answers))

    if approved:
        parts.append("## Approved plan\n" + approved[0].strip())
        parts.append(f"## Your task now\n{approved[2]}\nImplement only this task; earlier tasks of the plan "
                     "are already done and committed.")

    parts += _project_sections(root, cfg, Path(task.workdir or root))
    parts.append(_skills_section(root, approved[1] if approved else None))
    parts.append(_verification_section(cfg))

    if feedback:
        parts.append("## Feedback on the current changes\nThe working tree already contains the previous "
                     "attempt. Address the feedback below without unrelated changes.\n\n" + feedback)

    parts.append(PLAN_RULES if plan else RULES)
    return "\n\n".join(p for p in parts if p)


def build_interview_prompt(task: Task, root: Path, cfg: dict, topic: str,
                           answers: list[tuple[str, str]] | None = None) -> str:
    """Fresh session for /interview. `answers`: rounds already answered (when a session is restarted)."""
    parts = [f"# Interview {task.id}", "## Topic\n" + topic.strip()]
    if answers:
        parts.append("## Rounds already answered\n" + "\n\n".join(f"{q}\nAnswer: {a}" for q, a in answers))
    parts += _project_sections(root, cfg, Path(task.workdir or root))
    grilling = next((s for s in skills.discover(root) if s.name == "grilling"), None)
    if grilling:
        parts.append(f"The full method is in the grilling skill: {grilling.path}")
    parts.append(INTERVIEW_RULES)
    return "\n\n".join(parts)
