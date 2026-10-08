"""Desktop notifications (notify-send) and short sounds (pw-play / canberra / aplay).

Fire-and-forget subprocesses; any failure is silently ignored — alerts are optional.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SOUND_DIR = Path(__file__).with_name("sounds")
EVENT_URGENCY = {"needs_input": "normal", "completed": "low", "warning": "critical"}


class Alerts:
    def __init__(self, cfg: dict):
        self.notify_enabled = (cfg.get("notifications") or {}).get("enabled", True)
        sounds = cfg.get("sounds") or {}
        self.sound_enabled = sounds.get("enabled", True)
        self.volume = float(sounds.get("volume", 0.25))
        self.events = sounds.get("events") or {}

    def alert(self, event: str, title: str, body: str = "") -> None:
        self.notify(title, body, EVENT_URGENCY.get(event, "normal"))
        self.sound(event)

    def notify(self, title: str, body: str = "", urgency: str = "normal") -> None:
        if not self.notify_enabled or not shutil.which("notify-send"):
            return
        _spawn(["notify-send", "-a", "BOOST_AI", "-u", urgency, title, body[:300]])

    def sound(self, event: str) -> None:
        if not self.sound_enabled:
            return
        name = self.events.get(event)
        path = SOUND_DIR / f"{name}.wav" if name else None
        if not path or not path.is_file():
            return
        if shutil.which("pw-play"):
            _spawn(["pw-play", "--volume", f"{self.volume:.2f}", str(path)])
        elif shutil.which("paplay"):
            _spawn(["paplay", f"--volume={int(self.volume * 65536)}", str(path)])
        elif shutil.which("aplay"):
            _spawn(["aplay", "-q", str(path)])


def _spawn(argv: list[str]) -> None:
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass
