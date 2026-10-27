"""wfwatch – live view of Claude Code workflows and sessions in a separate terminal."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import time
from pathlib import Path

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from . import __version__, i18n, model, theme
from .i18n import t as tr
from .render import Overview, Painter, SessionPainter, keybar, tabs_line
from .widgets import run_duration, run_state

POLL_SEC = 0.5  # re-read files
SCAN_SEC = 3.0  # look for new runs, refresh the session registry and processes
FRAME_SEC = 0.12  # animation
IDLE_FRAME_SEC = 0.5  # nothing animates: redraw twice a second (clocks still tick)
TWO_DIGIT_SEC = 0.7  # two digits pressed within this time select item 10 and above


def list_runs(console: Console, limit: int) -> None:
    """--list: recent runs. Agent transcripts are only peeked at, so this stays cheap on a long history."""
    procs = model.ProcTable.snapshot()
    sessions = model.load_sessions(procs)
    t = Table(box=None, header_style="bold", pad_edge=False, collapse_padding=True)
    # Columns that matter (id, state, agents, time) never shrink. The names may (Rich collapses only
    # wrappable columns), but each cell is a no_wrap Text, so a long name is cut with an ellipsis.
    t.add_column(tr("list.run"), no_wrap=True)
    for key in ("list.workflow", "list.project", "list.session"):
        t.add_column(tr(key), min_width=4)
    t.add_column(tr("list.state"), no_wrap=True)
    t.add_column(tr("list.agents"), no_wrap=True, justify="right")
    t.add_column(tr("list.time"), no_wrap=True, justify="right")
    at = model.now()
    logs: dict[Path, model.SessionLog] = {}  # one read per session transcript, shared by its runs
    for d in model.discover_runs()[:limit]:
        p = d.parents[2].with_suffix(".jsonl")
        r = model.Run(d, log=logs.setdefault(p, model.SessionLog(p)), light=True)
        try:
            r.poll(sessions)
        except Exception as e:  # one unreadable run must not hide the others
            if model.DEBUG:
                raise
            state, color = f"? {model.describe_error(e)}", "red"
        else:
            st = run_state(r)
            state, color = st.text, st.color
        names = [Text(x, no_wrap=True, overflow="ellipsis") for x in (r.name, r.project, r.session_name)]
        # Text, not str: a str cell is read as Rich markup, and these come from the data
        t.add_row(Text(r.run_id), *names, Text(state, style=color), str(len(r.current_agents())), run_duration(r, at))
    console.print(t)


class Guarded:
    """Renders ``inner``; if rendering fails, shows the error instead of crashing (re-raises with --debug).

    It is also the last filter before the terminal: text from the data is cleaned where it is read
    (model.clean), and any control character that still made it into a frame is dropped here."""

    def __init__(self, inner: RenderableType, app: App | None = None):
        self.inner = inner
        self.app = app

    def __rich_console__(self, console, options):
        try:
            lines = console.render_lines(self.inner, options, new_lines=True)
        except Exception as e:  # last line of defence
            if model.DEBUG:
                raise
            msg = model.describe_error(e)
            if self.app is not None:
                self.app.frame_error = msg
            yield Text(tr("error.render", msg=msg), style="bold red", no_wrap=True, overflow="ellipsis")
            return
        bad = model.CONTROL_CHARS
        for line in lines:
            for seg in line:
                if seg.text != "\n" and not seg.control and bad.search(seg.text):
                    seg = Segment(bad.sub("", seg.text), seg.style)
                yield seg


class App:
    """Overview of all workflows and sessions, or a detailed view of one; switched with keys.

    The selected item is a run directory (Path) or a session id (str); numbers are shared:
    workflows first, then sessions, as in the overview."""

    def __init__(self, tracker: model.Tracker, expand_all: bool, pinned: bool = False, view: str = "graph"):
        self.tracker = tracker
        self.pinned = pinned  # the run was chosen explicitly: do not switch to new ones
        self.expand_all = expand_all
        self.view = view
        self.colors = theme.AgentColors()  # one per app: a label keeps its color on every screen
        self.painters: dict[Path, Painter] = {}
        self.session_painters: dict[str, SessionPainter] = {}
        self.mode = "overview"
        self.selected: Path | str | None = None
        self.failures: dict[str, str] = {}  # what failed in the main loop (scan, poll) – shown in a status line
        self.still = False  # full-screen frames without spinners and flowing dots (the screenshots)
        self.frame_error = ""
        self._digit: tuple[int, float] | None = None  # first digit of a two-digit number, and when it came

    # ---- painters ----

    def painter(self, r: model.Run) -> Painter:
        """The painter of a run; a new one when the Run object was replaced (the run was found again)."""
        p = self.painters.get(r.run_dir)
        if p is None or p.run is not r:
            new = Painter(r, self.expand_all, self.view, self.colors)
            if p is not None:
                new.phase_sel, new.scroll = p.phase_sel, p.scroll
            self.painters[r.run_dir] = p = new
        return p

    def session_painter(self, s: model.SessionWatch) -> SessionPainter:
        p = self.session_painters.get(s.session_id)
        if p is None or p.s is not s:
            new = SessionPainter(s, self.colors, self.view)
            if p is not None:
                new.scroll = p.scroll
            self.session_painters[s.session_id] = p = new
        return p

    def prune(self) -> None:
        """Forget painters of runs and sessions the tracker no longer has."""
        for k in [k for k in self.painters if k not in self.tracker.runs]:
            del self.painters[k]
        for k in [k for k in self.session_painters if k not in self.tracker.watches]:
            del self.session_painters[k]

    # ---- navigation ----

    def items(self) -> list[Path | str]:
        """Everything numbered in the overview, in the same order."""
        return [r.run_dir for r in self.tracker.visible()] + [s.session_id for s in self.tracker.active_sessions()]

    def current(self) -> Painter | None:
        if self.mode == "detail" and isinstance(self.selected, Path) and self.selected in self.tracker.runs:
            return self.painter(self.tracker.runs[self.selected])
        return None

    def current_session(self) -> SessionPainter | None:
        if self.mode == "detail" and isinstance(self.selected, str) and self.selected in self.tracker.watches:
            return self.session_painter(self.tracker.watches[self.selected])
        return None

    def keys(self, multi: bool):
        if isinstance(self.selected, str):
            return keybar("session", multi, False, self.view)
        p = self.current()
        return keybar("detail", multi, self.expand_all, self.view, bool(p and p.phase_sel is not None))

    def open(self, item: Path | str) -> None:
        self.mode, self.selected = "detail", item

    def on_new(self, new: list[model.Run]) -> None:
        """In the detailed view of a workflow, move to a new running run only if the current one has stopped."""
        if self.mode != "detail" or not new or self.pinned or isinstance(self.selected, str):
            return
        cur = self.tracker.runs.get(self.selected) if self.selected else None
        if cur is None or cur.state != "running":
            fresh = [r for r in new if r.state == "running"]
            if fresh:
                self.selected = fresh[0].run_dir

    def on_key(self, key: str, at: float | None = None) -> bool:
        """Handle a key; False means quit. ``at`` – monotonic time of the press (for two-digit numbers).

        Letter keys also work in the Russian keyboard layout (й – q, щ – o, ф – a, м – v, а – f)."""
        at = time.monotonic() if at is None else at
        items = self.items()
        if key in ("q", "Q", "й"):
            return False
        if len(key) == 1 and "0" <= key <= "9":  # ASCII only: "²".isdigit() is true too
            self._on_digit(int(key), items, at)
            return True
        self._digit = None
        if key in ("o", "esc", "щ"):
            self.mode = "overview"
        elif key in ("tab", "right", "shift-tab", "left") and items:
            step = 1 if key in ("tab", "right") else -1
            if self.mode == "overview" or self.selected not in items:
                self.open(items[0] if step > 0 else items[-1])
            else:
                self.open(items[(items.index(self.selected) + step) % len(items)])
        elif key in ("a", "ф"):
            self.expand_all = not self.expand_all
            for p in self.painters.values():
                p.expand_all = self.expand_all
        elif key in ("v", "м"):
            self.view = "cards" if self.view == "graph" else "graph"
            for p in [*self.painters.values(), *self.session_painters.values()]:
                p.view, p.scroll = self.view, 0
        elif key in ("up", "down"):
            step = 1 if key == "down" else -1
            if (sp := self.current_session()) is not None:
                sp.scroll += step
            else:
                self._step_phase(step)
        elif key in ("pgup", "pgdn", "home", "end"):
            self._scroll(key)
        elif key in ("f", "а"):
            if (p := self.current()) is not None:
                p.phase_sel, p.scroll = None, 0
            elif (sp := self.current_session()) is not None:
                sp.scroll = 0
        elif key == "enter" and self.mode == "overview" and items:
            self.open(items[0])
        return True

    def _scroll(self, key: str) -> None:
        """Cards: a page up or down, or to the very top or bottom (clamped when drawn)."""
        p = self.current() or self.current_session()
        if p is None:
            return
        if key == "pgup":
            p.scroll -= p.page
        elif key == "pgdn":
            p.scroll += p.page
        else:
            p.scroll = -(10**6) if key == "home" else 10**6

    def _on_digit(self, d: int, items: list, at: float) -> None:
        """1–9 open an item at once; a second digit within TWO_DIGIT_SEC makes it 10 and above; 0 – overview."""
        pending, self._digit = self._digit, None
        if pending is not None and at - pending[1] <= TWO_DIGIT_SEC:
            n = pending[0] * 10 + d
            if n <= len(items):
                self.open(items[n - 1])
                return
        if d == 0:
            self.mode = "overview"
            return
        if d <= len(items):
            self.open(items[d - 1])
        if d * 10 <= len(items):
            self._digit = (d, at)

    def _step_phase(self, step: int) -> None:
        p = self.current()
        if p is None:
            return
        phases = p.run.phases()
        if phases:
            cur = p.phase_sel if p.phase_sel is not None else model.current_phase(phases)
            p.phase_sel = min(max(cur + step, 0), len(phases) - 1)
            p.scroll = 0

    # ---- frames ----

    def animating(self) -> bool:
        """Is anything on screen moving (spinners, flowing dots)? If not, redraw less often."""
        tr = self.tracker
        if self.mode == "detail":
            if isinstance(self.selected, Path):
                r = tr.runs.get(self.selected)
                return r is not None and r.state == "running"
            w = tr.watches.get(self.selected) if isinstance(self.selected, str) else None
            return w is not None and w.animating()
        return any(r.state == "running" for r in tr.visible()) or any(w.animating() for w in tr.active_sessions())

    def safe(self, what: str, fn) -> None:
        """Run a step of the main loop; an unexpected failure goes to the status line (re-raised with --debug)."""
        try:
            fn()
        except Exception as e:  # last line of defence
            if model.DEBUG:
                raise
            self.failures[what] = model.describe_error(e)
        else:
            self.failures.pop(what, None)

    def status_error(self) -> str:
        errs = [f"{k}: {v}" for k, v in self.failures.items()] + self.tracker.errors()
        if not errs:
            return ""
        more = f" (+{len(errs) - 1})" if len(errs) > 1 else ""
        return tr("error.read", msg=errs[0], more=more)

    def _view(self, snapshot: bool, height: int = 0) -> RenderableType:
        self.prune()
        runs, sessions = self.tracker.visible(), self.tracker.active_sessions()
        for r in runs:
            self.painter(r)
        multi = len(runs) + len(sessions) > 1
        tabs = tabs_line(runs, self.selected, sessions) if multi else None
        if self.mode == "detail":
            sel = self.selected
            if isinstance(sel, Path) and sel in self.tracker.runs:
                p = self.painter(self.tracker.runs[sel])
                if snapshot:
                    return p.snapshot(tabs=tabs)
                return p.screen(height, tabs, self.keys(multi), animate=not self.still)
            if isinstance(sel, str) and sel in self.tracker.watches:
                sp = self.session_painter(self.tracker.watches[sel])
                if snapshot:
                    return sp.snapshot(tabs=tabs)
                return sp.screen(height, tabs, self.keys(multi), animate=not self.still)
            self.mode, self.selected = "overview", None  # the selected item is gone
        ov = Overview(runs, sessions, self.colors)
        ov.animate = not self.still
        return ov.snapshot() if snapshot else ov.screen(height)

    def _safe_view(self, snapshot: bool, height: int = 0) -> RenderableType:
        try:
            return Guarded(self._view(snapshot, height), self)
        except Exception as e:  # last line of defence
            if model.DEBUG:
                raise
            self.frame_error = model.describe_error(e)
            return Text(tr("error.render", msg=self.frame_error), style="bold red")

    def frame(self, height: int) -> RenderableType:
        """The full-screen frame; a failure while reading shows in a status line at the bottom."""
        self.frame_error = ""
        msg = self.status_error()
        if not msg:
            return self._safe_view(False, height)
        lay = Layout()
        lay.split_column(
            Layout(self._safe_view(False, height - 1), ratio=1),
            Layout(Text(model.clean(msg), style="bold red", no_wrap=True, overflow="ellipsis"), size=1),
        )
        return lay

    def snapshot(self) -> RenderableType:
        """One frame for --once (natural height, no key bar)."""
        self.frame_error = ""
        view = self._safe_view(True)
        msg = self.status_error()
        return Group(view, Text(model.clean(msg), style="bold red")) if msg else view


def choose_start(app: App, overview: bool = False, pinned: Path | None = None) -> None:
    """The first screen: a pinned run; or, if exactly one workflow is going and nothing else (its own
    session does not count), straight to its diagram; if nothing goes at all, the latest run."""
    tracker = app.tracker
    if pinned is not None:
        app.open(pinned)
        return
    if overview:
        return
    runs = tracker.visible()
    owners = {r.session_id for r in runs}
    others = [s for s in tracker.active_sessions() if s.session_id not in owners]
    if len(runs) == 1 and not others:
        app.open(runs[0].run_dir)
    elif not runs and not others:
        latest = model.discover_runs()[:1]
        if latest:
            tracker.extra = latest[0]
            app.safe("scan", tracker.scan)
            app.open(latest[0])


class _Terminated(BaseException):
    """SIGTERM/SIGHUP: unwind like Ctrl+C so the terminal is restored (BaseException: never swallowed)."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def run_live(app: App, console: Console) -> int:
    """The full-screen mode: redraw, re-read, handle keys until q, Ctrl+C or a termination signal."""
    from .keys import Keys  # termios: POSIX only

    tracker = app.tracker
    got: list[int] = []

    def on_signal(signum, frame):
        got.append(signum)
        raise _Terminated(signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):  # not the main thread
                previous[sig] = signal.signal(sig, on_signal)
    last_poll = last_scan = time.monotonic()
    try:
        with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                t = time.monotonic()
                if t - last_scan >= SCAN_SEC:
                    last_scan = t
                    app.safe("scan", lambda: app.on_new(tracker.scan()))
                if t - last_poll >= POLL_SEC:
                    last_poll = t
                    app.safe("poll", tracker.poll)
                live.update(app.frame(console.size.height), refresh=True)
                for k in keys.poll(FRAME_SEC if app.animating() else IDLE_FRAME_SEC):
                    if not app.on_key(k):
                        return 0
    except KeyboardInterrupt:
        return 0
    except _Terminated as e:
        return 128 + e.signum
    except OSError:
        if got:  # the terminal went away with SIGHUP: nothing left to restore
            return 128 + got[0]
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _err(msg: str) -> None:
    print(f"wfwatch: {msg}", file=sys.stderr)


