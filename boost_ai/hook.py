"""Safety hook for agent runtimes + the file-based approval protocol behind it.

Claude Code and Antigravity run `python -m boost_ai.hook` before every shell
command (PreToolUse).
Safe commands pass immediately. Dangerous ones are either blocked outright or,
when BOOST_AI_APPROVAL_DIR is set (a harness is running), turned into an approval
request that the harness shows to the user:

    <dir>/<id>.request.json   written by the hook   {id, command, reasons}
    <dir>/<id>.response.json  written by the harness {approved: bool}

Exit 0 allows the command; exit 2 blocks it and the stderr text goes back to the agent.
Both files stay in place as an audit trail.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import safety

WAIT_SECONDS = 540  # below the hook timeout registered with the runtime


@dataclass
class ApprovalRequest:
    id: str
    command: str
    reasons: list[str]


def request_approval(directory: Path, command: str, reasons: list[str], wait: float = WAIT_SECONDS,
                     poll: float = 0.25) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    tmp = directory / f"{rid}.request.tmp"
    tmp.write_text(json.dumps({"id": rid, "command": command, "reasons": reasons}))
    tmp.replace(directory / f"{rid}.request.json")
    response = directory / f"{rid}.response.json"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if response.is_file():
            try:
                return bool(json.loads(response.read_text()).get("approved"))
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(poll)
    return False


def pending(directory: Path) -> list[ApprovalRequest]:
    out = []
    for path in sorted(directory.glob("*.request.json")) if directory.is_dir() else []:
        rid = path.name.removesuffix(".request.json")
        if (directory / f"{rid}.response.json").exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(ApprovalRequest(rid, data.get("command", ""), list(data.get("reasons", []))))
    return out


def respond(directory: Path, rid: str, approved: bool) -> None:
    tmp = directory / f"{rid}.response.tmp"
    tmp.write_text(json.dumps({"approved": approved}))
    tmp.replace(directory / f"{rid}.response.json")


def decide(command: str, env) -> tuple[bool, str]:
    """(allowed, message for the agent). Safe commands pass; dangerous ones need the user."""
    dangers = safety.check(command)
    if not dangers:
        return True, ""
    reasons = [d.reason for d in dangers]
    approval_dir = env.get("BOOST_AI_APPROVAL_DIR")
    if approval_dir and request_approval(Path(approval_dir), command, reasons):
        return True, "Approved by the user."
    verdict = "The user denied it" if approval_dir else "It needs human approval"
    return False, (f"BOOST_AI blocked this command ({'; '.join(reasons)}). {verdict}. Do not retry it; "
                   "use a safer alternative, or finish with status \"blocked\" and explain what you need.")


# Antigravity can only run commands headlessly with every permission skipped, so for it
# this hook is the gate for *all* tools: an allow-list, failing closed.
AGY_SHELL = {"run_command": "CommandLine", "send_command_input": "Input"}
AGY_READ = {"view_file", "list_dir", "find_by_name", "grep_search", "command_status", "finish",
            "wait", "wait_5_seconds", "manage_task", "list_permissions"}
AGY_WRITE = {"write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"}


def decide_agy(tool: str, args: dict, env) -> tuple[bool, str]:
    if tool in AGY_SHELL:
        return decide(str(args.get(AGY_SHELL[tool], "")), env)
    if tool in AGY_READ:
        return True, ""
    if tool in AGY_WRITE:
        if env.get("BOOST_AI_READ_ONLY") == "1":
            return False, "BOOST_AI: this is a read-only review; do not modify files."
        workspace = env.get("BOOST_AI_WORKSPACE")
        paths = [v for k, v in args.items() if isinstance(v, str) and k.lower().endswith(("file", "path"))]
        if not workspace or not paths:
            return False, "BOOST_AI could not verify where this edit writes, so it was blocked."
        root = Path(workspace).resolve()
        for raw in paths:
            target = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            if not target.is_relative_to(root):
                return False, f"BOOST_AI: writing outside the workspace is not allowed ({raw})."
        return True, ""
    return False, f"BOOST_AI: the tool `{tool}` is not available in this run. Use files and shell only."


def main(stdin=None, env=None, stdout=None) -> int:
    """Speaks both hook protocols:
    - Claude Code: {"tool_input": {"command"}} → exit 0 allow / exit 2 block (stderr to agent)
    - Antigravity: {"toolCall": {"name", "args"}} → stdout {"decision": "allow"|"deny"}
    """
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    env = os.environ if env is None else env
    try:
        data = json.load(stdin)
        if "toolCall" in data:
            call = data.get("toolCall") or {}
            allowed, message = decide_agy(str(call.get("name", "")), dict(call.get("args") or {}), env)
            stdout.write(json.dumps({"decision": "allow" if allowed else "deny", "reason": message}))
            return 0
        command = str((data.get("tool_input") or {}).get("command", ""))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        print("BOOST_AI could not inspect this command, so it was blocked.", file=sys.stderr)
        return 2  # fail closed

    allowed, message = decide(command, env)
    if not allowed:
        print(message, file=sys.stderr)
    return 0 if allowed else 2


if __name__ == "__main__":
    sys.exit(main())
