"""Colors, the status table and agent colors shared by every view.

Main text uses the terminal's own foreground and muted text the `dim` attribute, so both
follow the terminal's color scheme. Accents come from a palette: `dark` (for dark
backgrounds) or `light` (the same hues, darker: contrast of at least 5:1 on white).
NO_COLOR is honoured by Rich itself (colors are dropped, bold and dim stay)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from .i18n import plural
from .i18n import t as tr
from .model import DONE, FAILED, PENDING, RUNNING, STALE

THEMES = ("auto", "dark", "light")

# extra statuses that only the views know about
SUPERSEDED = "superseded"  # an attempt replaced by a later one (resume, retry)
SKIPPED = "skipped"  # a phase without agents while a later phase has started


@dataclass
class Palette:
    orange: str
    green: str
    blue: str
    purple: str
    red: str
    yellow: str
    cyan: str
    pink: str
    lime: str
    text: str = ""  # main text: the terminal's foreground
    dim: str = "dim"  # muted text, borders of inactive things
    key: str = "bold #ffffff on #1f2430"  # key bar: the key
    label: str = "#000000 on #56b6c2"  # key bar: what it does

    @property
    def agents(self) -> tuple[str, ...]:
        """Colors given to agent labels in turn."""
        return (self.blue, self.purple, self.green, self.orange, self.cyan, self.yellow, self.pink, self.lime)


DARK = Palette(
    orange="#e8875b",
    green="#6cc28a",
    blue="#7aa7f0",
    purple="#b39df0",
    red="#e06c75",
    yellow="#e5c07b",
    cyan="#56b6c2",
    pink="#d19ad9",
    lime="#98c379",
)
LIGHT = Palette(
    orange="#b94c1a",
    green="#337b4c",
    blue="#1a67e5",
    purple="#7750e4",
    red="#cf2c38",
    yellow="#8d651c",
    cyan="#2e767f",
    pink="#a644b5",
    lime="#517836",
    key="bold #ffffff on #3b4252",
    label="#ffffff on #2e767f",
)

C = Palette(**{f.name: getattr(DARK, f.name) for f in fields(Palette)})  # the palette in use
_current = "dark"


def detect_theme(environ=None) -> str:
    """dark or light from COLORFGBG ("fg;bg", set by many terminals); dark when unknown."""
    env = os.environ if environ is None else environ
    bg = (env.get("COLORFGBG") or "").split(";")[-1].strip()
    if not bg.isdigit():
        return "dark"
    n = int(bg)
    return "dark" if n <= 6 or n == 8 else "light"  # the same rule as vim's


def use_theme(name: str = "auto", environ=None) -> str:
    """Switch the palette in use (auto, dark or light); returns the one chosen."""
    if name not in ("dark", "light"):
        name = detect_theme(environ)
    global _current
    src = LIGHT if name == "light" else DARK
    for f in fields(Palette):
        setattr(C, f.name, getattr(src, f.name))
    _current = name
    return name


def current() -> str:
    """The palette in use: dark or light."""
    return _current


def bold(color: str) -> str:
    return f"bold {color}".strip()


# ---- statuses ----


@dataclass(frozen=True)
class Look:
    icon: str
    color: str  # a Palette attribute
    key: str  # message key of the word; "count." + the status is the key of "N <status>"

    @property
    def word(self) -> str:
        return tr(self.key)


STATUS = {
    RUNNING: Look("●", "blue", "status.running"),
    DONE: Look("✓", "green", "status.done"),
    FAILED: Look("✗", "red", "status.failed"),
    STALE: Look("⚠", "yellow", "status.stale"),
    PENDING: Look("○", "dim", "status.pending"),
    SUPERSEDED: Look("↺", "dim", "status.superseded"),
    SKIPPED: Look("○", "dim", "status.skipped"),
}
COUNTED = (RUNNING, DONE, FAILED, STALE)  # what progress counters show, in this order
ATTENTION = (RUNNING, FAILED, STALE, DONE)  # "+N" cards: what needs attention first (the last line may be cut)


def look(status: str) -> Look:
    return STATUS.get(status, STATUS[PENDING])


def status_color(status: str) -> str:
    return getattr(C, look(status).color)


def status_icon(status: str) -> str:
    return look(status).icon


def status_word(status: str) -> str:
    return look(status).word


def status_count(status: str, n: int) -> str:
    """ "3 done", "1 running": a number of agents with this status."""
    return plural(f"count.{status}", n)


def phase_color(status: str) -> str:
    """A phase or a run node: the running one is the accent of the screen."""
    return C.orange if status == RUNNING else status_color(status)


# ---- agent colors ----


class AgentColors:
    """Label -> color, given in order of appearance and kept for the whole session of wfwatch,
    so a label has the same color in every view and every frame."""

    def __init__(self):
        self._index: dict[str, int] = {}

    def __call__(self, label: str) -> str:
        if not label:  # workflow events
            return C.orange
        i = self._index.get(label)
        if i is None:
            i = self._index[label] = len(self._index)
        agents = C.agents
        return agents[i % len(agents)]
