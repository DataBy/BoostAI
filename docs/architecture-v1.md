# BOOST_AI — Architecture V1

A minimal local harness that gives the right task, context, tools and verification loop
to the right AI runtime. One Python package, the filesystem, Git and subprocesses.
Dependencies: `textual`, `typer`, `pyyaml`.

## Flow

```text
TUI ─ task text
  → triage.py        deterministic level L0–L4 + risk flags (keyword rules, EN/ES)
  → routing.py       pure function: policy[level] minus unavailable/exhausted/failed runtimes
  → gitops.py        L3+ → detached worktree .boost-ai/worktrees/T-NNNN; commits land on the user's branch
  → context.py       prompt = task + capped AGENTS.md excerpt + verify commands + rules
  → runtimes/*.py    agent CLI as subprocess, structured final JSON, tokens, progress
  → verify.py        configured commands; compressed summary to UI/model, raw logs on disk
       fail → retry same runtime with failure summary (attempts_per_runtime)
       fail again → ASK: escalate / retry / stop
       quota/auth/outage → ASK: switch (recommended fallback) / choose / logs / stop
       runtime "blocked" → ask the user its question, re-run with the answer
       dangerous command inside the agent → safety hook → ASK: deny / allow once
  → review (L≥3)     a different runtime reviews read-only; changes requested → ASK: fix / accept / stop
  → result           summary, decisions, diff stat, /diff
  → ASK  "Run git add . ?"  → editable Conventional Commit message → [Commit][Edit][Cancel]
  → memory.py        ImplementationRecord → MemoryBackend (failures never fail the task)
  → metrics.py       one JSONL row per attempt
  → alerts.py        notify-send + short sound on needs_input / completed / warning
```

`orchestrator.Harness` owns this loop and talks to the user only through a five-method
`UI` protocol (`say`, `progress`, `status`, `choose`, `ask_text`). The TUI implements it;
tests use a scripted implementation.

## Components

| Module | Responsibility |
|---|---|
| `cli.py` | `boost-ai` (TUI), `init`, `doctor`, `status`, `stats` |
| `config.py` + `defaults.yaml` | defaults → `~/.config/boost-ai/config.yaml` → `.boost-ai/config.yaml` (deep merge) |
| `task.py` | `Task` dataclass; one JSON file per task; `current.json` for status/indicator |
| `triage.py` | Level + risk rules. Risk floors (auth→L3, migration→L4) beat triviality |
| `routing.py` | `select`, `escalate`, `fallback` — no I/O, fully unit-tested |
| `runtimes/` | `base.py` interface, `UsageBook`, process helper; one adapter per provider |
| `context.py` | Progressive disclosure: no repo dumps; the runtime explores itself |
| `verify.py` | Deterministic checks, output compression, project venv activation |
| `safety.py` | Dangerous-command patterns (harness commands and agent hook) |
| `hook.py` | Claude `PreToolUse` hook + file-based approval protocol with the running harness |
| `gitops.py` | Worktrees, diff, `git add`, commit with sanitized message. **No push function exists.** |
| `memory.py` | `MemoryBackend` interface + `LocalMemoryBackend` |
| `metrics.py` | JSONL append + aggregates |
| `alerts.py` | Notifications and sounds, fire-and-forget |
| `tui/` | Textual app, `theme.tcss` |

## Runtime abstraction

```python
class Runtime(ABC):
    name: str
    def installed(self) -> bool: ...
    async def execute(self, request: ExecutionRequest, progress) -> ExecutionResult: ...
```

`ExecutionResult` carries `ok`, the structured `final` object, token counts, and on
failure an `ErrorKind` (`rate_limit`, `auth`, `unavailable`, `timeout`, `crash`) plus a
human-readable reason and the last relevant output. Every runtime is asked for the same
final JSON (`RESULT_SCHEMA`: status, summary, commit_message, decisions, problems,
architecture_notes, blocker). That one answer feeds the commit message and the memory
record, so neither needs an extra LLM call.

