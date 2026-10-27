"""The diagram of a run: phases on the left, the current phase fanned out into its agents,
flowing dots along the links. And the same for an ordinary session and its subagents.

Everything is drawn on a character grid (Canvas) that knows that some characters take two
terminal cells."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import NamedTuple

from rich.cells import cell_len, get_character_cell_size
from rich.text import Text

from .i18n import styled
from .i18n import t as tr
from .model import (
    BUSY,
    CONTROL_CHARS,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    STALE,
    Agent,
    BgTask,
    Phase,
    Run,
    SessionWatch,
    current_phase,
    short_model,
)
from .theme import (
    ATTENTION,
    SKIPPED,
    SUPERSEDED,
    AgentColors,
    C,
    bold,
    phase_color,
    status_color,
    status_count,
    status_icon,
    status_word,
)
from .widgets import (
    SESSION_TAG,
    action_text,
    agent_look,
    agent_status,
    bg_done_parts,
    bg_parts,
    calls_stats,
    counts,
    event_text,
    fit,
    fmt_dur,
    fmt_k,
    hhmm,
    join,
    last_action,
    live_icon,
    model_color,
    phase_note,
    phase_status,
    phase_title,
    run_duration,
    run_state,
    session_since,
    session_state,
    session_state_text,
    short_models,
    sparkline,
    status_counts,
    where,
)

try:  # Rich 14+: grapheme clusters (emoji sequences) are measured as a whole
    from rich.cells import split_graphemes as _split_graphemes
except ImportError:  # pragma: no cover – older Rich measures code points one by one
    _split_graphemes = None

SIDE_W = 28  # the phase column on the left
SIDEBAR_MIN_W = 100  # narrower: no phase column
MIN_W = 70  # narrower: no diagram at all, cards instead
GAP = 2  # between agent cards
NODE_H = 4  # a phase block: frame and two lines
TOP = 4  # rows above the diagram: title, legend, rule and a blank line
CARD_W_MAX, CARD_W_MIN, CARD_W_OVER = 30, 20, 22  # card widths: all in a row / with a "+N" card
MAX_ROWS = 4  # rows of cards in the fan

# (up, down, left, right) -> line junction
_J = {
    (1, 1, 0, 0): "│",
    (0, 0, 1, 1): "─",
    (0, 1, 0, 1): "┌",
    (0, 1, 1, 0): "┐",
    (1, 0, 0, 1): "└",
    (1, 0, 1, 0): "┘",
    (1, 1, 0, 1): "├",
    (1, 1, 1, 0): "┤",
    (0, 1, 1, 1): "┬",
    (1, 0, 1, 1): "┴",
    (1, 1, 1, 1): "┼",
    (0, 1, 0, 0): "│",
    (1, 0, 0, 0): "│",
    (0, 0, 0, 1): "─",
    (0, 0, 1, 0): "─",
}


def junction(up: bool, down: bool, left: bool, right: bool) -> str:
    return _J.get((int(up), int(down), int(left), int(right)), " ")


# ---- the character grid ----

Cell = tuple[str, int, str]  # grapheme, cells it takes (1 or 2), style


def _sanitize(s: str) -> str:
    """A control character never reaches the grid (the data is cleaned on reading; this is the backstop)."""
    return CONTROL_CHARS.sub(" ", s)


def graphemes(s: str) -> list[tuple[str, int]]:
    """(grapheme, width in cells), measured the way Rich measures it."""
    s = _sanitize(s)
    if _split_graphemes is not None:
        spans, _ = _split_graphemes(s)
        return [(s[a:b], w) for a, b, w in spans if w > 0]
    out: list[tuple[str, int]] = []
    for ch in s:
        w = get_character_cell_size(ch)
        if w == 0:
            if out:
                out[-1] = (out[-1][0] + ch, out[-1][1])
        else:
            out.append((ch, w))
    return out


def _cells(parts: list[tuple[str, str]]) -> list[Cell]:
    return [(g, w, style) for s, style in parts for g, w in graphemes(s)]


def _fit_cells(cells: list[Cell], width: int) -> list[Cell]:
    """Cut to ``width`` cells, ending with an ellipsis when something was cut."""
    if width <= 0:
        return []
    if sum(w for _, w, _ in cells) <= width:
        return cells
    out, used = [], 0
    for c in cells:
        if used + c[1] > width - 1:
            break
        out.append(c)
        used += c[1]
    return [*out, ("…", 1, out[-1][2] if out else (cells[0][2] if cells else ""))]


class Canvas:
    """A character grid with styles; turned into a rich Text at the end.

    A wide character (CJK, most emoji) takes its cell and the next one; the next one is then a
    continuation (width 0) that is skipped on output."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[""] * w for _ in range(h)]
        self.wd = [[1] * w for _ in range(h)]  # 1 normal, 2 the first cell of a wide character, 0 its second cell

    def _free(self, x: int, y: int) -> None:
        """Before writing to (x, y): a wide character that overlaps the cell is replaced by a blank."""
        wd, ch = self.wd[y], self.ch[y]
        if wd[x] == 0 and x > 0:
            ch[x - 1], wd[x - 1] = " ", 1
        elif wd[x] == 2 and x + 1 < self.w:
            ch[x + 1], wd[x + 1] = " ", 1
        ch[x], wd[x] = " ", 1

    def put_cells(self, x: int, y: int, cells: list[Cell]) -> None:
        if not 0 <= y < self.h:
            return
        for g, n, style in cells:
            if x >= self.w:
                break
            if x < 0 or x + n > self.w:  # partly outside: blanks
                for i in range(n):
                    if 0 <= x + i < self.w:
                        self._free(x + i, y)
                        self.st[y][x + i] = style
                x += n
                continue
            for i in range(n):
                self._free(x + i, y)
            self.ch[y][x], self.wd[y][x], self.st[y][x] = g, n, style
            if n == 2:
                self.ch[y][x + 1], self.wd[y][x + 1], self.st[y][x + 1] = "", 0, style
            x += n

    def put(self, x: int, y: int, s: str, style: str = "") -> None:
        self.put_cells(x, y, _cells([(s, style)]))

    def parts(self, x: int, y: int, parts: list[tuple[str, str]], width: int, align: str = "left") -> None:
        cells = _fit_cells(_cells(parts), width)
        used = sum(c[1] for c in cells)
        if align == "center":
            x += max(0, (width - used) // 2)
        elif align == "right":
            x += max(0, width - used)
        self.put_cells(x, y, cells)

    def text(self, x: int, y: int, s: str, width: int, style: str = "", align: str = "left") -> None:
        self.parts(x, y, [(s, style)], width, align)

    def rich(self, x: int, y: int, t: Text, width: int | None = None) -> None:
        s = t.plain
        styles = [str(t.style or "")] * len(s)
        for sp in t.spans:
            for i in range(sp.start, min(sp.end, len(s))):
                styles[i] = str(sp.style)
        runs: list[tuple[str, str]] = []
        for ch, st in zip(s, styles, strict=True):
            if runs and runs[-1][1] == st:
                runs[-1] = (runs[-1][0] + ch, st)
            else:
                runs.append((ch, st))
        self.parts(x, y, runs, self.w if width is None else width)

    def vline(self, x: int, y1: int, y2: int, style: str, ch: str = "│") -> list[tuple[int, int]]:
        cells = [(x, y) for y in range(y1, y2 + 1)]
        for cx, cy in cells:
            self.put(cx, cy, ch, style)
        return cells

    def box(self, x: int, y: int, w: int, h: int, style: str, heavy: bool = False, title: str = "") -> None:
        if w < 2 or h < 2:
            return
        tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if heavy else ("╭", "╮", "╰", "╯", "─", "│")
        self.put(x, y, tl + hz * (w - 2) + tr, style)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, style)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, vt, style)
            self.put(x + 1, yy, " " * (w - 2))
            self.put(x + w - 1, yy, vt, style)
        if title:
            self.text(x + 2, y, f" {title} ", w - 4, bold(style))

    def flow(self, path: list[tuple[int, int]], t: float, color: str, offset: int = 0, speed: float = 10.0) -> None:
        """A dot with a tail running along the path."""
        if not path:
            return
        n = len(path) + max(3, len(path) // 2)
        i = (int(t * speed) + offset) % n
        for k, (ch, st) in enumerate((("●", bold(color)), ("•", color), ("·", color))):
            j = i - k
            if 0 <= j < len(path):
                x, y = path[j]
                self.put(x, y, ch, st)

    def row(self, y: int) -> str:
        """The plain text of a row (for tests)."""
        return "".join(c for c, w in zip(self.ch[y], self.wd[y], strict=True) if w)

    def to_text(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            chars, styles, widths = self.ch[y], self.st[y], self.wd[y]
            run, style = [], None
            for x in range(self.w):
                if not widths[x]:
                    continue
                if styles[x] != style and run:
                    out.append("".join(run), style=style or None)
                    run = []
                style = styles[x]
                run.append(chars[x])
            if run:
                out.append("".join(run), style=style or None)
            if y < self.h - 1:
                out.append("\n")
        return out


def title_line(c: Canvas, w: int, name: str, name_color: str, state: str, state_color: str) -> None:
    """The first line of a diagram: WFWATCH, the name (cut if needed) and the state (never cut)."""
    fixed = len("WFWATCH") + 8 + cell_len(state)
    c.parts(
        0,
        0,
        [
            ("WFWATCH", bold(C.text)),
            ("    ", ""),
            (fit(name, max(8, w - fixed)), bold(name_color)),
            ("    ", ""),
            (state, bold(state_color)),
        ],
        w,
        "center",
    )


def hpath(x1: int, x2: int, y: int) -> list[tuple[int, int]]:
    step = 1 if x2 >= x1 else -1
    return [(x, y) for x in range(x1, x2 + step, step)]


# ---- layout of the fan ----


class Fan(NamedTuple):
    """One way to lay out the diagram, from the richest to the most compact."""

    prev: bool  # the block of the previous phase above
    conn: int  # rows of the link from it
    label: bool  # a caption over the fan
    box_h: int  # height of an agent card: 7 with stats, 6 with a sparkline, 5 or 4 compact
    nxt: bool  # the block of the next phase below
    back: int  # rows of the way back under the cards: 3 into the next block, 2 a closing line, 0 none


FANS = [
    Fan(True, 2, True, 7, True, 3),
    Fan(True, 1, True, 7, True, 3),
    Fan(True, 1, True, 6, True, 3),
    Fan(True, 1, False, 6, True, 3),
    Fan(False, 0, False, 6, True, 3),
    Fan(False, 0, False, 6, False, 2),
    Fan(False, 0, False, 5, False, 2),
    Fan(False, 0, False, 5, False, 0),
    Fan(False, 0, False, 4, False, 0),
]


@dataclass(frozen=True)
class Geo:
    """Agent cards of the fan: width, cards per row, rows; ``over`` – the last slot is a "+N" card."""

    bw: int
    per_row: int
    rows: int
    over: bool

    @property
    def slots(self) -> int:
        return self.per_row * self.rows


def per_row_max(cw: int, bw: int) -> int:
    return max(1, (cw + GAP) // (bw + GAP))


def geometry(n: int, cw: int, rows: int) -> Geo:
    if n <= rows * per_row_max(cw, CARD_W_MIN):
        k = max(1, math.ceil(n / rows))
        bw = next((b for b in range(CARD_W_MAX, CARD_W_MIN - 1, -1) if per_row_max(cw, b) >= k), CARD_W_MIN)
        return Geo(min(bw, cw), k, rows, False)
    return Geo(min(CARD_W_OVER, cw), per_row_max(cw, CARD_W_OVER), rows, True)


def fan_height(f: Fan, rows: int, has_agents: bool) -> int:
    if not has_agents:
        return 3
    return (3 if f.label else 1) + 2 + rows * f.box_h + (rows - 1) + f.back


@dataclass(frozen=True)
class Plan:
    fan: Fan
    geo: Geo
    height: int  # rows the diagram takes


def choose(n: int, cw: int, avail: int, side: bool, need, fans: list[Fan] = FANS) -> tuple[Fan, Geo] | None:
    """The richest layout that fits ``avail`` rows. With the phase column on the side, more rows of
    cards beat the blocks of the neighbouring phases (the column shows where we are)."""
    rmax = max(1, min(MAX_ROWS, math.ceil(n / per_row_max(cw, CARD_W_MIN)))) if n else 1
    full = [f for f in fans if f.box_h >= 6]
    compact = [f for f in fans if f.box_h < 6]
    rows = range(rmax, 0, -1)
    order = [(f, r) for r in rows for f in full] if side else [(f, r) for f in full for r in rows]
    for f, r in order + [(f, 1) for f in compact]:
        if need(f, r) <= avail:
            return f, geometry(n, cw, r)
    return None


def pick(agents: list[Agent], slots: int, over: bool, at: datetime) -> list[Agent | None]:
    """The agents to show (None – the "+N" card): running first, then failed, then the latest."""

    def by_start(a: Agent) -> datetime:
        return a.started or at

    if not over:
        return sorted(agents, key=by_start)
    rank = {RUNNING: 0, FAILED: 1, STALE: 1, DONE: 2}
    prio = sorted(agents, key=lambda a: (4 if a.superseded else rank.get(a.status, 3), -(a.last_ts or at).timestamp()))
    return sorted(prio[: max(0, slots - 1)], key=by_start) + [None]


# ---- the diagram, common part ----


class Diagram:
    """Shared drawing: the fan of agent cards and its links."""

    # message keys: the caption over the fan, the note when it is empty, the hint on its "+N" card
    FAN_LABEL = "fan.phase"
    EMPTY = "fan.phase_empty"
    MORE_HINT = "fan.phase_more"

    def __init__(self, colors: AgentColors, at: datetime, t: float, height: int | None = None, animate: bool = True):
        self.colors = colors
        self.at, self.t = at, t if animate else 0.0  # a still frame (--once): no flowing dots, spinners stand still
        self.height = height
        self.animate = animate

    def legend_statuses(self, agents: list[Agent]) -> list[tuple[str, str]]:
        out = [(C.blue, status_word(RUNNING)), (C.green, status_word(DONE)), (C.dim, status_word(PENDING))]
        for st in (STALE, FAILED):
            if any(a.status == st and not a.superseded for a in agents):
                out.append((status_color(st), status_word(st)))
        if any(a.superseded for a in agents):
            out.append((C.dim, f"{status_icon(SUPERSEDED)} {status_word(SUPERSEDED)}"))
        parts = []
        for col, name in out:
            parts += [("■ ", col), (name + "  ", C.text)]
        return parts

    def fan(
        self,
        c: Canvas,
        agents: list[Agent],
        x0: int,
        cw: int,
        cx: int,
        y: int,
        f: Fan,
        geo: Geo,
        live: bool,
        flows: list,
    ) -> int:
        """Cards of the agents fanned out under (cx, y); returns the row under the fan.

        ``live`` – the phase is running: its links are bright and carry flowing dots."""
        line = C.orange if live else C.dim
        if not agents:
            c.put(cx, y, "│", C.dim)
            c.text(x0, y + 1, tr(self.EMPTY), cw, C.dim, "center")
            c.put(cx, y + 2, "│", C.dim)
            return y + 3
        shown = pick(agents, geo.slots, geo.over, self.at)
        k = min(geo.per_row, len(shown))
        bw = geo.bw
        total = k * bw + (k - 1) * GAP
        bx0 = cx - total // 2  # the fan is centred on the trunk, so the middle card sits right under it
        cols = [bx0 + i * (bw + GAP) + bw // 2 for i in range(k)]
        trunk = [(cx, y)]
        c.put(cx, y, "│", line)
        if f.label:
            models = short_models(agents)
            c.parts(
                x0,
                y + 1,
                [(tr(self.FAN_LABEL), C.text), (" · " + ", ".join(models) if models else "", C.purple)],
                cw,
                "center",
            )
            c.put(cx, y + 2, "•", line)
            trunk.append((cx, y + 2))
            yb = y + 3
        else:
            yb = y + 1
        lo, hi = min([*cols, cx]), max([*cols, cx])
        for x in range(lo, hi + 1):
            c.put(x, yb, junction(x == cx, x in cols, x > lo, x < hi), line)
        for x, a in zip(cols, shown, strict=False):  # the first row of cards only
            c.put(x, yb + 1, "▼", status_color(agent_look(a)) if a else C.dim)
        by = yb + 2
        last_row: dict[int, int] = {}  # column -> top row of its last card
        for i, a in enumerate(shown):
            r, j = divmod(i, k)
            bx, ry = bx0 + j * (bw + GAP), by + r * (f.box_h + 1)
            last_row[j] = ry
            if a is None:
                self.more_box(c, agents, [s for s in shown if s], bx, ry, bw, f.box_h)
            else:
                self.agent_box(c, a, bx, ry, bw, f.box_h)
        bottom = by + geo.rows * f.box_h + (geo.rows - 1)
        if live and self.animate:
            for i, (x, a) in enumerate(zip(cols, shown, strict=False)):
                if a and a.status == RUNNING and not a.superseded:
                    flows.append((trunk + hpath(cx, x, yb) + [(x, yb + 1)], C.blue, i * 3))
        if not f.back:
            return bottom
        done = [a for a in shown if a and a.status == DONE]
        back = C.green if done and live else C.dim
        ends = {j: ry + f.box_h for j, ry in last_row.items()}  # first row under each column's last card
        for j, y1 in ends.items():
            c.vline(cols[j], y1, bottom, back)
        for x in range(lo, hi + 1):
            c.put(x, bottom + 1, junction(x in cols, x == cx and f.back > 2, x > lo, x < hi), back)
        if f.back > 2:
            c.put(cx, bottom + 2, "│", back)
        if live and self.animate:
            for i, a in enumerate(shown):
                j = i % k
                if a and a.status == DONE and last_row[j] == by + (i // k) * (f.box_h + 1):
                    path = [(cols[j], yy) for yy in range(ends[j], bottom + 1)] + hpath(cols[j], cx, bottom + 1)
                    flows.append((path + ([(cx, bottom + 2)] if f.back > 2 else []), C.green, i * 3))
        return bottom + f.back

    def agent_box(self, c: Canvas, a: Agent, x: int, y: int, w: int, h: int) -> None:
        look = agent_look(a)
        c.box(x, y, w, h, status_color(look))
        iw, ix = w - 4, x + 2
        c.text(ix, y + 1, a.label, iw, C.dim if a.superseded else bold(self.colors(a.label)), "center")
        model = short_model(a.model) or "…"
        st = agent_status(a, self.at, self.t, animate=self.animate)
        if st.cell_len + 1 + min(len(model), 9) > iw:  # the model must stay readable: it links the card to the legend
            st = agent_status(a, self.at, self.t, compact=True, animate=self.animate)
        sw = st.cell_len
        c.text(ix, y + 2, model, iw - sw - 1, C.dim if a.superseded else model_color(a.model))
        c.rich(ix + iw - sw, y + 2, st)
        row = y + 3
        if h >= 6:
            c.rich(ix, row, sparkline(a, self.at, iw))
            row += 1
        if h >= 7:
            c.text(ix, row, calls_stats(a, with_out=False), iw, C.dim)
            row += 1
        if h >= 5:
            c.text(ix, row, last_action(a) or "…", iw, C.text if a.status == RUNNING and not a.superseded else C.dim)

    def more_box(self, c: Canvas, agents: list[Agent], shown: list[Agent], x: int, y: int, w: int, h: int) -> None:
        ids = {id(a) for a in shown}
        rest = [a for a in agents if id(a) not in ids]
        c.box(x, y, w, h, C.dim)
        iw, ix = w - 4, x + 2
        c.text(ix, y + 1, tr("more.plus", n=len(rest)), iw, bold(C.text), "center")
        row = y + 2
        last = y + h - (3 if self.MORE_HINT and h >= 5 else 2)
        cnt = counts(rest)  # errors before what is done: the last lines are the ones cut
        lines = [(f"{status_icon(s)} {status_count(s, cnt[s])}", status_color(s)) for s in ATTENTION if cnt[s]]
        if n := sum(a.superseded for a in rest):
            lines.append((f"{status_icon(SUPERSEDED)} {status_count(SUPERSEDED, n)}", C.dim))
        for s, col in lines:
            if row > last:
                break
            c.text(ix, row, s, iw, col, "center")
            row += 1
        if self.MORE_HINT and h >= 5:
            c.text(ix, y + h - 2, tr(self.MORE_HINT, key="v"), iw, C.dim, "center")


# ---- a workflow run ----


@dataclass
class Item:
    """An item of the column on the left: the launch, a phase or the finish."""

    kind: str
    status: str
    phase: Phase | None = None
    n: int = 0


def timeline(run: Run) -> list[Item]:
    fin = {"completed": DONE, "failed": FAILED, "killed": STALE, "stopped": STALE}.get(run.state, PENDING)
    phases = run.phases()
    return (
        [Item("start", DONE)]
        + [Item("phase", phase_status(phases, i), p, i + 1) for i, p in enumerate(phases)]
        + [Item("finish", fin)]
    )


def phase_span(p: Phase, at: datetime) -> float:
    starts = [a.started for a in p.agents if a.started]
    if not starts:
        return 0.0
    ends = [at if a.status == RUNNING else (a.last_ts or at) for a in p.agents]
    return max(0.0, (max(ends) - min(starts)).total_seconds())


class GraphView(Diagram):
    """The diagram of one run (the painter gives the run, the selected phase and agent colors)."""

    def __init__(self, painter, at: datetime, t: float, height: int | None = None, animate: bool = True):
        super().__init__(painter.colors, at, t, height, animate)
        self.p = painter
        self.run: Run = painter.run
        self.live = self.run.state == "running"

    def __rich_console__(self, console, options):
        w = options.max_width
        h = options.height or self.height or 36
        plan = self.plan(w, h)
        if plan is None:  # does not fit: the painter normally switches to cards before this
            yield Text(fit(tr("diagram.no_fit", key="v"), w), style=C.dim)
            return
        yield self.draw(w, h, plan).to_text()

    # ---- geometry ----

    def selected(self, phases: list[Phase]) -> int:
        sel = self.p.phase_sel if self.p.phase_sel is not None else current_phase(phases)
        return min(max(sel, 0), len(phases) - 1)

    def plan(self, w: int, h: int) -> Plan | None:
        """How to draw on w×h, or None if the diagram does not fit (too narrow or too low)."""
        if w < MIN_W:
            return None
        phases = self.run.phases()
        if not phases:
            return Plan(FANS[-1], Geo(CARD_W_MIN, 1, 1, False), TOP + 2)
        n = len(phases[self.selected(phases)].agents)
        side = w >= SIDEBAR_MIN_W
        cw = w - (SIDE_W + 4 if side else 0)

        def need(f: Fan, rows: int) -> int:
            return (
                ((NODE_H + f.conn) if f.prev else 0) + NODE_H + fan_height(f, rows, bool(n)) + (NODE_H if f.nxt else 0)
            )

        got = choose(n, cw, h - TOP, side, need)
        if got is None:
            return None
        f, geo = got
        return Plan(f, geo, TOP + need(f, geo.rows))

    # ---- drawing ----

    def draw(self, w: int, h: int, plan: Plan | None = None) -> Canvas:
        plan = plan or self.plan(w, h) or Plan(FANS[-1], geometry(1, w, 1), h)
        c = Canvas(w, h)
        self.header(c, w)
        items = timeline(self.run)
        phases = [it.phase for it in items if it.kind == "phase"]
        if not phases:
            c.text(0, TOP + 1, tr("cards.no_agents"), w, C.dim, "center")
            return c
        sel = self.selected(phases)
        ci = sel + 1
        phase = phases[sel]
        f, geo = plan.fan, plan.geo

        side = w >= SIDEBAR_MIN_W
        x0 = SIDE_W + 4 if side else 0
        cw = w - x0
        cx = x0 + cw // 2
        node_w = min(cw - 4, 50)
        nx = cx - node_w // 2
        live_phase = self.live and phase.status == RUNNING

        flows: list[tuple[list, str, int]] = []
        anchors: dict[int, int] = {}
        y = TOP
        if f.prev:
            # the block above is the last phase that actually ran (a skipped one is not "previous")
            pi = next((j + 1 for j in range(sel - 1, -1, -1) if phases[j].agents), 0)
            self.node(c, items[pi], nx, y, node_w, current=False)
            anchors[pi] = y + 1
            y += NODE_H
            cells = c.vline(cx, y, y + f.conn - 1, C.orange if live_phase else C.dim)
            if live_phase and self.animate:
                flows.append((cells, C.orange, 0))
            y += f.conn
        self.node(c, items[ci], nx, y, node_w, current=True)
        anchors[ci] = y + 1
        y += NODE_H
        y = self.fan(c, phase.agents, x0, cw, cx, y, f, geo, live_phase, flows)
        if f.nxt and ci + 1 < len(items):
            self.node(c, items[ci + 1], nx, y, node_w, current=False)
            anchors[ci + 1] = y + 1
        if side:
            self.sidebar(c, items, ci, anchors, h, nx, flows)
        for path, color, off in flows:
            c.flow(path, self.t, color, off)
        return c

    def header(self, c: Canvas, w: int) -> None:
        r = self.run
        state = run_state(r)
        title_line(
            c,
            w,
            (r.name or r.run_id).upper(),
            C.orange,
            f"{state.text.upper()}  {run_duration(r, self.at)}",
            state.color,
        )
        legend = [
            (fit(r.project, 30), C.cyan),
            (" · ", C.dim),
            (fit(r.session_name, 30), C.purple),
            (" · " + r.run_id, C.dim),
            ("     ", ""),
        ]
        for m in short_models(list(r.agents.values())):
            legend += [("■ ", model_color(m)), (m + "   ", C.text)]
        legend += self.legend_statuses(list(r.agents.values()))
        c.parts(0, 1, legend, w, "center")
        c.put(0, 2, "─" * w, C.dim)

    def node(self, c: Canvas, it: Item, x: int, y: int, w: int, current: bool) -> None:
        color = C.purple if it.kind == "start" else phase_color(it.status)
        c.box(x, y, w, NODE_H, color, heavy=current)
        l1, l2 = self.node_text(it, color)
        c.parts(x + 2, y + 1, l1, w - 4, "center")
        c.parts(x + 2, y + 2, l2, w - 4, "center")

    def node_text(self, it: Item, color: str) -> tuple[list, list]:
        r = self.run
        if it.kind == "start":
            return (
                styled("node.session", C.dim, session=(r.session_name, bold(C.purple))),
                [(tr("node.launched"), C.text), (f" · {hhmm(r.started)}" if r.started else "", C.dim)],
            )
        if it.kind == "finish":
            if r.state == "running":
                return [("◇ " + tr("diagram.finish"), C.dim)], [(tr("node.result_pending"), C.dim)]
            state = run_state(r)
            return [(state.text, bold(state.color))], [(join(tr("node.result_done"), hhmm(r.ended)), C.dim)]
        p, st = it.phase, it.status
        l1 = [(f"{status_icon(st)} {it.n}. {phase_title(p)}", bold(color))]
        if st in (PENDING, SKIPPED):
            return l1, [(phase_note(st, r.state == "running"), C.dim)]
        l2 = status_counts(p.agents)
        l2 += [(" · " if l2 else "", C.dim), (fmt_dur(phase_span(p, self.at)), C.dim)]
        return l1, l2

    def sidebar(self, c: Canvas, items: list[Item], ci: int, anchors: dict[int, int], h: int, nx: int, flows) -> None:
        """All items from the launch to the finish over the whole height of the column; folded into
        "… N more" only when the rows really run out. The current item points at its block."""
        c.box(0, 3, SIDE_W, h - 3, C.dim, title=tr("diagram.phases"))
        lo, hi = 4, h - 2
        stats = self.stats()
        n = len(items)
        room = hi - lo + 1
        if room - len(stats) - 1 >= n:
            room -= len(stats) + 1
        else:
            stats = []
        rows: dict[int, int] = {}
        notes: list[tuple[int, str]] = []
        if n <= room:
            # the current item in line with its block if possible; items around it two rows apart
            # (with a dotted link between them), or one row apart where the rows are short
            bottom = lo + room - 1
            want = anchors.get(ci, lo + 2 * ci)
            y_cur = min(max(want, lo + ci), bottom - (n - 1 - ci))
            up = 2 if y_cur - lo >= 2 * ci else 1
            down = 2 if bottom - y_cur >= 2 * (n - 1 - ci) else 1
            rows = {i: y_cur + (i - ci) * (up if i < ci else down) for i in range(n)}
        else:
            a = b = ci

            def used(a: int, b: int) -> int:
                return (b - a + 1) + (a > 0) + (b < n - 1)

            grew = True
            while grew:
                grew = False
                for a2, b2 in ((a - 1, b), (a, b + 1)):
                    if a2 >= 0 and b2 < n and used(a2, b2) <= room:
                        a, b, grew = a2, b2, True
            y = lo
            if a > 0:
                notes.append((y, tr("more.dots", n=a)))
                y += 1
            for i in range(a, b + 1):
                rows[i] = y
                y += 1
            if b < n - 1:
                notes.append((y, tr("more.dots", n=n - 1 - b)))

        for y1, y2 in pairwise(sorted(rows.values())):
            for yy in range(y1 + 1, y2):
                c.put(2, yy, "┊", C.dim)
        for idx, yy in rows.items():
            self.side_item(c, items[idx], yy, idx == ci)
        for yy, text in notes:
            c.text(2, yy, text, SIDE_W - 4, C.dim)
        for j, (name, value) in enumerate(stats):
            yy = hi - len(stats) + 1 + j
            c.parts(2, yy, [(name, C.dim)], SIDE_W - 4)
            c.text(2 + cell_len(name) + 1, yy, value, SIDE_W - 5 - cell_len(name), C.text, "right")

        for idx, yn in sorted(anchors.items(), key=lambda kv: kv[0] == ci):  # the current one last, on top
            ys = rows.get(idx)
            if ys is None:
                continue
            current = idx == ci
            color = C.orange if current else C.dim
            if ys == yn:
                path = hpath(SIDE_W, nx - 2, yn)
            elif current:  # bend: along, down (or up) at the column edge, along again
                xm = SIDE_W + 1
                path = [(SIDE_W, ys)] + [(xm, yy) for yy in range(ys, yn, 1 if yn > ys else -1)] + hpath(xm, nx - 2, yn)
            else:
                continue
            for x, y in path:
                c.put(x, y, "┄", color)
            if ys != yn:
                xm = SIDE_W + 1
                down = yn > ys
                for yy in range(min(ys, yn) + 1, max(ys, yn)):
                    c.put(xm, yy, "┆", color)
                c.put(xm, ys, "╮" if down else "╯", color)
                c.put(xm, yn, "╰" if down else "╭", color)
            c.put(nx - 1, yn, "►", color)
            if current and self.live and items[idx].status == RUNNING and self.animate:
                flows.append((path, C.orange, 0))

    def side_item(self, c: Canvas, it: Item, y: int, current: bool) -> None:
        r = self.run
        if it.kind == "start":
            icon, color, title, right = "◇", C.purple, tr("diagram.launch"), hhmm(r.started)
        elif it.kind == "finish":
            icon = "◇" if it.status == PENDING else status_icon(it.status)
            color, title = status_color(it.status), tr("diagram.finish")
            right = hhmm(r.ended) if r.ended and r.state != "running" else ""
        else:
            icon, color = status_icon(it.status), phase_color(it.status)
            title = f"{it.n}. {phase_title(it.phase)}"
            right = fmt_dur(phase_span(it.phase, self.at)) if it.phase.agents else ""
        iw = SIDE_W - 4
        c.put(2, y, icon, bold(color))
        c.text(4, y, title, iw - 2 - cell_len(right) - 1, bold(color) if current else color)
        c.text(2, y, right, iw, C.text if current else C.dim, "right")
        if current:
            c.put(1, y, "▸", bold(color))

    def stats(self) -> list[tuple[str, str]]:
        ags = self.run.current_agents()
        return [
            (tr("stats.agents"), f"{sum(a.status == DONE for a in ags)}/{len(ags)}"),
            (tr("stats.calls"), str(sum(a.tool_calls for a in ags))),
            (tr("stats.out"), fmt_k(sum(a.output_tokens for a in ags))),
            (tr("stats.time"), run_duration(self.run, self.at)),
        ]


# ---- an ordinary session ----


class SessionLayout(NamedTuple):
    node_h: int  # the session block: 6 with a sparkline, 5 without
    bg_lines: int  # lines of the background commands block (0 – no block)


# a session has no neighbouring blocks; back=3 leads into the "results go back" block
SESSION_FANS = [
    Fan(False, 0, True, 7, False, 3),
    Fan(False, 0, False, 6, False, 3),
    Fan(False, 0, False, 6, False, 2),
    Fan(False, 0, False, 5, False, 2),
    Fan(False, 0, False, 5, False, 0),
    Fan(False, 0, False, 4, False, 0),
]


class SessionGraph(Diagram):
    """An ordinary session: its block, its background commands, its subagents fanned out,
    its latest actions on the left."""

    FAN_LABEL = "fan.session"
    EMPTY = "fan.session_empty"
    MORE_HINT = "fan.session_more"
    ACTIONS_W = 40
    BG_MAX = 4  # lines of background commands at most

    def __init__(self, painter, at: datetime, t: float, height: int | None = None, animate: bool = True):
        super().__init__(painter.colors, at, t, height, animate)
        self.p = painter
        self.s: SessionWatch = painter.s
        self.live = self.s.status == BUSY

    def __rich_console__(self, console, options):
        w = options.max_width
        h = options.height or self.height or 30
        plan = self.plan(w, h)
        if plan is None:
            yield Text(fit(tr("diagram.no_fit", key="v"), w), style=C.dim)
            return
        yield self.draw(w, h, plan).to_text()

    def bg_tasks(self) -> list[BgTask]:
        return self.s.recent_bg_tasks(self.at)

    def plan(self, w: int, h: int) -> tuple[SessionLayout, Fan, Geo, int] | None:
        """How to draw on w×h, or None if the diagram does not fit."""
        if w < MIN_W:
            return None
        side = w >= SIDEBAR_MIN_W
        cw = w - (self.ACTIONS_W + 4 if side else 0)
        subs = list(self.s.subagents.values())
        bg = len(self.bg_tasks())
        for lay in (
            SessionLayout(6, min(bg, self.BG_MAX)),
            SessionLayout(5, min(bg, self.BG_MAX)),
            SessionLayout(5, min(bg, 1)),
        ):

            def need(f: Fan, rows: int, lay: SessionLayout = lay) -> int:
                fan = fan_height(f, rows, True) + (3 if f.back == 3 else 0) if subs else 2
                return lay.node_h + (lay.bg_lines + 2 if lay.bg_lines else 0) + fan

            got = choose(len(subs), cw, h - TOP, side, need, SESSION_FANS)
            if got is not None:
                f, geo = got
                return lay, f, geo, TOP + need(f, geo.rows)
        return None

    def draw(self, w: int, h: int, plan=None) -> Canvas:
        plan = plan or self.plan(w, h) or (SessionLayout(5, 0), SESSION_FANS[-1], geometry(1, w, 1), h)
        lay, f, geo, _ = plan
        c, s = Canvas(w, h), self.s
        state = session_state(s)
        since = session_since(s, self.at)
        place = where(s.where)
        model = short_model(s.main.model)
        title_line(c, w, s.name.upper(), state.color, f"{state.text.upper()}  {since}", state.color)
        legend = [
            (fit(s.project, 30), C.cyan),
            (f" · {place}" if place else "", C.dim),
            (" · " + tr("diagram.session"), C.dim),
            ("     ", ""),
        ]
        if model:
            legend += [("■ ", model_color(model)), (model + "   ", C.text)]
        legend += self.legend_statuses(list(s.subagents.values()))
        c.parts(0, 1, legend, w, "center")
        c.put(0, 2, "─" * w, C.dim)

        side = w >= SIDEBAR_MIN_W
        x0 = self.ACTIONS_W + 4 if side else 0
        cw = w - x0
        cx = x0 + cw // 2
        node_w = min(cw - 4, 60)
        nx = cx - node_w // 2
        y = TOP
        c.box(nx, y, node_w, lay.node_h, state.color, heavy=True)
        iw, ix = node_w - 4, nx + 2
        st = session_state_text(s, self.at, self.t, self.animate)
        c.parts(
            ix,
            y + 1,
            styled("node.session", C.dim, session=(f"{SESSION_TAG} {s.name}", bold(state.color))),
            iw - st.cell_len - 1,
        )
        c.rich(ix + iw - st.cell_len, y + 1, st)
        c.parts(
            ix,
            y + 2,
            [
                (s.project, C.cyan),
                (f" · {place}" if place else "", C.dim),
                (f" · {model}" if model else "", model_color(model)),
            ],
            iw,
        )
        row = y + 3
        if lay.node_h >= 6:
            c.rich(ix, row, sparkline(s.main, self.at, iw))
            row += 1
        c.text(ix, row, "→ " + (action_text(s.main.last_action) or "…"), iw, C.text)
        anchor = y + 1
        y += lay.node_h
        if lay.bg_lines:
            self.bg_block(c, nx, y, node_w, lay.bg_lines)
            y += lay.bg_lines + 2

        flows: list[tuple[list, str, int]] = []
        subs = list(s.subagents.values())
        if subs:
            live = bool(s.running_subagents())
            y = self.fan(c, subs, x0, cw, cx, y, f, geo, live, flows)
            if f.back == 3 and y + 3 <= h:
                c.box(nx, y, node_w, 3, C.dim)
                c.text(ix, y + 1, tr("session.back"), iw, C.dim, "center")
        else:
            c.put(cx, y, "┊", C.dim)
            note = tr("session.alone" if s.status == BUSY else self.EMPTY)
            c.text(x0, y + 1, note, cw, C.dim, "center")
        if side:
            self.actions(c, h, nx, anchor, flows)
        for path, col, off in flows:
            c.flow(path, self.t, col, off)
        return c

    def bg_block(self, c: Canvas, x: int, y: int, w: int, lines: int) -> None:
        """Background commands: running ones (what runs now, last output line) and recently finished."""
        tasks = self.bg_tasks()
        running = [t for t in tasks if t.status == RUNNING]
        finished = sorted((t for t in tasks if t.status != RUNNING), key=lambda t: t.ended or self.at, reverse=True)
        c.box(x, y, w, lines + 2, C.cyan if running else C.dim, title=tr("bg.title"))
        iw, ix = w - 4, x + 2
        shown = (running + finished)[:lines]
        hidden = len(running) + len(finished) - len(shown)
        for i, task in enumerate(shown):
            if i == len(shown) - 1 and hidden:
                c.text(ix, y + 1 + i, tr("more.dots", n=hidden + 1), iw, C.dim)
                break
            if task.status == RUNNING:
                parts = bg_parts(self.s, task, self.at, with_proc=i == 0)
                icon = live_icon(self.t, self.animate)
                parts = [(f"{icon} ", bold(C.cyan))] + parts[1:]
            else:
                parts = bg_done_parts(task, self.at)
            c.parts(ix, y + 1 + i, parts, iw)

    def actions(self, c: Canvas, h: int, nx: int, anchor: int, flows) -> None:
        """The session's own latest actions, newest first; the newest is level with the session block."""
        sw = self.ACTIONS_W
        c.box(0, 3, sw, h - 3, C.dim, title=tr("session.actions"))
        mine = [e for e in self.s.events if e.who == self.s.main.label][::-1]
        c.text(2, anchor - 1, tr("session.newest"), sw - 4, C.dim)
        for i, e in enumerate(mine[: max(0, h - 2 - anchor)]):
            y = anchor + i
            first = i == 0
            c.text(2, y, e.ts.astimezone().strftime("%H:%M:%S"), 8, C.text if first else C.dim)
            c.text(11, y, event_text(e), sw - 13, bold(C.blue) if first else C.dim)
            if first:
                c.put(1, y, "▸", bold(C.blue))
        if not mine:
            c.text(2, anchor, tr("session.nothing"), sw - 4, C.dim)
        arrow = hpath(sw, nx - 2, anchor)
        for x, y in arrow:
            c.put(x, y, "┄", C.blue)
        c.put(nx - 1, anchor, "►", C.blue)
        if self.live and self.animate:
            flows.append((arrow, C.blue, 0))