def _isatty(f) -> bool:
    """A real terminal. Rich's is_terminal alone is not enough: FORCE_COLOR makes it true for a pipe."""
    try:
        return f.isatty()
    except (AttributeError, ValueError, OSError):
        return False


class _Formatter(argparse.HelpFormatter):
    """argparse help in the interface language ("usage:" included)."""

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, tr("cli.usage") if prefix is None else prefix)


def build_parser() -> argparse.ArgumentParser:
    """The command line, described in the current language (see i18n.use_lang)."""
    ap = argparse.ArgumentParser(
        prog="wfwatch", description=tr("cli.description"), add_help=False, formatter_class=_Formatter
    )
    pos = ap.add_argument_group(tr("cli.arguments"))
    opt = ap.add_argument_group(tr("cli.options"))
    pos.add_argument("run", nargs="?", help=tr("help.run"))
    opt.add_argument("-h", "--help", action="help", help=tr("help.help"))
    opt.add_argument("-V", "--version", action="version", version=f"wfwatch {__version__}", help=tr("help.version"))
    opt.add_argument("-l", "--list", action="store_true", help=tr("help.list"))
    opt.add_argument("-n", type=int, default=15, help=tr("help.n"))
    opt.add_argument("-o", "--overview", action="store_true", help=tr("help.overview"))
    opt.add_argument("-a", "--all", action="store_true", help=tr("help.all"))
    opt.add_argument("-c", "--cards", action="store_true", help=tr("help.cards"))
    opt.add_argument("--once", action="store_true", help=tr("help.once"))
    opt.add_argument("--width", type=int, help=tr("help.width"))
    opt.add_argument("--theme", choices=theme.THEMES, default="auto", help=tr("help.theme"))
    opt.add_argument("--lang", choices=i18n.CHOICES, default="auto", help=tr("help.lang"))
    opt.add_argument("--debug", action="store_true", help=tr("help.debug"))
    return ap


