#!/usr/bin/env python3
"""The screenshots in docs/: SVG files (Rich) and an animated GIF (Pillow), made from demo data only.

    python scripts/screenshots.py              # docs/*.svg and, if Pillow is installed, docs/demo.gif
    python scripts/screenshots.py --no-gif     # only the SVG files
    pip install -e '.[demo]'                   # Pillow, for the GIF

Everything comes from scripts/demo_data.py, never from your own ~/.claude: the data goes to a
temporary directory, the clock is fixed (a run is always at the same moment, the times are in UTC)
and the process list is made up too, so with the same version of Rich the SVG files come out the
same on every machine. The frames are what wfwatch draws in a terminal of the given size: the same
App, the same keys.

The GIF is drawn from the character grid with a monospace font (Fira Code if it is installed,
else DejaVu Sans Mono or Menlo; --font to choose); box-drawing characters are drawn as lines, so
the frames join up whatever the font.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # the wfwatch of this checkout, installed or not
sys.path.insert(0, str(ROOT / "scripts"))

import demo_data  # noqa: E402

START = datetime(2026, 10, 27, 14, 30, 0, tzinfo=timezone.utc)  # the moment the screenshots show

# A made-up process list: the session processes, and for nightly-bench the shell its Bash tool
# started for the background command, with the command inside.
PIDS = {"release-prep": 4101, "cache-refactor": 4102, "docs-rewrite": 4103, "nightly-bench": 4104, "deps-bump": 4105}
PS = """\
    1     0   0.0 10-02:11:45 /sbin/launchd
 4101     1   0.4    01:12:30 claude
 4102     1   2.1    03:05:12 claude
 4103     1   0.2       47:10 claude
 4104     1   0.3    02:41:09 claude
 4105     1   0.1    01:02:44 claude
 4210  4104   0.0       05:31 /bin/zsh -c source ~/.claude/shell-snapshots/snap.sh && eval 'python bench/run.py'
 4211  4210  97.4       05:30 python3 bench/run.py --all
