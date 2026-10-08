"""Skills: BOOST_AI's own layer (harness/skills/, beside the package) plus project skills
(.boost-ai/skills/). Both are split by category: <category>/<skill>/SKILL.md. Other agents'
folders (~/.claude/skills, ~/.agents/skills, ~/.codex/skills) are never read or written.

Progressive disclosure: prompts carry only name, category, description and path; any runtime
reads a SKILL.md itself when the task needs it. A project skill overrides a harness skill with
the same name.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config

MAX_DESCRIPTION = 200


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    scope: str  # "harness" | "project"
    category: str = ""


def _read(skill_md: Path, scope: str) -> Skill | None:
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return None
    meta: dict = {}
    if text.startswith("---\n") and "\n---" in text[4:]:
        try:
            meta = yaml.safe_load(text[4:text.index("\n---", 4)]) or {}
        except yaml.YAMLError:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("name") or skill_md.parent.name)
    description = " ".join(str(meta.get("description") or "").split())
    if not description:  # fall back to the first prose line
        body = text.split("\n---", 2)[-1] if meta else text
        description = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 1] + "…"
    return Skill(name, description, skill_md, scope, skill_md.parent.parent.name)


def harness_dir() -> Path:
    return config.HARNESS_DIR / "skills"


def global_dirs() -> list[Path]:
    """The folders a runtime may read skills from (Claude gets them through --add-dir)."""
    return [d for d in [harness_dir()] if d.is_dir()]


def discover(project_root: Path) -> list[Skill]:
    found: dict[str, Skill] = {}
    for scope, base in (("harness", harness_dir()), ("project", project_root / ".boost-ai" / "skills")):
        for skill_md in sorted(base.glob("*/*/SKILL.md")) if base.is_dir() else []:
            if skill := _read(skill_md, scope):
                found[skill.name] = skill
    return sorted(found.values(), key=lambda s: (s.category, s.name))


def index(skills: list[Skill]) -> str:
    """Compact listing for prompts."""
    return "\n".join(f"- {s.name} [{s.category}]: {s.description} ({s.path})" for s in skills)


# Installing: a shallow git clone of the package, then its skill folders are copied into
# harness/skills/<category>/. Deterministic: no model, no quota, no other agent's folder touched.
RECOMMENDER = "claude-automation-recommender"   # from Claude Code Setup (anthropics/claude-plugins-official)
RECOMMENDER_SOURCE = "anthropics/claude-plugins-official"


def _command(*args: str) -> str:
    import shlex
    return " ".join(shlex.quote(a) for a in [sys.executable, "-m", "boost_ai.skills", *args])


def add_command(source: str, category: str | None, names: list[str]) -> str:
    """`/skill add <source> [<category> <name…|all>]`. Without a category it only lists the package."""
    return _command("add", source, *([category, *names] if category else []))


def remove_command(names: list[str]) -> str:
    return _command("remove", *names)


def install(source: str, category: str | None, names: list[str]) -> str:
    url = source if "://" in source or Path(source).exists() else f"https://github.com/{source}.git"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "-q", "--depth", "1", url, tmp], check=True)
        offered = {s.name: s.path.parent for md in sorted(Path(tmp).rglob("SKILL.md"))
                   if ".git" not in md.parts and (s := _read(md, "harness"))}
        if not category:
            return "Skills in " + source + ":\n" + "\n".join(f"- {n}" for n in sorted(offered))
        if not category.replace("-", "").isalnum():
            raise SystemExit(f"Invalid category: {category}")
        picked = sorted(offered) if names == ["all"] else names
        if missing := [n for n in picked if n not in offered]:
            raise SystemExit(f"Not in {source}: {', '.join(missing)}")
        target = harness_dir() / category
        for name in picked:
            dest = target / offered[name].name
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(offered[name], dest, ignore=shutil.ignore_patterns(".git"))
    return f"Installed into {target}: {', '.join(picked)}"


def uninstall(names: list[str]) -> str:
    gone = []
    for skill_md in sorted(harness_dir().glob("*/*/SKILL.md")):
        if (skill := _read(skill_md, "harness")) and skill.name in names:
            shutil.rmtree(skill.path.parent)
            gone.append(f"{skill.category}/{skill.name}")
    return "Removed: " + (", ".join(gone) or "nothing (no such harness skill)")


def recommend_request(found: list[Skill]) -> str:
    """The message `/skills recommend` sends to the agent (a read-only analysis)."""
    have = ", ".join(s.name for s in found) or "none"
    return (f"Use the {RECOMMENDER} skill to analyze this project (read-only, change no files) and recommend "
            f"which agent skills it needs. Installed skills: {have}. For each recommendation give the "
            f"command to install it with BOOST_AI: /skill add <owner/repo> <category> <skill-name>.")


if __name__ == "__main__":  # what /skill add and /skill remove run
    cmd, *rest = sys.argv[1:]
    if cmd == "add":
        print(install(rest[0], rest[1] if len(rest) > 1 else None, rest[2:]))
    else:
        print(uninstall(rest))