def lang_option(argv: list[str]) -> str:
    """The value of --lang, looked up before parsing: the help itself must be in that language."""
    value = "auto"
    for i, a in enumerate(argv):
        if a == "--":
            break
        if a == "--lang" and i + 1 < len(argv):
            value = argv[i + 1]
        elif a.startswith("--lang="):
            value = a.partition("=")[2]
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:  # Ctrl+C during the first scan, --list or --once
        return 130
    except BrokenPipeError:  # `wfwatch --list | head`
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _main(argv: list[str] | None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    debug, palette, lang = model.DEBUG, theme.current(), i18n.current()
    try:
        i18n.use_lang(lang_option(argv))  # an unknown value: auto here, then the parser rejects it
        args = build_parser().parse_args(argv)
        i18n.use_lang(args.lang)
        console = Console(width=args.width) if args.width else Console()
        model.DEBUG = debug or args.debug
        theme.use_theme(args.theme)
        return _run(args, console)
    finally:
        model.DEBUG = debug
        theme.use_theme(palette)
        i18n.use_lang(lang)


def _run(args: argparse.Namespace, console: Console) -> int:
    if args.list:
        list_runs(console, args.n)
        return 0
    pinned = None
    if args.run is not None:
        pinned = model.find_run(args.run)
        if pinned is None:
            console.print(tr("cli.not_found", run=args.run), markup=False, highlight=False)
            return 1
    if not args.once:
        if sys.platform == "win32":
            _err(tr("cli.windows"))
            return 2
        if not (console.is_terminal and _isatty(console.file)):
            _err(tr("cli.no_tty"))
            return 2

    tracker = model.Tracker(extra=pinned)
    app = App(tracker, args.all, pinned=pinned is not None, view="cards" if args.cards else "graph")
    app.safe("scan", tracker.scan)
    choose_start(app, overview=args.overview, pinned=pinned)

    if args.once:
        console.print(app.snapshot())
        return 1 if app.frame_error else 0
    return run_live(app, console)


if __name__ == "__main__":
    raise SystemExit(main())
