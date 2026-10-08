from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from boost_ai import config
from boost_ai.runtimes.base import ExecutionRequest, ExecutionResult, Runtime


@pytest.fixture(autouse=True)
def no_desktop_indicator(monkeypatch):
    """Tests must never put a real indicator in the user's top bar."""
    monkeypatch.setattr("boost_ai.indicator.launch", lambda root, cfg: None)


@pytest.fixture(autouse=True)
def no_graft(monkeypatch):
    """Unit tests never build or query a real graft graph (test_graft opts back in)."""
    monkeypatch.setattr("boost_ai.graft.available", lambda cfg: False)


HARNESS_DIR = config.HARNESS_DIR


@pytest.fixture(autouse=True)
def isolated_harness(tmp_path, monkeypatch):
    """Tests see only the skills and rules they create; test_suite_is_discovered checks the real layer."""
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")


@pytest.fixture(autouse=True)
def no_plan_step(request, monkeypatch):
    """Mechanism tests go straight to execution; test_plan.py covers the plan step."""
    if not request.module.__name__.endswith("test_plan"):
        monkeypatch.setattr("boost_ai.orchestrator.Harness._needs_plan", lambda self, task: False)


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Never touch the real ~/.config or ~/.local/share during tests."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # never read the user's ~/.claude/skills etc.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # unit tests never spend API balance
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hello\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def cfg(repo) -> dict:
    c = config.load(repo)
    c["notifications"]["enabled"] = False
    c["sounds"]["enabled"] = False
    # Mechanism tests pin an explicit policy instead of depending on the shipped defaults.
    c["routing"]["policy"] = {"L0": ["codex", "claude"], "L1": ["codex", "claude"], "L2": ["codex", "claude"],
                              "L3": ["codex", "claude"], "L4": ["claude", "codex"]}
    c["routing"]["worktree_min_level"] = 2
    c["review"]["runtimes"] = ["claude", "codex"]
    c["runtimes"]["claude"]["models"] = {}
    return c


def done(summary="did it", commit_message="feat(core): add thing", **extra) -> dict:
    return {"status": "done", "summary": summary, "commit_message": commit_message, "decisions": [],
            "problems": [], "architecture_notes": [], "blocker": "", **extra}


Step = Callable[[ExecutionRequest], ExecutionResult]


class FakeRuntime(Runtime):
    """Scripted runtime: each execute() runs the next step."""

    def __init__(self, name: str, steps: list[Step]):
        self.name = name
        self.steps = list(steps)
        self.prompts: list[str] = []
        self.requests: list[ExecutionRequest] = []

    def installed(self) -> bool:
        return True

    async def execute(self, req, progress):
        self.prompts.append(req.prompt)
        self.requests.append(req)
        progress("working")
        return self.steps.pop(0)(req)


def writes(filename: str, content: str = "x\n", final: dict | None = None) -> Step:
    def step(req: ExecutionRequest) -> ExecutionResult:
        (req.cwd / filename).write_text(content)
        return ExecutionResult(ok=True, final=final or done(), input_tokens=100, output_tokens=10, duration=1)
    return step


def fails(kind, error="boom") -> Step:
    def step(req):
        return ExecutionResult(ok=False, error_kind=kind, error=error, duration=1)
    return step


class ScriptedUI:
    """Answers questions from a queue; records everything said."""

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.said: list[tuple[str, str]] = []
        self.questions: list[tuple[str, list[str]]] = []
        self.fields: dict = {}

    def say(self, text, kind="info"):
        self.said.append((kind, text))

    def progress(self, text):
        pass

    def status(self, **fields):
        self.fields.update(fields)

    async def choose(self, question, options):
        self.questions.append((question, options))
        answer = self.answers.pop(0)
        assert answer in options, f"{answer!r} not in {options} for {question!r}"
        return answer

    async def ask_text(self, question, placeholder="", default="", multiline=False, options=None):
        self.questions.append((question, list(options or [])))
        answer = self.answers.pop(0) if self.answers else None
        return default if answer in (None, "<default>") else answer

    def text(self) -> str:
        return "\n".join(t for _, t in self.said)
