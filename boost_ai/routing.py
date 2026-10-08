"""Deterministic runtime selection and escalation. Pure functions, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from .runtimes.base import Usage


@dataclass
class Route:
    runtime: str | None
    reason: str
    skipped: list[str] = field(default_factory=list)  # "claude: not installed", ...


def _blocked(name: str, available: set[str], usage: dict[str, Usage],
             failures: dict[str, int], max_failures: int) -> str | None:
    if name not in available:
        return "not available"
    if usage.get(name) is Usage.EXHAUSTED:
        return "usage exhausted"
    if failures.get(name, 0) >= max_failures:
        return f"failed {failures[name]}×"
    return None


def select(level: int, cfg: dict, available: set[str], usage: dict[str, Usage] | None = None,
           failures: dict[str, int] | None = None) -> Route:
    """First runtime in the level's policy that is available, not exhausted, not failed out."""
    routing = cfg["routing"]
    usage, failures = usage or {}, failures or {}
    max_failures = routing.get("attempts_per_runtime", 2)
    policy = routing["policy"].get(f"L{level}", [])
    skipped = []
    for name in policy:
        why = _blocked(name, available, usage, failures, max_failures)
        if why is None:
            note = f" (skipped: {', '.join(skipped)})" if skipped else ""
            return Route(name, f"L{level} policy → {name}{note}", skipped)
        skipped.append(f"{name}: {why}")
    # Policy exhausted: fall back to the strongest usable runtime rather than a weaker one.
    for name in reversed(routing.get("escalation", [])):
        if _blocked(name, available, usage, failures, max_failures) is None:
            return Route(name, f"no L{level} policy runtime usable; strongest available → {name}", skipped)
    return Route(None, "no runtime available", skipped)


def escalate(current: str, cfg: dict, available: set[str], usage: dict[str, Usage] | None = None,
             failures: dict[str, int] | None = None) -> str | None:
    """Next stronger usable runtime after `current` in the escalation chain."""
    routing = cfg["routing"]
    chain = routing.get("escalation", [])
    max_failures = routing.get("attempts_per_runtime", 2)
    start = chain.index(current) + 1 if current in chain else 0
    for name in chain[start:]:
        if name != current and _blocked(name, available, usage or {}, failures or {}, max_failures) is None:
            return name
    return None


def fallback(current: str, cfg: dict, available: set[str], usage: dict[str, Usage] | None = None,
             failures: dict[str, int] | None = None) -> str | None:
    """Replacement when `current` is unusable (quota, outage): prefer stronger, else any."""
    nxt = escalate(current, cfg, available, usage, failures)
    if nxt:
        return nxt
    max_failures = cfg["routing"].get("attempts_per_runtime", 2)
    for name in reversed(cfg["routing"].get("escalation", [])):
        if name != current and _blocked(name, available, usage or {}, failures or {}, max_failures) is None:
            return name
    return None