Implemented, both on the local CLI login (no API keys):

- **Codex**: `codex exec --json -s workspace-write --output-schema` (`-s read-only` for review).
- **Claude Code**: `claude -p --output-format stream-json --json-schema --permission-mode
  acceptEdits` with an allow-list of tools, `git commit/push/reset/rebase` denied, the
  safety hook registered through `--settings`, and `--strict-mcp-config
  --disable-slash-commands`. That last pair keeps your global MCP servers and skills out of
  the agent's context, which measured 20.7k → 12.0k fixed tokens per turn
  (`runtimes.claude.global_extensions: true` turns them back on). Review mode removes the
  edit tools.

- **Antigravity** (opt-in, `runtimes.antigravity.enabled`): `agy --output-format stream-json
  --json-schema … -p <prompt>`, model `gemini-3.8-flash-medium`. In headless mode `agy`
  denies every shell command unless all permissions are skipped. Neither hook `allow`
  decisions nor a project-level `settings.json` grant permission; both were tested. So the
  adapter runs with `--dangerously-skip-permissions` and, for the duration of the run only,
  installs `.agents/hooks.json` in the workspace. This makes the BOOST_AI hook an
  **allow-list gate for every tool**:
  - shell: screened by `safety`; dangerous commands go to the user.
  - file writes: allowed only inside the workspace, and blocked entirely during review.
  - reads and search: allowed.
  - everything else (browser, MCP, web, sub-agents, scheduling, messaging): denied.
  - unverifiable calls: denied.

  An existing project hooks file is merged and restored byte-for-byte.

- **DeepSeek** (`runtimes/deepseek.py`, needs `DEEPSEEK_API_KEY` in your shell):
  - **As an agent:** the Claude Code CLI with `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`,
    set only for that process. Your Claude login is never used for it. Tools, the safety hook,
    git, sessions and structured output work as with Claude.
  - **Models:** `deepseek-flash` for L0/L1, `deepseek-v4-pro` above.
  - **Billing and usage:** billed to your DeepSeek balance. The Usage field shows the exact
    balance (`$2.79 left`); the cost Claude Code computes at Anthropic prices is discarded.
  - **As a plain API:** a stdlib client (`chat_json`) used by triage. An empty JSON answer is
    retried once.

Requests can ask for `read_only` and a custom `schema` (used by review). Adding one means
one file in `runtimes/` plus one line in `runtimes/__init__.py`.

## Routing and usage

Policy and escalation chain live in config (`routing.policy`, `routing.escalation`).
Runtimes not installed or not implemented are skipped and the reason is shown. Usage
state comes from evidence only (`UsageBook`, `~/.local/share/boost-ai/usage.json`):
`UNKNOWN` until observed, estimates labelled `(est.)`. `EXHAUSTED` expires after 2 h.
Claude reports exact utilization per window (`rate_limit_event`), shown as e.g.
`29% · 5h AVAILABLE`. At ≥75% (CONSERVE) the user is told. No runtime switch is ever silent.

## Review

Only for `review.min_level` (default L3) and above. The first runtime in `review.runtimes`
that differs from the implementer runs read-only with `REVIEW_SCHEMA` (verdict, summary,
findings with severity). The reviewer gets the task, the implementer's summary and the
verification result, and inspects `git diff` itself instead of receiving the diff in the
prompt. If it requests changes, the user decides: fix (the implementer gets the findings
and verification runs again), accept, or stop. Findings go into the memory record's
problems. If the reviewer changes any file, the user is warned.

## State

Plain files, no database:

```text
.boost-ai/
  config.yaml            project config (commit it)
  .gitignore             ignores state/ and worktrees/
  skills/ specs/         reserved, used later
  state/tasks/T-NNNN.json
  state/current.json     what is running now (status command, future indicator)
  state/logs/T-NNNN/     raw runtime event streams, verification logs
  state/memory/          local memory backend
  worktrees/             git worktrees for isolated tasks
~/.local/share/boost-ai/metrics.jsonl, usage.json
```

