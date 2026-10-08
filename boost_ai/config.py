"""Configuration: built-in defaults < global < project, deep-merged."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")
# BOOST_AI's own layer, beside the package: harness/skills/<category>/<skill>/SKILL.md and
# harness/rules/<category>/*.md. Never another agent's folder (~/.claude, ~/.agents, ~/.codex).
HARNESS_DIR = Path(__file__).resolve().parent.parent / "harness"


def global_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "boost-ai"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    path = Path(base) / "boost-ai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def deep_merge(base: dict, override: dict) -> dict:
    """Return base updated by override; nested dicts merge, everything else replaces."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load(project_root: Path | None = None) -> dict[str, Any]:
    cfg = _read_yaml(DEFAULTS_PATH)
    cfg = deep_merge(cfg, _read_yaml(global_config_dir() / "config.yaml"))
    if project_root is not None:
        cfg = deep_merge(cfg, _read_yaml(project_root / ".boost-ai" / "config.yaml"))
    return cfg
