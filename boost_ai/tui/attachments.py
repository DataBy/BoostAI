"""Attachment tray: visible chips above the prompt, add with + / Ctrl+O / drop / paste / /attach,
remove with ✕ or Backspace on an empty prompt."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from textual.containers import Horizontal
from textual.widgets import Button

from ..task import MAX_ATTACHMENT_BYTES

# Absolute, ~ or file:// paths, optionally quoted (what a terminal inserts on drag-and-drop).
_PATH = re.compile(r"""'(?P<q1>(?:/|~|file://)[^'\n]+)'|"(?P<q2>(?:/|~|file://)[^"\n]+)"|"""
                   r"""(?P<bare>(?:file://|/|~/)(?:\\ |\S)+)""")


def _as_file(raw: str) -> Path | None:
    raw = unquote(raw.removeprefix("file://")) if raw.startswith("file://") else raw
    path = Path(raw.replace("\\ ", " ").rstrip(",;")).expanduser()
    try:
        if path.is_file() and path.stat().st_size <= MAX_ATTACHMENT_BYTES:
            return path.resolve()
    except OSError:
        pass
    return None


def extract_paths(text: str) -> tuple[str, list[Path]]:
    """Pull existing file paths out of typed/pasted text. Returns (remaining text, files)."""
    whole = text.strip()
    if whole.startswith(("/", "~/", "file://")) and (path := _as_file(whole)):
        return "", [path]  # a pasted path with unescaped spaces
    files: list[Path] = []

    def take(m: re.Match) -> str:
        path = _as_file(m.group("q1") or m.group("q2") or m.group("bare"))
        if path is None:
            return m.group(0)
        if path not in files:
            files.append(path)
        return ""

    rest = _PATH.sub(take, text)
    return (re.sub(r"\s{2,}", " ", rest).strip() if files else text), files


async def pick_files() -> list[Path] | None:
    """Native GNOME file chooser (zenity). None if no picker/display is available."""
    if not shutil.which("zenity") or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    proc = await asyncio.create_subprocess_exec(
        "zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Attach files to BOOST_AI",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if proc.returncode != 0:  # cancelled
        return []
    return [p for line in out.decode().splitlines() if (p := _as_file(line.strip()))]


class Chip(Button):
    def __init__(self, path: Path):
        super().__init__(f"📎 {path.name}  ✕", classes="chip")
        self.can_focus = False  # removing a chip never takes focus from the prompt
        self.path = path
        self.tooltip = f"{path}\nClick to remove"


class Tray(Horizontal):
    """Files staged for the next message."""

    def __init__(self) -> None:
        super().__init__(id="tray", classes="-empty")
        self.files: list[Path] = []

    def add(self, paths: list[Path]) -> list[Path]:
        added = [p for p in paths if p not in self.files]
        for p in added:
            self.files.append(p)
            self.mount(Chip(p))
        self.set_class(not self.files, "-empty")
        return added

    def remove(self, path: Path) -> None:
        if path in self.files:
            self.files.remove(path)
        for chip in self.query(Chip):
            if chip.path == path:
                chip.remove()
        self.set_class(not self.files, "-empty")

    def pop(self) -> Path | None:
        if not self.files:
            return None
        last = self.files[-1]
        self.remove(last)
        return last

    def take_all(self) -> list[Path]:
        files, self.files = list(self.files), []
        self.remove_children()
        self.add_class("-empty")
        return files