SQLite was rejected: there is a single writer and no ad-hoc queries; JSONL plus a
30-line aggregator answers the routing questions.

## Worktree strategy

L3+ conversations (`routing.worktree_min_level`) run in `.boost-ai/worktrees/<id>`, a
**detached** checkout of the user's branch. It has no branch of its own, so the user only ever
sees their own branches. Every approved commit **lands on the user's branch** right away: a
fast-forward when the branch has not moved (the very same commit), otherwise a cherry-pick.
A branch that is not checked out only accepts a fast-forward. If a commit cannot be applied
cleanly (a conflict, or the user's uncommitted files are in the way), nothing changes on the
branch: the commit stays in the worktree, the user gets `!git cherry-pick <sha>`, and `/new`
keeps that worktree. Otherwise `/new` removes it (and also keeps it while it is dirty). Other
levels, and a detached main checkout, work in place. The project's `.venv` is put on `PATH`
for runtimes and verification, because worktrees don't contain it. `.boost-ai/` itself is never
part of a task's change set.

## Safety boundaries

- **Harness-run commands** (verification) pass `safety.check`; dangerous ones are refused.
- **Claude's commands** pass through `python -m boost_ai.hook` (`PreToolUse`, Bash). Safe
  commands run. A dangerous one makes the hook write `<id>.request.json` into the task's
  `approvals/` directory. The harness shows it with the warning sound (force push gets an
  extra warning) and writes `<id>.response.json`. Denied or unanswered commands are blocked
  and the agent is told why. With no harness running, dangerous commands are always blocked.
  If the hook input can't be read, the command is blocked.
- **Antigravity's tools** all pass the same hook as an allow-list (see Runtime abstraction).
  Residual risk: shell commands that aren't in the dangerous-pattern list run unsandboxed,
  the same as Claude's Bash. If BOOST_AI is killed with SIGKILL during a run, the temporary
  `.agents/hooks.json` can be left in that workspace.
- **Codex's commands** can't be intercepted. They are contained by its sandbox
  (`-s workspace-write`; the worktree's `.git` lives outside the writable root, so agent
  commits fail).
- For all runtimes: worktree isolation, prompt rules, and a check after each run that warns
  if HEAD moved.
- Commits only after `Run git add . ?` → editable message → `Commit`. AI-attribution lines
  are stripped. The author is the user's Git identity.
- No push code path exists.

## Conversations: agents resolve, the harness guarantees

**A task is a conversation.** Every message you send is a turn handled by the routed
agent, which decides what the request needs: answering, running commands, using git or
changing code. BOOST_AI has no rules about what you may ask. Its job is to add
guarantees:

| Turn outcome | What the harness adds |
|---|---|
| Answer, commands, git (no files changed) | The agent's reply. Pushes and destructive commands were already approved by you through the hook |
| Files changed | Verification → retries / stronger model / escalation → review if the conversation is L3+ → diff → commit with your approval |

- **Triage:** deterministic risk rules give a floor (auth → L3, migrations, payments and
  production → L4). When `triage.llm: deepseek` is set and a key exists, a ~300-token
  DeepSeek call refines the level (a greeting becomes L0, "add hello.py" becomes L1). It runs
  off the event loop and is never below the floor; on any failure the rules are used alone.
- **Routing:** L0/L1 → DeepSeek; L2+ → Claude Code; Codex is the fallback and the reviewer.
  If a conversation's level rises past what its runtime is listed for, it moves to the right
  runtime (you are told, and the new session gets the earlier requests).
  The first message picks the runtime via the level policy. The default is
  Claude Code, because its hook can ask you before a push or a destructive command, so it
  can use git freely; Codex's sandbox can't touch `.git`. The model depends on the level
  (`runtimes.claude.models`: Haiku for L0/L1, Sonnet for L2, Opus for L3/L4). The
  conversation's level only rises, and a rise switches the model on the next turn.
- **One session per conversation:** each turn resumes the agent's session and sends only
  the new message, so the shared context comes from the provider cache (measured: 97%
  cached on follow-up turns). A fresh session after a runtime switch gets the earlier
  requests. `/new` closes the conversation and removes its worktree unless something there is not yet on
  your branch.
- **Workspace:** L3+ conversations get an isolated, branchless worktree whose approved commits
  land on your branch; others work in your checkout, so "create a branch" and "push" act on
  your repo. BOOST_AI never creates branches of its own.
- **After an approved commit BOOST_AI asks "Push <branch> to origin?"** [Push] [Not now]. It never
  pushes on its own; it sets the upstream for a new branch, asks nothing when there is no remote,
  and `git.offer_push: false` turns the question off.
- **Commits stay BOOST_AI's flow.** Agents can't run `git commit`, so the message is
  editable, Conventional and free of AI attribution. Only the files the agent changed are
  staged; your own uncommitted work is left alone. Declined changes are offered again with
  the next change.
- **Questions from agents** carry `blocker_options`, shown as buttons plus **Stop task**.
- **Memory:** each turn that changes code is one micro-spec (`T-0001-MS03`), giving
  Project → Task (conversation) → MicroSpec (turn) → Implementation.
- **`!command`** is the only bypass: you run something yourself, with no model involved.
  Dangerous commands are still confirmed, and `!git push` sets the upstream for a new branch.
- **Clear (Warp-style):** `clear`, `/clear` or **Ctrl+L** clears the screen only; the conversation,
  agent session and history continue, and a pending question stays visible. `/new` starts over.
- **History:** `/history`, `/open T-0003` (read-only replay of the local transcript),
  `/new`.
- **Redaction** (`redact.py`): API keys, bearer and JWT tokens, private keys and
  `password/token/secret=` values are masked before anything is written to transcripts,
  runtime logs, verification logs or memory.

## Branch dropdown (Warp-style)

Click the branch in the status bar (shown as `main ▾`) or press **Ctrl+B**. A dropdown
lists local branches (most recently used first, current one marked) and remote branches
you don't have locally (`remote`). Type to filter, or choose **＋ Create branch "…"**
when the name doesn't exist. Selecting runs `git switch` / `git switch -c` directly, with
no model involved, in the conversation's workspace, and the bar refreshes. It is disabled
while an agent is working; git's own refusals (for example conflicting uncommitted
changes) are shown as they are.

