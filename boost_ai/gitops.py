"""Git operations. Commits only with the user's approval; there is deliberately no push."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


# Harness files (.boost-ai/) are never part of a task's change set.
EXCLUDE = ":(exclude).boost-ai"


def git(cwd: Path, *args: str, input: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def repo_root(path: Path) -> Path | None:
    try:
        return Path(git(path, "rev-parse", "--show-toplevel").strip())
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


def current_branch(cwd: Path) -> str:
    out = git(cwd, "branch", "--show-current", check=False).strip()
    return out or "(detached)"


def branches(cwd: Path) -> list[tuple[str, bool]]:
    """(name, is_remote_only): local branches, most recently used first, then remote branches
    you don't have locally (`git switch <name>` creates the tracking branch)."""
    fmt = "--format=%(refname:short)"
    local = git(cwd, "for-each-ref", "--sort=-committerdate", fmt, "refs/heads", check=False).split()
    current = current_branch(cwd)
    if current != "(detached)" and current not in local:  # unborn branch of a repo with no commits
        local.insert(0, current)
    remote = []
    for ref in git(cwd, "for-each-ref", "--sort=-committerdate", fmt, "refs/remotes", check=False).split():
        name = ref.split("/", 1)[1] if "/" in ref else ref
        if name != "HEAD" and ref.split("/", 1)[0] != ref and name not in local and name not in remote:
            remote.append(name)
    return [(b, False) for b in local] + [(b, True) for b in remote]


def head(cwd: Path) -> str | None:
    out = git(cwd, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    return out or None


def is_dirty(cwd: Path) -> bool:
    return bool(git(cwd, "status", "--porcelain", "--", ".", EXCLUDE).strip())


def create_worktree(root: Path, task_id: str) -> Path:
    """Isolated checkout of HEAD with no branch of its own (detached): the user only ever sees
    their own branches. Approved commits are brought onto the user's branch with `land`."""
    path = root / ".boost-ai" / "worktrees" / task_id
    git(root, "worktree", "add", "--detach", str(path), "HEAD")
    return path


def land(root: Path, sha: str, branch: str) -> str:
    """Put a commit made in a worktree on the user's `branch`: a fast-forward when the branch has
    not moved (the very same commit), else a cherry-pick. A branch that is not checked out only
    accepts a fast-forward. Returns the commit now on the branch; on GitError nothing changed."""
    if current_branch(root) == branch:
        try:
            git(root, "merge", "--ff-only", "-q", sha)
            return sha
        except GitError:
            pass  # the branch moved on: replay the commit on top of it
        try:
            git(root, "cherry-pick", "--allow-empty", sha)
        except GitError:
            git(root, "cherry-pick", "--abort", check=False)
            raise
        return git(root, "rev-parse", "--short", "HEAD").strip()
    git(root, "fetch", "-q", ".", f"{sha}:refs/heads/{branch}")  # git refuses anything but a fast-forward
    return sha


def remove_worktree(root: Path, path: Path) -> None:
    """Remove a worktree whose work is committed. Never forced: refuses if dirty."""
    git(root, "worktree", "remove", str(path))


def changed_files(cwd: Path) -> list[str]:
    files = []
    for line in git(cwd, "status", "--porcelain", "-uall", "--", ".", EXCLUDE).splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


def diff_stat(cwd: Path) -> str:
    """Stat for tracked changes plus a list of untracked files."""
    stat = git(cwd, "diff", "HEAD", "--stat", "--", ".", EXCLUDE, check=False).rstrip()
    untracked = _untracked(cwd)
    if untracked:
        stat += ("\n" if stat else "") + "\n".join(f" {p} (new)" for p in untracked)
    return stat


def diff(cwd: Path) -> str:
    out = git(cwd, "diff", "HEAD", "--", ".", EXCLUDE, check=False)
    for p in _untracked(cwd):
        out += git(cwd, "diff", "--no-index", "/dev/null", p, check=False)
    return out


def _untracked(cwd: Path) -> list[str]:
    return git(cwd, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", EXCLUDE).split("\0")[:-1]


def snapshot(cwd: Path) -> dict[str, str]:
    """Fingerprint of every uncommitted path (content hash or 'deleted')."""
    out = {}
    for path in changed_files(cwd):
        target = cwd / path
        try:
            out[path] = hashlib.sha1(target.read_bytes()).hexdigest() if target.is_file() else "dir"
        except OSError:
            out[path] = "unreadable"
        if not target.exists():
            out[path] = "deleted"
    return out


def changed_since(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths whose uncommitted state changed between two snapshots (new, edited, deleted, reverted)."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def add_paths(cwd: Path, paths: list[str]) -> None:
    """Stage exactly these paths (including deletions)."""
    if paths:
        git(cwd, "add", "-A", "--", *paths)


def add_all(cwd: Path) -> None:
    """`git add .` — minus the harness's own .boost-ai/ files."""
    git(cwd, "add", "--", ".", EXCLUDE)


_ATTRIBUTION = re.compile(
    r"^\s*(co-authored-by:.*|.*generated (by|with) (claude|codex|chatgpt|gpt|ai|gemini|deepseek|antigravity).*|"
    r".*ai[- ]assisted.*|🤖.*)$",
    re.IGNORECASE,
)


def sanitize_message(message: str) -> str:
    """Drop AI attribution lines and trailing whitespace; keep the user's structure."""
    lines = [ln.rstrip() for ln in message.strip().splitlines() if not _ATTRIBUTION.match(ln)]
    return "\n".join(lines).strip()


def commit(cwd: Path, message: str) -> str:
    """Commit staged changes with the user's configured identity. Returns short sha."""
    message = sanitize_message(message)
    if not message:
        raise GitError("Commit message is empty.")
    git(cwd, "commit", "-F", "-", input=message + "\n")
    return git(cwd, "rev-parse", "--short", "HEAD").strip()
