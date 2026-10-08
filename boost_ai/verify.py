"""Deterministic verification: run configured project commands, compress output.

Full raw output always stays on disk; only a compact summary is shown to the
user or fed back to a model.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import safety
from .redact import redact


def project_env(root: Path) -> dict[str, str]:
    """Environment for project commands: activates <root>/.venv if present.

    Worktrees do not contain the (git-ignored) venv, so commands like `pytest`
    must resolve to the main checkout's environment.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # don't leave __pycache__ in the change set
    venv = root / ".venv"
    if (venv / "bin").is_dir():
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env

MAX_SUMMARY_LINES = 40


@dataclass
class Check:
    name: str
    command: str
    ok: bool
    summary: str
    log_path: Path
    passed: int | None = None
    failed: int | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    configured: bool = True

    @property
    def ok(self) -> bool:
        return self.configured and all(c.ok for c in self.checks)

    @property
    def tests(self) -> str | None:
        """'passed / total' when a test runner reported counts."""
        p = sum(c.passed or 0 for c in self.checks)
        f = sum(c.failed or 0 for c in self.checks)
        return f"{p} / {p + f}" if (p or f) else None

    def text(self) -> str:
        if not self.configured:
            return ("No verification commands are configured (.boost-ai/config.yaml → commands).\n"
                    "The change is NOT automatically verified.")
        lines = []
        for c in self.checks:
            lines.append(f"{'✓' if c.ok else '✗'} {c.name}: {c.command}")
            if not c.ok or c.passed is not None:
                lines += ["  " + ln for ln in c.summary.splitlines()]
        return "\n".join(lines)

    def failures_for_model(self) -> str:
        return "\n\n".join(f"`{c.command}` failed:\n{c.summary}" for c in self.checks if not c.ok)


async def run(commands: dict[str, str], cwd: Path, log_dir: Path, timeout: int = 900,
              env: dict[str, str] | None = None) -> Report:
    if not commands:
        return Report(configured=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    for name, command in commands.items():
        if not command:
            continue
        log_path = log_dir / f"verify-{name}.log"
        if dangers := safety.check(command):
            report.checks.append(Check(name, command, False,
                                       "Refused: " + "; ".join(d.reason for d in dangers), log_path))
            continue
        code, output = await run_shell(command, cwd, timeout, env)
        log_path.write_text(redact(output))
        check = Check(name, command, code == 0, redact(compress(output, code)), log_path)
        check.passed, check.failed = parse_counts(output)
        report.checks.append(check)
    return report


async def run_shell(command: str, cwd: Path, timeout: int, env: dict[str, str] | None) -> tuple[int, str]:
    """Non-interactive shell command: stdin closed, stdout+stderr combined, killed on timeout."""
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"Timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace")


_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_DURATION = re.compile(r"\bin [\d.]+s\b")
_JEST_TOTAL = re.compile(r"Tests:\s+(?:(\d+) failed, )?(?:\d+ skipped, )?(\d+) passed")


def parse_counts(output: str) -> tuple[int | None, int | None]:
    # pytest final line, framed ("==== 2 failed, 19 passed in 0.3s ====") or -q ("12 passed in 0.01s")
    for line in reversed(output.splitlines()):
        if _DURATION.search(line) and (found := _COUNT.findall(line)):
            counts = {k.rstrip("s"): int(n) for n, k in found}
            return counts.get("passed", 0), counts.get("failed", 0) + counts.get("error", 0)
    if m := _JEST_TOTAL.search(output):
        return int(m.group(2)), int(m.group(1) or 0)
    return None, None


def compress(output: str, code: int) -> str:
    """Keep what a human or model needs to act on: totals and failure details."""
    lines = output.rstrip().splitlines()
    if code == 0:
        return lines[-1] if lines else "ok"

    # pytest: the short summary section is the highest-signal part.
    if "short test summary info" in output:
        start = next(i for i, ln in enumerate(lines) if "short test summary info" in ln)
        failures = _pytest_failure_blocks(lines)
        return _cap(failures + lines[start:])

    # Generic: lines that look like errors, plus the tail.
    errorish = [ln for ln in lines if re.search(r"error|fail|assert|exception|traceback|✗|✕", ln, re.I)]
    tail = lines[-15:]
    picked = list(dict.fromkeys(errorish[:20] + tail))
    return _cap(picked + [f"(exit status {code})"])


def _pytest_failure_blocks(lines: list[str]) -> list[str]:
    """For each failing test, keep the header and the 'E   ' assertion lines."""
    out: list[str] = []
    in_failures = False
    for ln in lines:
        if re.match(r"=+ FAILURES =+", ln):
            in_failures = True
            continue
        if in_failures and re.match(r"=+ ", ln):
            break
        if in_failures and (re.match(r"_+ .* _+$", ln) or ln.startswith("E ") or re.match(r"\S+\.py:\d+:", ln)):
            out.append(ln)
    return out[:25]


def _cap(lines: list[str]) -> str:
    if len(lines) > MAX_SUMMARY_LINES:
        lines = lines[:MAX_SUMMARY_LINES] + [f"… ({len(lines) - MAX_SUMMARY_LINES} more lines in log)"]
    return "\n".join(lines)