## Top-bar indicator

`indicator.py` is a standalone script (stdlib + PyGObject) run with the system Python
(`indicator.python`, default `/usr/bin/python3`), because the project venv has no
PyGObject. It needs `gir1.2-ayatanaappindicator3-0.1` and GNOME's AppIndicator extension;
`boost-ai doctor` checks both.

- The TUI starts it, passing its own PID. The indicator exits when that process ends, so
  no daemon is needed.
- Every second it reads `state/current.json` and shows a label (`BOOST_AI · CODEX ·
  RUNNING`, `BOOST_AI · NEEDS YOU`), a status icon, and a menu with project, task,
  runtime and status.
- Menu actions **Stop task** and **Quit BOOST_AI** write `state/control`, which the TUI
  consumes. **Hide indicator** closes just the indicator.
- There is no "Open BOOST_AI", because Wayland doesn't let one app raise another app's
  window. There is no "Pause", because freezing an agent mid-request breaks its API
  connection; Stop is the reliable control.
- Disable it with `indicator.enabled: false`.

## Attachments (any file type)

**In the TUI** attachments are visible and editable before sending: a tray of chips
(`📎 spec.pdf ✕`) sits above the prompt.

- **Add:** the **＋** button or **Ctrl+O** opens the native GNOME file chooser (zenity,
  multi-select). Dropping a file onto the terminal or pasting its path turns it into a chip
  immediately, and so does `/attach <path>`.
- **Remove:** click a chip's **✕**, or press **Backspace** on an empty prompt to remove the last one.
- **Send:** the tray goes with the next message and then clears. Paths written inside the
  message text are still detected, as below.

