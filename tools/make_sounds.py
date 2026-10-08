"""Regenerate the bundled alert sounds (soft, short sine chimes). Run: python tools/make_sounds.py"""

import math
import struct
import wave
from pathlib import Path

RATE = 22050
OUT = Path(__file__).resolve().parent.parent / "boost_ai" / "sounds"


def note(freq: float, dur: float, gain: float = 0.5) -> list[float]:
    n = int(RATE * dur)
    out = []
    for i in range(n):
        t = i / RATE
        env = min(1.0, t / 0.008) * math.exp(-t * 7.0)  # quick soft attack, bell-like decay
        s = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
        out.append(gain * env * s / 1.25)
    return out


def sequence(notes: list[tuple[float, float]], overlap: float = 0.06) -> list[float]:
    buf: list[float] = []
    for freq, dur in notes:
        tone = note(freq, dur + 0.25)
        start = max(0, len(buf) - int(RATE * overlap)) if buf else 0
        buf.extend([0.0] * max(0, start + len(tone) - len(buf)))
        for i, s in enumerate(tone):
            buf[start + i] += s
        buf = buf[: start + int(RATE * dur)] + buf[start + int(RATE * dur):]
    return buf


def write(name: str, samples: list[float]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    peak = max(abs(s) for s in samples) or 1.0
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(32767 * 0.8 * s / peak)) for s in samples))


write("soft_ping", sequence([(1318.5, 0.09), (1760.0, 0.12)]))            # E6 → A6
write("soft_success", sequence([(1046.5, 0.08), (1318.5, 0.08), (1568.0, 0.16)]))  # C6 E6 G6
write("soft_warning", sequence([(880.0, 0.12), (698.5, 0.18)]))           # A5 → F5
