"""Rules (always sent, in full), graft wiring, attachment-aware triage and skill installs."""

from __future__ import annotations

import json
from pathlib import Path

from boost_ai import graft, rules, skills
from boost_ai.context import build_prompt, build_review_prompt
from boost_ai.runtimes.base import ExecutionRequest
from boost_ai.runtimes.claude import ClaudeRuntime
from boost_ai.runtimes.codex import CodexRuntime
from boost_ai.task import Task
from boost_ai.triage import triage


def test_rules_are_added_listed_and_always_in_full(repo, cfg, tmp_path):
    path = rules.add(repo, "Commits follow   Conventional Commits", "git")
    assert path == repo / ".boost-ai" / "rules" / "git" / "project.md"
    layer = tmp_path / "harness" / "rules" / "style"
    layer.mkdir(parents=True)
    (layer / "ui.md").write_text("- Spanish UI strings\n" + "x" * 6000)
    (layer.parent / "flat.md").write_text("- not in a category: ignored\n")
    assert [s for s, _ in rules.discover(repo)] == ["harness", "project"]
    prompt = build_prompt(Task(id="T-1", text="hi"), repo, cfg)
    assert "non-negotiable" in prompt and "- Commits follow Conventional Commits" in prompt
    assert "### git/project (project)" in prompt and "not in a category" not in prompt
    assert "x" * 6000 in prompt                                    # never truncated, unlike AGENTS.md
    review = build_review_prompt(Task(id="T-1", text="hi"), repo, cfg, "Codex", {}, "ok")
    assert "Conventional Commits" in review and "violations of the project" in review


def test_graft_map_and_tools_in_prompt(repo, cfg, monkeypatch):
    monkeypatch.setattr(graft, "available", lambda cfg: True)
    monkeypatch.setattr(graft, "repo_map", lambda workdir: "repo map — 3 files")
    prompt = build_prompt(Task(id="T-1", text="hi"), repo, cfg)
    assert "## Code context (graft)" in prompt and "graft_find_code" in prompt and "repo map — 3 files" in prompt


def test_runtimes_get_only_the_graft_mcp_server(tmp_path):
    server = graft.mcp_server(tmp_path)
    req = ExecutionRequest(prompt="p", cwd=tmp_path, log_path=tmp_path / "l", mcp_servers={"graft": server})
    argv = ClaudeRuntime().argv(req)
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {"graft": server}}
    assert "--strict-mcp-config" in argv and "mcp__graft" in argv[argv.index("--allowedTools") + 1]
    cargv = CodexRuntime().argv(req, tmp_path / "s", tmp_path / "f")
    assert 'mcp_servers.graft.command="graft"' in cargv
    assert f'mcp_servers.graft.args=["mcp", "{tmp_path}"]' in cargv
    assert 'mcp_servers.graft.default_tools_approval_mode="approve"' in cargv  # exec never prompts


def test_graft_ensure_skips_existing_graph(tmp_path, monkeypatch):
    (tmp_path / "graft").mkdir()
    monkeypatch.setattr(graft, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build")))
    assert graft.ensure(tmp_path) is None


def test_long_text_is_a_floor_even_for_the_llm():
    tri = triage("hazlo\n" + "detalle del spec " * 100)
    assert tri.level >= 2 and tri.floor >= 2


def test_skill_install_commands():
    assert skills.add_command("owner/repo", None, []).endswith("-m boost_ai.skills add owner/repo")
    assert skills.add_command("owner/repo", "qa", ["a", "b"]).endswith("add owner/repo qa a b")
    assert skills.remove_command(["a"]).endswith("-m boost_ai.skills remove a")
    assert "npx" not in skills.add_command("owner/repo", "qa", ["all"])   # never another agent's folder
    assert skills.RECOMMENDER in skills.recommend_request([])


def test_graft_map_drops_assistant_banners(tmp_path, monkeypatch):
    class P:
        returncode, stdout = 0, "[graft] tokens saved ≈ 9\nrepo map — 1 files\n"
    monkeypatch.setattr(graft, "_run", lambda *a, **k: P())
    assert graft.repo_map(Path(tmp_path)) == "repo map — 1 files"


def test_graft_deep_uses_deepseek_only_when_configured(monkeypatch):
    assert graft.deep_env({"context": {"graft_deep": "deepseek"}}) is None          # no key in tests
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = graft.deep_env({"context": {"graft_deep": "deepseek"}})
    assert env and env["GRAFT_PROVIDER"] == "openai" and env["GRAFT_API_KEY"] == "sk-test"
    assert graft.deep_env({"context": {"graft_deep": None}}) is None


def test_suite_is_discovered_pinned_and_licensed(repo, cfg, monkeypatch):
    from boost_ai import config

    from .conftest import HARNESS_DIR
    monkeypatch.setattr(config, "HARNESS_DIR", HARNESS_DIR)
    SUITE_DIR = HARNESS_DIR / "skills"
    found = {s.name: s for s in skills.discover(repo) if s.scope == "harness"}
    expected = {"codebase-design", "improve-codebase-architecture", "domain-modeling", "frontend-design",
                "vercel-react-best-practices", "vercel-composition-patterns", "web-design-guidelines",
                "differential-review", "sharp-edges", "supply-chain-risk-auditor", "tdd",
                "verification-before-completion", "property-based-testing", "webapp-testing", "grilling",
                "systematic-debugging", "code-review", "receiving-code-review",
                "claude-automation-recommender", "claude-md-improver", "claude-api", "mcp-builder", "skill-creator"}
    assert expected <= set(found) and all(s.description for s in found.values())
    assert found["tdd"].category == "qa" and found["grilling"].category == "interviewing"
    manifest = (SUITE_DIR / "SUITE.md").read_text()
    assert all(f"| {name} " in manifest for name in expected)           # every skill has origin + license
    assert "fetch" not in (SUITE_DIR / "frontend" / "web-design-guidelines" / "SKILL.md").read_text().lower().split(
        "## usage")[1]                                                    # pinned guidelines, no network
    prompt = build_prompt(Task(id="T-1", text="hi"), repo, cfg)
    assert "the harness owns planning, commits" in prompt and "grilling" in prompt
    assert skills.RECOMMENDER in found                                   # /skills recommend needs no install
