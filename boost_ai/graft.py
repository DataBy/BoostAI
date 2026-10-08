"""Project context from graft (https://www.npmjs.com/package/graft): a prebuilt graph of every
symbol, its file:line span and who calls what. Agents get its MCP tools and a short repo map
instead of exploring files blindly. Deterministic and $0 (no LLM), except the optional concept
pass (`deep`); any failure only means the agent works without it, never that the task fails.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

TOOLS = "graft_find_code, graft_find_all, graft_trace_calls, graft_file_api, graft_repo_map"


def available(cfg: dict) -> bool:
    return bool((cfg.get("context") or {}).get("graft", True)) and shutil.which("graft") is not None


def mcp_server(workdir: Path) -> dict:
    """MCP server spec (command + args) every runtime registers for this run."""
    return {"command": "graft", "args": ["mcp", str(workdir)]}


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["graft", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def ensure(workdir: Path) -> str | None:
    """Build the graph if this directory (e.g. a fresh worktree) has none. Graft refreshes an
    existing graph by itself on every query. Returns an error message, or None when ready."""
    if (workdir / "graft").is_dir():
        return None
    proc = _run(["build", ".", "--no-gitignore", "--no-ignore"], workdir, 600)
    if proc is None or proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()[-300:] if proc else "graft build did not finish"
    # The graph is a regenerable cache: keep it out of change detection and commits.
    ignored = subprocess.run(["git", "check-ignore", "-q", "graft/"], cwd=workdir).returncode == 0
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=workdir,
                            capture_output=True, text=True).stdout.strip()
    if not ignored and common:
        exclude = (workdir / common / "info" / "exclude").resolve()
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n/graft/\n")
    return None


def repo_map(workdir: Path, max_dirs: int = 16) -> str:
    """Token-budgeted orientation (directory clusters, hubs, hotspots), or "" on failure."""
    proc = _run(["map", "--max-dirs", str(max_dirs), "."], workdir, 60)
    if proc is None or proc.returncode != 0:
        return ""
    # Drop graft's own notes addressed to an interactive assistant (token-savings banners).
    return "\n".join(ln for ln in proc.stdout.splitlines() if not ln.startswith("[graft]")).strip()


def deep_env(cfg: dict) -> dict[str, str] | None:
    """Provider env for the concept pass (`context.graft_deep`), or None when off or unconfigured."""
    if (cfg.get("context") or {}).get("graft_deep") != "deepseek":
        return None
    from .runtimes.deepseek import openai_compatible_env
    return openai_compatible_env()


async def deep(workdir: Path, env: dict[str, str]) -> str | None:
    """Refresh the concept nodes (graft/*.md, per-symbol summaries) with the LLM pass. Incremental:
    unchanged files are replayed from graft's cache. Returns an error message, or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "graft", "build", ".", "--deep", "--allow-partial", "--no-gitignore", "--no-ignore", cwd=workdir,
            env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 1800)
    except (OSError, TimeoutError) as exc:
        return str(exc) or "timed out"
    return None if proc.returncode == 0 else out.decode(errors="replace").strip()[-300:]
