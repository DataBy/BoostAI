"""Rules: non-negotiable conventions, the counterpart of skills.

Skills are optional know-how loaded on demand; rules are mandatory and always sent in full
(planning, implementation and review), never truncated. Each rule set is a Markdown file split
by category: <category>/<name>.md, in BOOST_AI's own layer (harness/rules/, every project) and
in .boost-ai/rules/ (versioned with the repo). A project file overrides a harness file with the
same category and name.
"""

from __future__ import annotations

from pathlib import Path

from . import config

PROJECT_FILE = "project.md"
DEFAULT_CATEGORY = "general"


def project_dir(root: Path) -> Path:
    return root / ".boost-ai" / "rules"


def discover(root: Path) -> list[tuple[str, Path]]:
    """(scope, path) of every non-empty rule file, harness first."""
    found: dict[str, tuple[str, Path]] = {}
    for scope, base in (("harness", config.HARNESS_DIR / "rules"), ("project", project_dir(root))):
        for path in sorted(base.glob("*/*.md")) if base.is_dir() else []:
            try:
                if path.read_text(errors="replace").strip():
                    found[f"{path.parent.name}/{path.name}"] = (scope, path)
            except OSError:
                continue
    return list(found.values())


def text(root: Path) -> str:
    """All rules, ready for a prompt, or "" when there are none."""
    parts = []
    for scope, path in discover(root):
        try:
            body = path.read_text(errors="replace").strip()
        except OSError:
            continue
        parts.append(f"### {path.parent.name}/{path.stem} ({scope})\n{body}")
    return "\n\n".join(parts)


def add(root: Path, rule: str, category: str = DEFAULT_CATEGORY) -> Path:
    """Append one rule as a bullet to the project's <category>/project.md."""
    base = project_dir(root) / category
    path = base / PROJECT_FILE
    base.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.is_file() else "# Non-negotiable rules\n\n"
    if not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + f"- {' '.join(rule.split())}\n")
    return path
