"""DeepSeek: as an agent (Claude Code's CLI with DeepSeek as the engine) and as a plain API.

Agent: DeepSeek exposes an Anthropic-compatible endpoint, so BOOST_AI runs the Claude Code
CLI with ANTHROPIC_BASE_URL pointed at it. Tools, the safety hook, git, sessions and
structured output all work as with Claude; only that process's environment changes, so
your own Claude login is never touched. Billing is your DeepSeek API balance.

API: a tiny stdlib client for one-shot text calls (triage), plus the account balance,
which DeepSeek reports exactly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import ExecutionRequest, ExecutionResult, ProgressFn, Usage, UsageReport
from .claude import ClaudeRuntime

API = "https://api.deepseek.com"
ANTHROPIC_ENDPOINT = f"{API}/anthropic"
DEFAULT_MODEL = "deepseek-flash"


def api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or None


class DeepSeekRuntime(ClaudeRuntime):
    name = "deepseek"
    label = "DeepSeek"
    vision = False  # text-only models: images/PDFs move the conversation to a multimodal runtime

    def installed(self) -> bool:
        return super().installed() and api_key() is not None

    def _engine_env(self, req: ExecutionRequest, model: str) -> dict[str, str]:
        env = dict(req.env if req.env is not None else os.environ)
        key = api_key() or ""
        for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop(var, None)  # make sure nothing routes this run to your Anthropic account
        env.update({
            "ANTHROPIC_BASE_URL": ANTHROPIC_ENDPOINT,
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_MODEL": model,
            # Claude Code uses "small/fast" models for background work: keep those on DeepSeek too.
            "ANTHROPIC_SMALL_FAST_MODEL": DEFAULT_MODEL,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": DEFAULT_MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        return env

    async def execute(self, req: ExecutionRequest, progress: ProgressFn) -> ExecutionResult:
        model = req.model or self.cfg.get("model") or DEFAULT_MODEL
        req.model = model
        req.env = self._engine_env(req, model)
        result = await super().execute(req, progress)
        result.cost = None    # Claude Code prices tokens at Anthropic rates; wrong for DeepSeek
        result.usage = None   # no Anthropic rate-limit windows here; the balance is reported instead
        if (balance := account_balance()) is not None:
            result.usage = UsageReport(state=_balance_state(balance), percent=None, window=f"${balance:.2f}")
        return result


def openai_compatible_env() -> dict[str, str] | None:
    """Env for tools that speak the OpenAI wire format (graft's --deep pass), or None without a key."""
    key = api_key()
    if not key:
        return None
    return {"GRAFT_PROVIDER": "openai", "GRAFT_BASE_URL": API, "GRAFT_MODEL": DEFAULT_MODEL, "GRAFT_API_KEY": key}


def _balance_state(balance: float) -> Usage:
    if balance <= 0:
        return Usage.EXHAUSTED
    if balance < 0.5:
        return Usage.CRITICAL
    if balance < 2:
        return Usage.CONSERVE
    return Usage.AVAILABLE


def _request(path: str, payload: dict | None = None, timeout: float = 15) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def account_balance() -> float | None:
    """Total USD balance as reported by DeepSeek (exact), or None if unavailable."""
    try:
        infos = _request("/user/balance").get("balance_infos") or []
    except (OSError, urllib.error.URLError, ValueError, RuntimeError):
        return None
    usd = [float(b.get("total_balance", 0)) for b in infos if b.get("currency") == "USD"]
    return usd[0] if usd else None


def chat_json(system: str, user: str, model: str = DEFAULT_MODEL, timeout: float = 15, attempts: int = 2) -> dict:
    """One-shot JSON completion (no tools). Raises on any failure; callers fall back.
    DeepSeek's JSON mode occasionally returns empty content, so an empty answer is retried once."""
    last: Exception = ValueError("no attempts")
    for _ in range(attempts):
        out = _request("/chat/completions", {
            "model": model, "temperature": 0, "max_tokens": 800,  # room for its reasoning tokens
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, timeout=timeout)
        content = (out["choices"][0]["message"].get("content") or "").strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last = exc
    raise last
