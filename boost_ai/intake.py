"""User input that never needs a model.

Only explicit `!command` input is handled here; every other message goes to the agent,
which decides what the request needs. No rules about what the user may ask.
"""

from __future__ import annotations


def bang_command(text: str) -> str | None:
    """The command in `!command`, or None."""
    t = text.strip()
    return t[1:].strip() or None if t.startswith("!") else None


NO_COMMITS_WARNING = (
    "This repository has no commits yet, so risky work cannot be isolated in a worktree. "
    "Make a first commit (or ask the agent to):\n"
    "  !git add . && git commit -m \"chore: initial commit\"")
