"""Command-line entry point: `boost-ai` (TUI), init, doctor, status, stats."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
import yaml

from . import config, gitops, metrics
from .task import TaskStore

app = typer.Typer(add_completion=False, no_args_is_help=False,
                  help="BOOST_AI — a minimal local AI engineering harness.")


def project_root() -> Path:
    return gitops.repo_root(Path.cwd()) or Path.cwd()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Open the conversational TUI (default)."""
    if ctx.invoked_subcommand is None:
        root = project_root()
        if gitops.repo_root(root) is None:
            typer.echo("BOOST_AI needs a git repository. Run `git init` first.", err=True)
            raise typer.Exit(1)
        from .tui.app import BoostApp
        BoostApp(root).run()


# ── init ───────────────────────────────────────────────────────────────────
def detect_commands(root: Path) -> dict[str, str]:
    cmds: dict[str, str] = {}
    pyproject = (root / "pyproject.toml").read_text() if (root / "pyproject.toml").is_file() else ""
    if pyproject or (root / "pytest.ini").is_file() or (root / "setup.cfg").is_file():
        if "pytest" in pyproject or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
            cmds["test"] = "pytest -q"
        if "[tool.ruff" in pyproject or (root / "ruff.toml").is_file():
            cmds["lint"] = "ruff check ."
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts and "no test specified" not in scripts["test"]:
            cmds.setdefault("test", "npm test --silent")
        if "lint" in scripts:
            cmds.setdefault("lint", "npm run lint --silent")
        if "build" in scripts:
            cmds.setdefault("build", "npm run build --silent")
    if (root / "Cargo.toml").is_file():
        cmds.setdefault("test", "cargo test --quiet")
    if (root / "go.mod").is_file():
        cmds.setdefault("test", "go test ./...")
    makefile = root / "Makefile"
    if "test" not in cmds and makefile.is_file() and "\ntest:" in "\n" + makefile.read_text():
        cmds["test"] = "make test"
    return cmds


