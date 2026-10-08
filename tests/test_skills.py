from __future__ import annotations

import subprocess

from boost_ai import skills
from boost_ai.context import build_prompt
from boost_ai.task import Task


def make(base, name, text, category="general"):
    (base / category / name).mkdir(parents=True)
    (base / category / name / "SKILL.md").write_text(text)


def test_discover_harness_and_project_with_override(tmp_path, repo):
    g = tmp_path / "harness" / "skills"
    p = repo / ".boost-ai" / "skills"
    make(g, "docker", "---\nname: docker\ndescription: Debug containers.\n---\n# Docker\n...", "ops")
    make(g, "testing", "---\nname: testing\ndescription: Global testing.\n---\n", "qa")
    make(p, "testing", "---\nname: testing\ndescription: Run this project's suite.\n---\nBody " + "x" * 5000, "qa")
    make(p, "plain", "# Title\n\nFirst prose line used as description.\n")
    found = {s.name: s for s in skills.discover(repo)}
    assert set(found) == {"docker", "testing", "plain"}
    assert found["testing"].scope == "project" and found["testing"].description == "Run this project's suite."
    assert found["docker"].scope == "harness" and found["docker"].category == "ops"
    assert found["plain"].description == "First prose line used as description."


def test_prompt_lists_metadata_only(tmp_path, repo, cfg):
    make(repo / ".boost-ai" / "skills", "fastapi",
         "---\nname: fastapi\ndescription: API conventions.\n---\nSECRET_BODY_DETAIL " * 50, "backend")
    prompt = build_prompt(Task(id="T-1", text="add endpoint"), repo, cfg)
    assert "- fastapi [backend]: API conventions. (" in prompt and "SKILL.md" in prompt
    assert "SECRET_BODY_DETAIL" not in prompt


def test_no_skills_no_section(repo, cfg):
    assert "## Skills" not in build_prompt(Task(id="T-1", text="x"), repo, cfg)


def test_broken_frontmatter_is_tolerated(repo):
    make(repo / ".boost-ai" / "skills", "weird", "---\n: : :\n---\nStill usable.\n")
    assert [s.name for s in skills.discover(repo)] == ["weird"]


def test_other_agents_folders_and_uncategorized_skills_are_ignored(tmp_path, repo):
    home = tmp_path / "home"
    make(home / ".claude", "skills", "---\nname: remotion\ndescription: Claude copy.\n---\n")
    make(home / ".agents", "skills", "---\nname: docker\ndescription: Containers.\n---\n")
    (tmp_path / "harness" / "skills" / "flat").mkdir(parents=True)
    (tmp_path / "harness" / "skills" / "flat" / "SKILL.md").write_text("---\nname: flat\n---\n")
    assert skills.discover(repo) == [] and skills.global_dirs() == [tmp_path / "harness" / "skills"]


def test_install_copies_into_the_category_and_remove_deletes(tmp_path, repo):
    pkg = tmp_path / "pkg"
    make(pkg / "skills", "alpha", "---\nname: alpha\ndescription: A.\n---\n", "x")
    make(pkg / "skills", "beta", "---\nname: beta\ndescription: B.\n---\n", "y")
    subprocess.run("git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm init",
                   shell=True, cwd=pkg, check=True)
    assert "alpha" in skills.install(str(pkg), None, [])
    skills.install(str(pkg), "frontend", ["alpha"])
    found = {s.name: s for s in skills.discover(repo)}
    assert set(found) == {"alpha"} and found["alpha"].category == "frontend"
    assert "frontend/alpha" in skills.uninstall(["alpha"]) and skills.discover(repo) == []