"""


class Clock:
    """Stands in for the wall clock (model.now) and the animation clock (time.monotonic)."""

    def __init__(self, start: datetime):
        self.start = start
        self.t = 0.0  # seconds since the start

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    def mono(self) -> float:
        return 1000.0 + self.t


@dataclass
class Shot:
    name: str  # file name without extension
    lang: str
    size: tuple[int, int]  # columns, rows
    keys: tuple[str, ...] = ()  # pressed before the frame is taken
    at: float = 0.0  # seconds into the demo story
    title: str = ""


SHOTS = [
    Shot("overview", "en", (128, 46), title="wfwatch – overview"),
    Shot("workflow", "en", (140, 44), keys=("2",), title="wfwatch – workflow diagram"),
    Shot("session", "en", (128, 40), keys=("3",), title="wfwatch – session"),
    Shot("overview.ru", "ru", (128, 46), title="wfwatch – обзор"),
]

# The GIF: (seconds to stay, key pressed first)
TOUR = [(2.5, ""), (9.5, "2"), (3.5, "3"), (2.5, "o")]
TOUR_START = 36.0  # seconds into the story: the "Fix" phase is about to finish, "Review" to start
FRAME_SEC = 0.15


class Session:
    """Demo data in a temporary directory and wfwatch pointed at it, on a fixed clock.

    wfwatch is patched in place (its data directory, clocks, process list, language, theme);
    ``close`` puts everything back."""

    def __init__(self, lang: str, at: float = 0.0):
        from wfwatch import i18n, model, render, theme
        from wfwatch.cli import App, choose_start

        self._undo = contextlib.ExitStack()
        self.tmp = self._undo.enter_context(tempfile.TemporaryDirectory(prefix="wfwatch-shots-"))
        home = Path(self.tmp)
        self.clock = Clock(START)
        self._env("TZ", "UTC")  # the log shows local times
        self._env("CLAUDE_CONFIG_DIR", str(home))
        self._set(model, "CLAUDE_DIR", home)
        self._set(model, "CLAUDE_TMP", home / "tmp")
        self._set(model, "now", self.clock.now)
        self._set(render, "time", types.SimpleNamespace(monotonic=self.clock.mono))
        self._set(model.ProcTable, "snapshot", classmethod(lambda cls: cls.parse(PS, self.clock.now().timestamp())))
        lang_was, theme_was = i18n.current(), theme.current()
        self._undo.callback(i18n.use_lang, lang_was)
        self._undo.callback(theme.use_theme, theme_was)
        i18n.use_lang(lang)
        theme.use_theme("dark")

        self.demo = demo_data.Demo(home, lang, PIDS, start=START)
        self.demo.build()
        self.advance(at)
        self.tracker = model.Tracker()
        self.tracker.scan()
        self.tracker.poll()
        self.app = App(self.tracker, expand_all=False)
        choose_start(self.app)
        self._scan_at = self.clock.t

    def _set(self, obj, name: str, value) -> None:
        old = obj.__dict__[name] if isinstance(obj, type) else getattr(obj, name)
        self._undo.callback(setattr, obj, name, old)
        setattr(obj, name, value)

    def _env(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        self._undo.callback(self._restore_env, name, old)
        os.environ[name] = value
        if name == "TZ" and hasattr(time, "tzset"):
            time.tzset()

    @staticmethod
    def _restore_env(name: str, old: str | None) -> None:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old
        if name == "TZ" and hasattr(time, "tzset"):
            time.tzset()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._undo.close()

    def advance(self, t: float) -> None:
        self.clock.t = t
        self.demo.advance(t)

    def tick(self, t: float) -> None:
        """What the main loop does between frames: write the story up to t, re-read, rescan."""
        self.advance(t)
        if t - self._scan_at >= 3.0:
            self._scan_at = t
            self.app.on_new(self.tracker.scan())
        self.tracker.poll()

    def press(self, key: str) -> None:
        self.app.on_key(key, at=self.clock.mono())

    @staticmethod
    def console(size: tuple[int, int]):
        from rich.console import Console

        cols, rows = size
        # _environ: the frames must not depend on NO_COLOR, COLUMNS and the like of the caller
        return Console(
            width=cols,
            height=rows,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            record=True,
            legacy_windows=False,
            _environ={},
        )

    def frame(self, rows: int):
        """The full-screen frame, as the live mode draws it."""
        return self.app.frame(rows)


def take_svg(shot: Shot, out: Path) -> Path:
    with Session(shot.lang, shot.at) as s:
        s.app.still = True  # a picture: ● for what runs, as --once draws it (a frozen spinner reads as ◐)
        for k in shot.keys:
            s.press(k)
        con = s.console(shot.size)
        con.print(s.frame(shot.size[1]))
        path = out / f"{shot.name}.svg"
        con.save_svg(str(path), title=shot.title or "wfwatch")
        return path


# ---- GIF ----

FONTS = [
    "~/Library/Fonts/FiraCode-Regular.ttf",
    "/Library/Fonts/FiraCode-Regular.ttf",
    "/usr/share/fonts/truetype/firacode/FiraCode-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]
FALLBACK_FONTS = [  # for symbols the main font lacks
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
]

# box drawing: character -> (up, down, left, right), each 0 none, 1 light, 2 heavy
_BOX = {
    "─": (0, 0, 1, 1),
    "━": (0, 0, 2, 2),
    "│": (1, 1, 0, 0),
    "┃": (2, 2, 0, 0),
    "┌": (0, 1, 0, 1),
    "┐": (0, 1, 1, 0),
    "└": (1, 0, 0, 1),
    "┘": (1, 0, 1, 0),
    "┏": (0, 2, 0, 2),
    "┓": (0, 2, 2, 0),
    "┗": (2, 0, 0, 2),
    "┛": (2, 0, 2, 0),
    "├": (1, 1, 0, 1),
    "┤": (1, 1, 1, 0),
    "┬": (0, 1, 1, 1),
    "┴": (1, 0, 1, 1),
    "┼": (1, 1, 1, 1),
    "┣": (2, 2, 0, 2),
    "┫": (2, 2, 2, 0),
    "┳": (0, 2, 2, 2),
    "┻": (2, 0, 2, 2),
    "╋": (2, 2, 2, 2),
    "╽": (1, 2, 0, 0),
    "╿": (2, 1, 0, 0),
    "╴": (0, 0, 1, 0),
    "╶": (0, 0, 0, 1),
    "╵": (1, 0, 0, 0),
    "╷": (0, 1, 0, 0),
}
_ARCS = {"╭": (0, 1, 0, 1), "╮": (0, 1, 1, 0), "╯": (1, 0, 1, 0), "╰": (1, 0, 0, 1)}
_DASHED = {"┄": ("h", 3), "┈": ("h", 4), "╌": ("h", 2), "┆": ("v", 3), "┊": ("v", 4), "╎": ("v", 2)}


def find_font(paths: list[str]) -> str | None:
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return None


class Raster:
    """Draws a grid of styled characters into an image, the way a terminal would."""

    def __init__(self, font: str, size: int = 15):
        from PIL import ImageFont
        from rich.terminal_theme import SVG_EXPORT_THEME

        self.theme = SVG_EXPORT_THEME
        self.size = size
        self.font = ImageFont.truetype(font, size)
        bold = font.replace("Regular", "Bold") if "Regular" in font else font
        self.bold = ImageFont.truetype(bold if os.path.exists(bold) else font, size)
        self.fallbacks = [ImageFont.truetype(p, size) for p in FALLBACK_FONTS if os.path.exists(p)]
        self.cw = round(self.font.getlength("M"))
        ascent, descent = self.font.getmetrics()
        self.ch = round((ascent + descent) * 1.15)
        self.baseline = round((self.ch - ascent - descent) / 2 + ascent)
        self._notdef: dict[int, bytes] = {}
        self._glyphs: dict[tuple[str, bool, int], object] = {}

    # ---- colors ----

    def colors(self, style) -> tuple[tuple[int, int, int], tuple[int, int, int] | None]:
        """(foreground, background or None for the default) – the rules of Rich's SVG export."""
        from rich.color import blend_rgb

        t = self.theme
        fg = (
            t.foreground_color
            if style is None or style.color is None or style.color.is_default
            else style.color.get_truecolor(t)
        )
        default_bg = style is None or style.bgcolor is None or style.bgcolor.is_default
        bg = t.background_color if default_bg else style.bgcolor.get_truecolor(t)
        if style is not None and style.reverse:
            fg, bg, default_bg = bg, fg, False
        if style is not None and style.dim:
            fg = blend_rgb(fg, bg, 0.4)
        return tuple(fg), None if default_bg else tuple(bg)

    # ---- glyphs ----

    def _ink(self, font, ch: str) -> bytes:
        from PIL import Image, ImageDraw

        img = Image.new("L", (self.cw * 3, self.ch), 0)
        ImageDraw.Draw(img).text((self.cw, self.baseline), ch, font=font, fill=255, anchor="ls")
        return img.tobytes()

    def _has(self, font, ch: str) -> bool:
        """Does the font have the character? A missing one is drawn as the font's "no glyph" box."""
        key = id(font)
        if key not in self._notdef:
            self._notdef[key] = self._ink(font, "\U0010fffd")
        ink = self._ink(font, ch)
        return any(ink) and ink != self._notdef[key]

    def glyph(self, ch: str, bold: bool, cells: int):
        """The glyph as an "L" mask of the cell size."""
        key = (ch, bold, cells)
        if key in self._glyphs:
            return self._glyphs[key]
        from PIL import Image, ImageDraw

        w = self.cw * cells
        img = Image.new("L", (w, self.ch), 0)
        d = ImageDraw.Draw(img)
        if not self._box(d, ch, w):
            font = self.bold if bold else self.font
            if not self._has(font, ch):
                font = next((f for f in self.fallbacks if self._has(f, ch)), font)
            gw = font.getlength(ch)
            d.text(((w - gw) / 2, self.baseline), ch, font=font, fill=255, anchor="ls")
        self._glyphs[key] = img
        return img

    def _box(self, d, ch: str, w: int) -> bool:
        """Box-drawing and block characters drawn as shapes, so they join between cells."""
        h, cx, cy = self.ch, w // 2, self.ch // 2
        thin, thick = max(1, round(self.size / 14)), max(2, round(self.size / 6))
        if ch in _BOX:
            up, down, left, right = _BOX[ch]
            for weight, box in (
                (up, (cx, 0, cx, cy)),
                (down, (cx, cy, cx, h)),
                (left, (0, cy, cx, cy)),
                (right, (cx, cy, w, cy)),
            ):
                if weight:
                    lw = thick if weight == 2 else thin
                    x0, y0, x1, y1 = box
                    d.rectangle((x0 - lw // 2, y0 - lw // 2, x1 + (lw - 1) // 2, y1 + (lw - 1) // 2), fill=255)
            return True
        if ch in _ARCS:
            up, down, left, right = _ARCS[ch]
            r = min(cx, cy)
            # the straight parts up to the arc, then a quarter circle
            ex = 0 if left else w
            ey = 0 if up else h
            d.line((cx, ey, cx, cy + (r if down else -r)), fill=255, width=thin)
            d.line((ex, cy, cx + (r if right else -r), cy), fill=255, width=thin)
            bx = cx if right else cx - 2 * r
            by = cy if down else cy - 2 * r
            start = {(1, 0, 0, 1): 90, (1, 0, 1, 0): 0, (0, 1, 1, 0): 270, (0, 1, 0, 1): 180}[(up, down, left, right)]
            d.arc((bx, by, bx + 2 * r, by + 2 * r), start, start + 90, fill=255, width=thin)
            return True
        if ch in _DASHED:
            axis, n = _DASHED[ch]
            length = w if axis == "h" else h
            n = max(1, min(n, length // 4))  # a narrow cell: fewer dashes, but always with gaps
            seg = length / n
            gap = max(2.0, seg * 0.4)
            for i in range(n):
                a, b = round(i * seg + gap / 2), round((i + 1) * seg - gap / 2) - 1
                if axis == "h":
                    d.rectangle((a, cy, b, cy + thin - 1), fill=255)
                else:
                    d.rectangle((cx, a, cx + thin - 1, b), fill=255)
            return True
        if "▁" <= ch <= "█":  # lower eighths
            k = ord(ch) - 0x2580
            d.rectangle((0, h - round(h * k / 8), w - 1, h - 1), fill=255)
            return True
        return False

    # ---- frames ----

    def render(self, rows: list[list[tuple[str, object, int]]], title: str = "", badge: str = ""):
        """rows of (character, style, cells) → an RGB image with a window frame like Rich's SVG."""
        from PIL import Image, ImageDraw

        cols = max(sum(c for _, _, c in r) for r in rows)
        pad, bar = 18, 34
        width, height = cols * self.cw + 2 * pad, len(rows) * self.ch + bar + pad
        bg = tuple(self.theme.background_color)
        img = Image.new("RGB", (width, height), (18, 18, 18))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, width - 1, height - 1), radius=10, fill=bg, outline=(70, 70, 70))
        for i, c in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
            d.ellipse((16 + i * 20, 12, 28 + i * 20, 24), fill=c)
        if title:
            tw = self.font.getlength(title)
            d.text(((width - tw) / 2, 23), title, font=self.font, fill=(150, 150, 150), anchor="ls")
        if badge:
            bw = self.bold.getlength(badge) + 16
            d.rounded_rectangle((width - bw - 14, 7, width - 14, 29), radius=6, fill=(229, 192, 123))
            d.text((width - bw - 6, 23), badge, font=self.bold, fill=(30, 30, 30), anchor="ls")
        for y, row in enumerate(rows):
            x = 0
            top = bar + y * self.ch
            for ch, style, cells in row:
                fore, back = self.colors(style)
                left = pad + x * self.cw
                if back is not None:
                    d.rectangle((left, top, left + cells * self.cw - 1, top + self.ch - 1), fill=back)
                if ch.strip():
                    img.paste(fore, (left, top), self.glyph(ch, bool(style is not None and style.bold), cells))
                if style is not None and style.underline:
                    d.line((left, top + self.ch - 3, left + cells * self.cw, top + self.ch - 3), fill=fore)
                x += cells
        return img


def grid(console, renderable) -> list[list[tuple[str, object, int]]]:
    """The character grid of a frame: rows of (character, style, cells)."""
    from rich.cells import get_character_cell_size

    rows = []
    for line in console.render_lines(renderable, console.options, pad=True):
        row = []
        for seg in line:
            if seg.control:
                continue
            for ch in seg.text:
                n = get_character_cell_size(ch)
                if n == 0:
                    if row:  # a combining character: keep it with the previous one
                        prev = row[-1]
                        row[-1] = (prev[0] + ch, prev[1], prev[2])
                    continue
                row.append((ch, seg.style, n))
        rows.append(row)
    return rows


def tour_frames(lang: str = "en", size: tuple[int, int] = (128, 40)) -> Iterator[tuple[list, str]]:
    """Frames of the tour: (grid, the key shown in the corner)."""
    with Session(lang, TOUR_START) as s:
        con = s.console(size)
        t = TOUR_START
        for stay, key in TOUR:
            if key:
                s.press(key)
            shown_until = t + 1.2
            end = t + stay
            while t < end:
                s.tick(t)
                badge = f"key {key}" if key and t < shown_until else ""
                yield grid(con, s.frame(size[1])), badge
                t += FRAME_SEC


def make_gif(out: Path, font: str, size: tuple[int, int] = (128, 40)) -> Path:
    from PIL import Image

    r = Raster(font)
    frames = [r.render(g, "wfwatch", badge) for g, badge in tour_frames("en", size)]
    # one palette for every frame (taken from a few of them), so colors do not flicker
    picks = frames[:: max(1, len(frames) // 6)]
    sample = Image.new("RGB", (frames[0].width, frames[0].height * len(picks)))
    for i, f in enumerate(picks):
        sample.paste(f, (0, i * f.height))
    palette = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    quantized = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    path = out / "demo.gif"
    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=round(FRAME_SEC * 1000),
        loop=0,
        optimize=True,
        disposal=1,
    )
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make the screenshots in docs/ from demo data.")
    ap.add_argument("--out", type=Path, default=ROOT / "docs", help="where to write (default: docs/)")
    ap.add_argument("--no-gif", action="store_true", help="only the SVG files")
    ap.add_argument("--font", help="a monospace TTF/OTF font for the GIF")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        print(take_svg(shot, args.out))
    if args.no_gif:
        return 0
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("screenshots: Pillow is not installed (pip install -e '.[demo]') – no GIF", file=sys.stderr)
        return 0
    font = args.font or find_font(FONTS)
    if font is None:
        print("screenshots: no monospace font found – pass --font PATH", file=sys.stderr)
        return 1
    print(make_gif(args.out, font))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