`task.find_attachments` detects files that exist and are referenced in a message:
dropped or pasted paths (including `\ `-escaped spaces), quoted paths, `file://` URIs, `~`
and project-relative paths. Any type is accepted (PDF, CSV, logs, JSON, docx, code,
images…), up to 50 MB. Files git already tracks in the project are skipped, since the agent
reads them in place, and so is anything under `.boost-ai/`. Copies go to
`.boost-ai/state/attachments/<task>/`, so they are reproducible and readable from any worktree.

The turn's request lists them under "Attached files" and the agent reads them with its own
tools:

- **Claude:** `Read` handles text, images, PDF and notebooks.
- **Codex:** images via `-i`; everything else through its shell.
- **Antigravity:** `view_file`.

Images and PDFs need a multimodal model. If the conversation is on a text-only runtime
(DeepSeek), it moves to the next capable runtime and says so. Reviewers don't get
attachments.

## Plan before changes

L2+ turns (`plan.min_level`) and any turn with attached files (`plan.attachments`) start with a
read-only planning run by the routed runtime (`PLAN_SCHEMA`). The user sees the plan and the
skills it will use, then picks **Approve**, **Request changes** (the planner revises it in the same
session) or **Stop**. Nothing is written before approval. On approval the same session is
resumed to implement (it has already explored the code). A planner may also answer directly
(`status: answer`) or ask a question (`status: blocked`).

The plan is rendered as Markdown (headings, bold, tables) and always ends with its ordered
**tasks**. After approval they run one at a time: execute → verify → review (L3+) → commit,
with a ○/✓ checklist and `Progress k/N` in the status bar. A stopped task stops the plan;
earlier tasks keep their commits. Commits are asked for one by one unless **auto-commit** is on
(`Commits` in the status bar: click it, press Ctrl+T or use `/autocommit on|off`; default
`git.auto_commit`). Auto-commit stages only the agent's files and uses the agent's message.
Pushing is never offered during a plan and never automatic: the user asks for it (`!git push`).

Triage reads the text of attached files too (up to 20k chars), and a long description is a
floor of L2 even when the LLM triage says otherwise: "implement this" + a big spec is not L0.

## The harness layer

Skills and rules live in BOOST_AI's own layer, `harness/` beside the package, split by category,
never in another agent's folder (`~/.claude`, `~/.agents`, `~/.codex` are neither read nor written):

```
harness/skills/<category>/<skill>/SKILL.md     .boost-ai/skills/<category>/<skill>/SKILL.md
harness/rules/<category>/*.md                  .boost-ai/rules/<category>/*.md
```

The left side applies to every project; the right side is the project's own (versioned) and
overrides an entry with the same name (skills) or `category/file` (rules). Files outside a
category folder are ignored.

## Rules (non-negotiable)

The counterpart of skills. Unlike skills they are always sent in full, never truncated, to
planning, implementation and review. The reviewer treats a violation as at least "high".
`/rule [category:] <text>` appends to `.boost-ai/rules/<category>/project.md` (default
category `general`), and `/rules` lists them.

## Code context: graft

Every runtime gets graft's MCP server and nothing else (Claude/DeepSeek: `--mcp-config` +
`--strict-mcp-config`; Codex: `-c mcp_servers.graft.*`, tools auto-approved because they are
read-only). The prompt also carries `graft map`, plus an instruction to explore through graft
before reading files. A directory without a graph (a new worktree) gets `graft build` first ($0).
After each approved commit, `graft build --deep` refreshes the concept nodes in the
background (incremental, billed to DeepSeek; `context.graft_deep`). Browse them with
`graft viz`. Any graft failure is a warning; the agent then works without it.

## Skills

Skills come from the harness layer and the project (see above). Prompts carry only
`name [category]: description (path)` (13 skills ≈ 390 tokens, sent only when a session
starts). The agent reads a `SKILL.md` when relevant; Claude gets `harness/skills` through
`--add-dir`. `/skills` lists them by category. When a turn is planned, only the skills the
approved plan names are offered to the implementation.

