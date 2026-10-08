"""Dangerous-command detection for commands BOOST_AI runs itself.

Commands executed *inside* agent runtimes are contained by each runtime's own
sandbox, by worktree isolation and by post-run checks (see orchestrator).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Danger:
    reason: str
    match: str


RULES: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive\s+--force|-r\s+-f|-f\s+-r)\b",
     "recursive force delete"),
    (r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b|\s\+\S)", "force push (rewrites remote history)"),
    (r"\bgit\s+push\b", "push to remote"),
    (r"\bgit\s+reset\s+--hard\b", "discard local changes (git reset --hard)"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "delete untracked files (git clean)"),
    (r"\bgit\s+(rebase|filter-branch|filter-repo)\b", "rewrite git history"),
    (r"\bgit\s+commit\b[^\n]*--amend\b", "rewrite last commit"),
    (r"\bgit\s+branch\s+(-D|--delete\s+--force)\b", "force-delete branch"),
    (r"\bgit\s+checkout\s+--\s+\.", "discard working-tree changes"),
    (r"\bdrop\s+(database|table|schema)\b", "drop database objects"),
    (r"\btruncate\s+table\b", "truncate table"),
    (r"\bdelete\s+from\s+\w+\s*;?\s*$", "unfiltered DELETE"),
    (r"\bdocker\s+(system|volume|image|container)\s+prune\b", "destructive docker cleanup"),
    (r"\bdocker\s+volume\s+rm\b", "delete docker volume"),
    (r"\bsudo\b|\bsu\s+-|\bpkexec\b", "privilege escalation"),
    (r"\bmkfs\b|\bdd\s+[^\n]*of=/dev/", "overwrite disk"),
    (r"\bchmod\s+-R\s+777\b|\bchown\s+-R\b", "recursive permission change"),
    (r"\b(kubectl\s+delete|terraform\s+destroy|helm\s+uninstall)\b", "destroy infrastructure"),
    (r"(curl|wget)[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipe remote script to shell"),
    (r">\s*/dev/sd[a-z]", "write to block device"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in RULES]


def check(command: str) -> list[Danger]:
    found: list[Danger] = []
    for rx, reason in _COMPILED:
        if m := rx.search(command):
            if not any(d.reason == reason for d in found):
                found.append(Danger(reason, m.group(0).strip()))
    # A force push also matches plain "push"; keep only the stronger warning.
    if any(d.reason.startswith("force push") for d in found):
        found = [d for d in found if d.reason != "push to remote"]
    return found


def is_force_push(command: str) -> bool:
    return any(d.reason.startswith("force push") for d in check(command))
