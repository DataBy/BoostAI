"""Mask secrets before anything is written to logs, transcripts or memory."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,})"),            # OpenAI / Anthropic / DeepSeek
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),                       # GitHub
    re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),                               # AWS access key id
    re.compile(r"\b(AIza[0-9A-Za-z_-]{30,})"),                           # Google API key
    re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})"),                    # Slack
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),  # JWT
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{16,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
# key=value / key: value for sensitive names; the value is masked, the name kept.
_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passwd|password|private[_-]?key)[A-Z0-9_]*)"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;]{6,})")


def redact(text: str) -> str:
    if not text:
        return text
    for rx in _PATTERNS:
        text = rx.sub(lambda m: m.group(0).replace(m.group(m.lastindex or 0), "[REDACTED]"), text)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