BOOST_AI ships a curated, pinned **suite** (in `harness/skills/`, listed with origin, commit and
license in `harness/skills/SUITE.md`): architecture (codebase-design, improve-codebase-architecture,
domain-modeling), frontend (frontend-design, React best practices, composition patterns, web
design guidelines), security (differential-review, sharp-edges, supply-chain-risk-auditor), QA
(tdd, verification-before-completion, property-based-testing, webapp-testing), interviewing
(grilling), debugging (systematic-debugging), code review (code-review,
receiving-code-review), and from Anthropic, project setup (claude-automation-recommender, which is
Claude Code Setup, and claude-md-improver) and building with AI (claude-api, mcp-builder,
skill-creator). Prompts tell the agent that the
harness owns planning, commits, branches and PRs, so it skips skill steps that do those.

`/interview <topic>` runs the grilling method read-only: rounds of numbered questions, each with
a recommended answer (Answer / Accept all recommendations / Write the spec now / Stop). The
agent looks facts up itself and asks only for decisions. It ends in
`.boost-ai/specs/<task>-<slug>.md`, which you can plan right away (it becomes the attached
spec of a new conversation).

`/skill add <owner/repo> <category> <name…|all>` shallow-clones the package and copies those
skills into `harness/skills/<category>/` (deterministic, no model); without a category it only
lists the package. `/skill remove <name…>` deletes them from the layer. `/skills recommend` asks
the agent to run Claude Code Setup's `claude-automation-recommender` (part of the suite).

## Memory

```python
class MemoryBackend(ABC):
    async def write_implementation(self, record: ImplementationRecord) -> str
    async def search_context(self, query: str, limit: int = 5) -> list[MemoryHit]
```

The hierarchy is Project → Task → MicroSpec → Implementation. `ImplementationRecord`
holds project, task, micro-specs, decisions, summary, files changed, problems,
architectural reasoning, branch, commit and timestamp.

`LocalMemoryBackend` writes `.boost-ai/state/memory/<project>/<task>.md`: YAML front
matter with the full structured record, then a Markdown body that already uses
`[[wiki links]]` (`[[T-0001]]`, `[[Project]]`, `[[T-0001-MS01]]`).

**Adding Logseq later:**

1. Create `LogseqMemoryBackend(MemoryBackend)` in `memory.py` or `memory_logseq.py`.
   - File graph: write `render_markdown(record)` (converted to Logseq outline bullets)
     into `<graph>/pages/<task_id>.md`, and properties as `key:: value` lines. This works
     with Logseq closed.
   - DB graph: call the Logseq HTTP API (needs the app running and a token from the
     environment). Return the page name as the locator.
   - `search_context`: grep the graph's `pages/` folder (file graph) or call the API search.
2. Register it in `backend_from_config` under `memory.backend: logseq` with a
   `memory.logseq.graph_path` setting.
3. Optional: a `MultiMemoryBackend` that writes local first, then Logseq, so the local
   copy survives Logseq outages.

The orchestrator already treats any memory error as a warning, so no other code changes.

## TUI

The screen has four parts: a centered ASCII logo, a status grid (Project, Branch, Task,
Status, Model, Level, Tests, Usage), a scrolling conversation, a single-line activity
spinner and the input. Questions appear inline as button rows, which can also be
answered by typing a prefix or number. Long text (the commit message) opens an inline
editor, saved with Ctrl+S. Slash commands: `/help /status /diff /tests /logs /stats
/stop /quit`.

## Not built in V1

Daemon, SQLite, vector search, web UI, LangChain/LangGraph, MCP servers other than graft,
Jira, Logseq integration (interface only), ML routing, multi-agent swarms, automatic
push of any kind.

## Next phases

2. Micro-spec decomposition of approved plans for L3/L4; L4 "Claude plans → Codex implements" step.
3. `LogseqMemoryBackend`; pipx packaging.
