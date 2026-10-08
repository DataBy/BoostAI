"""Runtime registry. Add an adapter here and it becomes routable."""

from __future__ import annotations

from .antigravity import AntigravityRuntime
from .base import Runtime
from .claude import ClaudeRuntime
from .codex import CodexRuntime
from .deepseek import DeepSeekRuntime

LABELS = {"codex": "Codex", "claude": "Claude Code", "antigravity": "Antigravity", "deepseek": "DeepSeek"}


def build_runtimes(cfg: dict) -> dict[str, Runtime]:
    rcfg = cfg.get("runtimes", {})
    runtimes: dict[str, Runtime] = {
        "codex": CodexRuntime(rcfg.get("codex")),
        "claude": ClaudeRuntime(rcfg.get("claude")),
        "deepseek": DeepSeekRuntime(rcfg.get("deepseek")),  # needs DEEPSEEK_API_KEY + the claude CLI
    }
    if (rcfg.get("antigravity") or {}).get("enabled"):
        runtimes["antigravity"] = AntigravityRuntime(rcfg.get("antigravity"))
    return {name: rt for name, rt in runtimes.items() if rt.installed()}


def label(name: str | None) -> str:
    return LABELS.get(name or "", name or "—")
