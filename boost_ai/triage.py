"""Deterministic task triage: difficulty level (L0-L4) and risk flags.

V1 uses keyword rules only (no LLM call). An optional cheap LLM triage can be
layered on later; these rules would still apply as floors so security-sensitive
work is never routed to a weak runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LONG_TEXT = 1200
LEVEL_NAMES = {0: "TRIVIAL", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "CRITICAL"}

# (risk flag, minimum level, pattern). English + Spanish.
RISK_RULES: list[tuple[str, int, str]] = [
    ("data", 4, r"\bmigrat\w*|migraci[oó]n|drop (table|database|column)|delete data|borrar datos|data loss"),
    ("security", 4, r"\bencrypt\w*|cifrad\w*|\bcrypto\w*|payment|pago[s]?\b|billing|factura"),
    ("production", 4, r"\bproduction\b|producci[oó]n|\bprod\b|deploy\w*|despliegue"),
    ("security", 3, r"\bauth\w*|autentic\w*|autoriz\w*|login|password|contraseñ\w*|token|oauth|jwt|"
                    r"permission|permiso|secur\w*|segurid\w*|session|sesi[oó]n|csrf|xss|sql injection"),
    ("architecture", 3, r"architect\w*|arquitect\w*|redesign|rediseñ\w*|refactor\w* (the |el |la )?"
                        r"(whole|entire|todo|toda|core|n[uú]cleo)|concurren\w*|race condition"),
    ("schema", 3, r"\bschema\b|esquema|database model|modelo de datos"),
]

TRIVIAL = r"\btypo\w*|errata|spelling|ortograf\w*|whitespace|indent\w*|bump (the )?version|" \
          r"rename (a |the )?variable|renombrar (una |la )?variable"
LOW = r"\bdocstring|comment\w*|comentario|readme|log message|mensaje de log|add (a |one )?test|" \
      r"añadir (un )?test|agregar (un )?test|rename|renombr\w*|format\w*|lint\w*"


@dataclass
class Triage:
    level: int
    risks: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    floor: int = 0          # minimum level forced by deterministic rules (risk, length)
    source: str = "rules"   # "rules" | "llm"

    @property
    def label(self) -> str:
        return f"L{self.level} {LEVEL_NAMES[self.level]}"


def triage(text: str) -> Triage:
    t = text.lower()
    risks: list[str] = []
    reasons: list[str] = []
    floor = 0
    for flag, level, pattern in RISK_RULES:
        if m := re.search(pattern, t):
            if flag not in risks:
                risks.append(flag)
            if level > floor:
                floor = level
            reasons.append(f"'{m.group(0)}' → {flag} (≥L{level})")

    if re.search(TRIVIAL, t):
        base = 0
    elif re.search(LOW, t):
        base = 1
    else:
        base = 2
    # Long pasted requirements (or an attached spec) are rarely trivial, whatever a model says.
    if len(text) > LONG_TEXT:
        floor = max(floor, 2)
        if base < 2:
            base = 2
            reasons.append("long description (≥L2)")

    return Triage(level=max(base, floor), risks=risks, reasons=reasons, floor=floor)


RISK_FLAGS = ("security", "data", "production", "architecture", "schema")
LLM_SYSTEM = (
    "Classify a request sent to a software-engineering agent, for routing. Levels: "
    "0 trivial (greeting, question, typo, rename, a single git command); "
    "1 low (small isolated change, docs, a simple test); "
    "2 medium (ordinary feature or bug fix in one area); "
    "3 high (auth, security, permissions, concurrency, cross-module refactor, architecture); "
    "4 critical (data migrations, payments, production/deploy, possible data loss). "
    'Reply only with JSON: {"level": 0-4, "risks": [subset of "security","data","production",'
    '"architecture","schema"], "reason": "<10 words"}')


def llm_triage(text: str, cfg: dict) -> Triage | None:
    """Optional cheap model classification. Never below the deterministic risk floor;
    None if disabled or on any failure (the rules are then used alone)."""
    if (cfg.get("triage") or {}).get("llm") != "deepseek":
        return None
    from .runtimes.deepseek import api_key, chat_json
    if not api_key():
        return None
    rules = triage(text)
    try:
        out = chat_json(LLM_SYSTEM, text[:4000], timeout=8)  # the rules above saw all of it
        level = int(out["level"])
        if not 0 <= level <= 4:
            return None
        risks = [r for r in out.get("risks") or [] if r in RISK_FLAGS]
    except Exception:  # network, quota, malformed JSON: fall back to rules
        return None
    merged = sorted(set(rules.risks) | set(risks))
    reason = str(out.get("reason") or "")[:80]
    return Triage(level=max(level, rules.floor), risks=merged, reasons=rules.reasons + [f"llm: {reason}"],
                  floor=rules.floor, source="llm")


async def assess(text: str, cfg: dict) -> Triage:
    """LLM-refined triage when configured, else rules. Runs off the event loop."""
    import asyncio
    refined = await asyncio.to_thread(llm_triage, text, cfg)
    return refined or triage(text)