@app.command()
def init() -> None:
    """Prepare .boost-ai/ for the current repository (never overwrites without asking)."""
    root = project_root()
    if gitops.repo_root(root) is None:
        typer.echo("Not a git repository. Run `git init` first.", err=True)
        raise typer.Exit(1)
    base = root / ".boost-ai"
    for sub in ("skills", "specs", "state"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text("state/\nworktrees/\n")

    cfg_path = base / "config.yaml"
    if cfg_path.exists() and not typer.confirm(f"{cfg_path.relative_to(root)} exists. Overwrite?", default=False):
        typer.echo("Kept existing config.")
    else:
        cmds = detect_commands(root)
        body = yaml.safe_dump({"commands": cmds}, sort_keys=False) if cmds else "commands: {}\n"
        cfg_path.write_text(
            "# BOOST_AI project config. Overrides ~/.config/boost-ai/config.yaml and built-in defaults.\n"
            "# Verification commands run after every attempt (deterministic, no LLM).\n" + body +
            "\n# routing:\n#   policy:\n#     L2: [codex, claude]\n#   worktree_min_level: 2\n")
        found = ", ".join(f"{k}: {v}" for k, v in cmds.items()) or "none detected — edit commands:"
        typer.echo(f"Wrote {cfg_path.relative_to(root)} ({found})")
    if not (root / "AGENTS.md").exists():
        typer.echo("Tip: a short AGENTS.md (project rules, layout, conventions) improves every run.")
    typer.echo("Ready. Run `boost-ai`.")


# ── doctor ─────────────────────────────────────────────────────────────────
def _version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


@app.command()
def doctor() -> None:
    """Check dependencies. Optional features never block the harness."""
    ok, warn = typer.style("✓", fg="green"), typer.style("⚠", fg="yellow")
    bad = typer.style("✗", fg="red")
    root = project_root()

    def line(mark: str, name: str, detail: str = "", hint: str = "") -> None:
        typer.echo(f"{mark} {name:<14} {detail}")
        if hint:
            typer.echo(f"  {'':<14} {hint}")

    line(ok if sys.version_info >= (3, 11) else bad, "Python", sys.version.split()[0])
    gv = _version(["git", "--version"])
    line(ok if gv else bad, "Git", gv or "missing (required)")
    line(ok if gitops.repo_root(root) else warn, "Repository", str(root),
         "" if gitops.repo_root(root) else "not a git repository")
    cfg_exists = (root / ".boost-ai" / "config.yaml").exists()
    line(ok if cfg_exists else warn, "Project config", "present" if cfg_exists else "missing",
         "" if cfg_exists else "run `boost-ai init`")

    cfg = config.load(root)
    agy_on = bool(((cfg.get("runtimes") or {}).get("antigravity") or {}).get("enabled"))
    for name, cmd, note in (("Codex", ["codex", "--version"], ""),
                            ("Claude Code", ["claude", "--version"], ""),
                            ("Antigravity", ["agy", "--help"],
                             "" if agy_on else "disabled — set runtimes.antigravity.enabled: true")):
        v = _version(cmd) if shutil.which(cmd[0]) else None
        shown = (v if v and not v.lower().startswith("usage") else "installed")[:40] if v else "not found"
        line(ok if v and not note else warn, name, shown, note if v else "")
    if os.environ.get("DEEPSEEK_API_KEY"):
        from .runtimes.deepseek import account_balance
        balance = account_balance()
        line(ok if balance else warn, "DeepSeek",
             f"key set · balance ${balance:.2f}" if balance is not None else "key set · API not reachable")
    else:
        line(warn, "DeepSeek", "DEEPSEEK_API_KEY not set",
             "export it in ~/.bashrc (never in a repo file); L0/L1 then use Claude")

    if not (cfg.get("context") or {}).get("graft", True):
        line(warn, "graft", "disabled in config (context.graft)")
    elif not shutil.which("graft"):
        line(warn, "graft", "not found", "npm i -g graft — agents then explore without the code graph")
    else:
        built = (root / "graft").is_dir()
        line(ok if built else warn, "graft", _version(["graft", "--version"]) or "installed",
             "" if built else "no graph yet — built on the first task (or run `graft build`)")
    from . import rules
    line(ok, "Rules", f"{len(rules.discover(root))} rule file(s)", "")

    backend = (cfg.get("memory") or {}).get("backend", "local")
    line(ok, "Memory", f"{backend} (.boost-ai/state/memory/)",
         "Logseq not configured — BOOST_AI continues with local memory")
    from . import indicator as ind
    icfg = cfg.get("indicator") or {}
    if not icfg.get("enabled", True):
        line(warn, "Indicator", "disabled in config")
    elif ind.available(icfg.get("python") or ind.SYSTEM_PYTHON):
        line(ok, "Indicator", "AyatanaAppIndicator3")
    else:
        line(warn, "Indicator", "unavailable", "sudo apt install gir1.2-ayatanaappindicator3-0.1")
    line(ok if shutil.which("notify-send") else warn, "Notifications",
         "notify-send" if shutil.which("notify-send") else "notify-send missing (sudo apt install libnotify-bin)")
    player = next((p for p in ("pw-play", "paplay", "aplay") if shutil.which(p)), None)
    line(ok if player else warn, "Audio", player or "no player found; sounds disabled")


# ── status / stats ─────────────────────────────────────────────────────────
@app.command()
def status() -> None:
    """Show what BOOST_AI is doing in this project."""
    cur = TaskStore(project_root()).current()
    if not cur:
        typer.echo("Idle — no task has run here yet.")
        return
    for key in ("project", "task", "status", "model", "level", "tests", "branch", "usage", "updated_at"):
        typer.echo(f"{key.capitalize():<10} {cur.get(key, '—')}")


@app.command()
def stats() -> None:
    """Runtime efficiency by difficulty level (from local metrics)."""
    from .tui.app import format_stats
    typer.echo(format_stats(metrics.summarize(metrics.load())))


if __name__ == "__main__":
    app()
