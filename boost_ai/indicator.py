"""BOOST_AI top-bar indicator (Ubuntu AppIndicator / StatusNotifierItem).

Standalone on purpose: stdlib + `gi` only, run with the *system* Python (the
project venv has no PyGObject). The TUI launches it; it reads
<root>/.boost-ai/state/current.json, writes requests to <root>/.boost-ai/state/control,
and exits on its own when the BOOST_AI process (--parent-pid) ends.

Wayland does not let one app raise another app's window, so there is no
"Open BOOST_AI" item: the TUI is wherever you left it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ICONS = {
    "RUNNING": "media-playback-start-symbolic",
    "VERIFYING": "media-playback-start-symbolic",
    "REVIEWING": "media-playback-start-symbolic",
    "PLANNING": "media-playback-start-symbolic",
    "INTERVIEWING": "media-playback-start-symbolic",
    "NEEDS_YOU": "dialog-warning-symbolic",
    "REVIEW": "dialog-warning-symbolic",
    "DONE": "emblem-ok-symbolic",
    "FAILED": "dialog-error-symbolic",
    "STOPPED": "media-playback-stop-symbolic",
}
IDLE_ICON = "utilities-terminal-symbolic"
ACTIVE = ("RUNNING", "PLANNING", "INTERVIEWING", "VERIFYING", "REVIEWING", "NEEDS_YOU", "REVIEW", "TRIAGED")
SYSTEM_PYTHON = "/usr/bin/python3"


# ── pure helpers (importable without gi, unit-tested) ──────────────────────
def read_state(root: Path) -> dict:
    try:
        return json.loads((root / ".boost-ai" / "state" / "current.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def label_for(state: dict) -> str:
    status = state.get("status") or "IDLE"
    if status in ("NEEDS_YOU", "REVIEW"):
        return "BOOST_AI · NEEDS YOU"
    if status in ("RUNNING", "PLANNING", "INTERVIEWING", "VERIFYING", "REVIEWING"):
        model = (state.get("model") or "").upper()
        return f"BOOST_AI · {model} · {status}" if model and model != "—" else f"BOOST_AI · {status}"
    if status in ("DONE", "FAILED", "STOPPED"):
        return f"BOOST_AI · {status}"
    return "BOOST_AI"


def icon_for(state: dict) -> str:
    return ICONS.get(state.get("status") or "", IDLE_ICON)


def info_lines(state: dict) -> list[str]:
    task = state.get("task") or "—"
    if state.get("title") and task != "—":
        title = state["title"]
        task = f"{task} · {title[:40] + '…' if len(title) > 40 else title}"
    return [f"Project: {state.get('project') or '—'}", f"Task: {task}",
            f"Runtime: {state.get('model') or '—'}", f"Status: {(state.get('status') or 'IDLE').replace('_', ' ')}"]


def control_path(root: Path) -> Path:
    return root / ".boost-ai" / "state" / "control"


def request(root: Path, action: str) -> None:
    path = control_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(action)
    tmp.replace(path)


def take_request(root: Path) -> str | None:
    """Consume a pending control request ('stop' | 'quit'), if any."""
    path = control_path(root)
    try:
        action = path.read_text().strip()
        path.unlink()
    except OSError:
        return None
    return action if action in ("stop", "quit") else None


def launch(root: Path, cfg: dict) -> subprocess.Popen | None:
    """Start the indicator for this BOOST_AI process; None if disabled or unavailable."""
    icfg = cfg.get("indicator") or {}
    python = icfg.get("python") or SYSTEM_PYTHON
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not icfg.get("enabled", True) or not has_display:
        return None
    if not Path(python).is_file():
        return None
    try:
        return subprocess.Popen([python, "-I", __file__, "--root", str(root), "--parent-pid", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def available(python: str = SYSTEM_PYTHON) -> bool:
    code = ("import gi; gi.require_version('AyatanaAppIndicator3', '0.1'); "
            "from gi.repository import AyatanaAppIndicator3")
    try:
        return subprocess.run([python, "-c", code], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── the indicator process ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(prog="boost-ai-indicator")
    args.add_argument("--root", required=True)
    args.add_argument("--parent-pid", type=int, required=True)
    opts = args.parse_args(argv)
    root = Path(opts.root)

    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError):
        return 3  # PyGObject / AppIndicator typelib missing: run without an indicator

    indicator = AppIndicator.Indicator.new(f"boost-ai-{opts.parent_pid}", IDLE_ICON,
                                           AppIndicator.IndicatorCategory.APPLICATION_STATUS)
    indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    indicator.set_title("BOOST_AI")

    menu = Gtk.Menu()
    info_items = []
    for _ in range(4):
        item = Gtk.MenuItem(label="")
        item.set_sensitive(False)
        menu.append(item)
        info_items.append(item)
    menu.append(Gtk.SeparatorMenuItem())
    stop_item = Gtk.MenuItem(label="Stop task")
    stop_item.connect("activate", lambda _: request(root, "stop"))
    quit_item = Gtk.MenuItem(label="Quit BOOST_AI")
    quit_item.connect("activate", lambda _: request(root, "quit"))
    hide_item = Gtk.MenuItem(label="Hide indicator")
    hide_item.connect("activate", lambda _: Gtk.main_quit())
    for item in (stop_item, quit_item, hide_item):
        menu.append(item)
    menu.show_all()
    indicator.set_menu(menu)

    last: dict = {"_": None}

    def refresh() -> bool:
        try:
            os.kill(opts.parent_pid, 0)
        except ProcessLookupError:
            Gtk.main_quit()
            return False
        except PermissionError:
            pass
        state = read_state(root)
        if state == last["_"]:
            return True
        last["_"] = state
        indicator.set_icon_full(icon_for(state), state.get("status") or "idle")
        indicator.set_label(label_for(state), "BOOST_AI · CLAUDE CODE · VERIFYING")
        for item, text in zip(info_items, info_lines(state), strict=True):
            item.set_label(text)
        stop_item.set_sensitive((state.get("status") or "") in ACTIVE)
        return True

    refresh()
    GLib.timeout_add(1000, refresh)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
