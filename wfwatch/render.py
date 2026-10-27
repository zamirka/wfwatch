"""The screens: overview of everything, a run (diagram or cards), a session; plus the key bar,
the tab line and the log. Each screen is built at render time, when the real size is known."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from rich import box
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from . import model as _model
from .graph import MIN_W, GraphView, SessionGraph, pick
from .i18n import plural
from .i18n import t as tr
from .model import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    STALE,
    Agent,
    Event,
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
)
from .widgets import (
    SESSION_COUNTS,
    SESSION_TAG,
    Blank,
    Fit,
    Sized,
    action_text,
    agent_look,
    agent_status,
    ago,
    bg_done_parts,
    bg_parts,
    calls_stats,
    counts,
    event_text,
    fmt_dur,
    fmt_k,
    join,
    last_action,
    live_icon,
    model_color,
    more_hidden,
    phase_chain,
    phase_note,
    phase_status,
    phase_title,
    progress,
    run_duration,
    run_state,
    running_of,
    session_icon,
    session_since,
    session_state,
    session_state_text,
    sparkline,
    status_counts,
    status_line,
    text_of,
    where,
)

CARD_W = 38
CARD_W_TIGHT = 34  # a card may get this narrow so that one more fits in a row (two on 80 columns)
CARD_H = 6  # a card: frame and four lines
LOG_MIN, LOG_MAX = 3, 12
LOG_MARK = {"start": "▶ ", "done": "✓ ", "failed": "✗ ", "run": "◆ ", "tool": "→ "}


def now() -> datetime:
    return _model.now()  # looked up on every call, so tests can freeze the clock


def log_style(kind: str) -> str:
    return {"start": C.blue, "done": C.green, "failed": C.red, "run": bold(C.orange)}.get(kind, C.text)


def log_size(room: int, events: int) -> int:
    """Rows of the log: what is left on the screen, but no more than there are events (and LOG_MAX)."""
    return max(LOG_MIN, min(room, events, LOG_MAX))


def log_panel(items: list[tuple[Event, Text | None, str]], rows: int) -> Panel:
    """items: (event, tag of its run or None, author color)."""
    g = Table.grid(padding=(0, 2), expand=True)
    g.add_column(no_wrap=True, style=C.dim, min_width=8)
    tagged = any(tag is not None for _, tag, _ in items)
    if tagged:
        g.add_column(no_wrap=True, min_width=2)
    g.add_column(no_wrap=True, min_width=24, max_width=24, overflow="ellipsis")
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for e, tag, color in items[-rows:] if rows > 0 else []:
        cells: list = [e.ts.astimezone().strftime("%H:%M:%S")]
        if tagged:
            cells.append(tag or Text(""))
        cells += [
            Text(e.who or tr("log.workflow"), style=color, no_wrap=True, overflow="ellipsis"),
            Text(LOG_MARK.get(e.kind, "") + event_text(e), style=log_style(e.kind), no_wrap=True, overflow="ellipsis"),
        ]
        g.add_row(*cells)
    return Panel(
        g,
        title=Text(f" {tr('log.title')} ", style=C.dim),
        title_align="left",
        border_style=C.dim,
        box=box.ROUNDED,
        height=rows + 2,
    )


# ---- key bar and tabs ----


class KeyBar:
    """The key line at the bottom (as in htop or mc): only the keys that work on this screen.
    When it does not fit, labels go first, then the least important keys; "q quit" always stays."""

    def __init__(self, items: list[tuple[str, str, int]]):
        self.items = items  # (key, label, importance: 0 – always shown, larger – dropped earlier)

    def _build(self, items: list[tuple[str, str]]) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        for key, label in items:
            t.append(f" {key} ", style=C.key)
            if label:
                t.append(f" {label} ", style=C.label)
            t.append(" ")
        return t

    def variants(self) -> Iterable[Text]:
        items = [[k, label] for k, label, _ in self.items]
        yield self._build([(k, label) for k, label in items])
        order = sorted((i for i, it in enumerate(self.items) if it[2] > 0), key=lambda i: -self.items[i][2])
        for i in order:
            items[i][1] = ""
            yield self._build([(k, label) for k, label in items])
        keep = set(range(len(items)))
        for i in order:
            keep.discard(i)
            yield self._build([(k, label) for j, (k, label) in enumerate(items) if j in keep])
        yield self._build([(k, "") for j, (k, _) in enumerate(items) if j in keep])  # the keys alone

    def fit(self, width: int) -> Text:
        t = Text()
        for t in self.variants():
            if t.cell_len <= width:
                return t
        return t

    @property
    def plain(self) -> str:
        return self._build([(k, label) for k, label, _ in self.items]).plain

    def __rich_console__(self, console, options):
        yield self.fit(options.max_width)


def keybar(mode: str, multi: bool, expand_all: bool, view: str = "graph", manual: bool = False) -> KeyBar:
    """The keys of a screen: overview, a run ("detail") or a session."""
    nav = []
    if multi or mode == "overview":
        nav = [("1–9", tr("key.open" if mode == "overview" else "key.goto"), 4), ("Tab", tr("key.next"), 5)]
    if mode == "overview":
        return KeyBar([*nav, ("q", tr("key.quit"), 0)])
    items = [*nav, ("o", tr("key.overview"), 1)]
    if mode == "detail":
        items.append(("↑↓", tr("key.phase"), 2))
    if view == "cards":
        items.append(("PgUp PgDn", tr("key.scroll"), 3))
    if manual:
        items.append(("f", tr("key.follow"), 2))
    items.append(("v", tr("key.diagram" if view == "cards" else "key.cards"), 1))
    if mode == "detail" and view == "cards":
        items.append(("a", tr("key.collapse" if expand_all else "key.expand"), 3))
    items.append(("q", tr("key.quit"), 0))
    return KeyBar(items)


@dataclass
class Tab:
    full: str
    short: str
    style: str
    selected: bool


class TabsLine:
    """Tabs over a detailed view; the selected one is always visible: when they do not fit, the
    others shrink to number and icon, then only a window around the selected one is shown."""

    def __init__(self, tabs: list[Tab]):
        self.tabs = tabs

    @property
    def plain(self) -> str:
        return self._build(range(len(self.tabs)), full=True).plain

    def _build(self, idx: Iterable[int], full: bool, left: bool = False, right: bool = False) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        if left:
            t.append("‹ ", style=C.dim)
        for i in idx:
            tab = self.tabs[i]
            t.append(tab.full if full or tab.selected else tab.short, style=tab.style)
            t.append(" ")
        if right:
            t.append("›", style=C.dim)
        return t

    def fit(self, width: int) -> Text:
        n = len(self.tabs)
        for full in (True, False):
            t = self._build(range(n), full)
            if t.cell_len <= width:
                return t
        sel = next((i for i, tab in enumerate(self.tabs) if tab.selected), 0)
        a = b = sel

        def size(a: int, b: int) -> int:
            return self._build(range(a, b + 1), False, a > 0, b < n - 1).cell_len

        grew = True
        while grew:
            grew = False
            for a2, b2 in ((a, b + 1), (a - 1, b)):
                if a2 >= 0 and b2 < n and size(a2, b2) <= width:
                    a, b, grew = a2, b2, True
        return self._build(range(a, b + 1), False, a > 0, b < n - 1)

    def __rich_console__(self, console, options):
        yield self.fit(options.max_width)


def tabs_line(runs: list[Run], selected, sessions: list[SessionWatch] | None = None) -> TabsLine:
    """Tabs over a detailed view: workflows, then sessions; numbers as in the overview."""
    tabs = []
    for n, r in enumerate(runs, 1):
        st = run_state(r)
        running = sum(a.status == RUNNING for a in r.current_agents())
        mark = f"●{running}" if r.state == "running" else st.icon
        sel = r.run_dir == selected
        tabs.append(
            Tab(
                f" {n} {r.name or r.run_id} {mark} ",
                f" {n}{st.icon} ",
                f"bold reverse {st.color}" if sel else st.color,
                sel,
            )
        )
    for n, sw in enumerate(sessions or [], len(runs) + 1):
        st = session_state(sw)
        sel = sw.session_id == selected
        tabs.append(
            Tab(
                f" {n} {SESSION_TAG} {sw.name} {st.icon} ",
                f" {n}{SESSION_TAG} ",
                f"bold reverse {st.color}" if sel else st.color,
                sel,
            )
        )
    return TabsLine(tabs)


def run_tag(n: int, r: Run) -> Text:
    return Text(f"{n}", style=bold(run_state(r).color))


# ---- cards with scrolling ----


@dataclass
class Section:
    body: RenderableType
    lead: RenderableType | None = None  # a link from the section above (drawn only under another section)


class CardsView:
    """Sections (phases, rows of cards) in a window of the available height: anchored to the
    current section of the owner, shifted by its ``scroll`` (PgUp/PgDn), with "↑ more / ↓ more"."""

    def __init__(self, owner, at: datetime, t: float, animate: bool = True):
        self.owner = owner  # .sections(width, height, at, t, animate) -> (sections, anchor); .scroll; .page
        self.at, self.t, self.animate = at, t, animate

    def __rich_console__(self, console, options):
        w, h = options.max_width, options.height
        sections, anchor = self.owner.sections(w, h, self.at, self.t, self.animate)
        opts = options.update(height=None)
        bodies = [console.render_lines(s.body, opts, pad=True) for s in sections]
        leads = [console.render_lines(s.lead, opts, pad=True) if s.lead is not None else [] for s in sections]
        n = len(sections)
        if h is None:  # natural height (--once): everything
            for i in range(n):
                for line in (leads[i] if i else []) + bodies[i]:
                    yield from line
                    yield Segment.line()
            return

        def fill(start: int) -> int:
            used, end = (1 if start > 0 else 0), start
            while end < n:
                hh = len(bodies[end]) + (len(leads[end]) if end > start else 0)
                if end > start and used + hh + (1 if end + 1 < n else 0) > h:
                    break
                used += hh
                end += 1
            return end

        anchor = min(max(anchor, 0), n - 1) if n else 0
        auto = anchor
        while auto > 0 and fill(auto - 1) > anchor:
            auto -= 1
        start = min(max(auto + self.owner.scroll, 0), max(n - 1, 0))
        end = fill(start)
        while start > 0 and end == n and fill(start - 1) == n:
            start -= 1
        self.owner.scroll, self.owner.page = start - auto, max(1, end - start - 1)

        lines: list[list[Segment]] = []
        if start > 0:
            lines += console.render_lines(Text("  " + tr("more.up", n=start, key="PgUp"), style=C.dim), opts, pad=True)
        for i in range(start, end):
            lines += (leads[i] if i > start else []) + bodies[i]
        more = n - end
        if more:
            down = Text("  " + tr("more.down", n=more, key="PgDn"), style=C.dim)
            lines = lines[: h - 1] + console.render_lines(down, opts, pad=True)
        lines = lines[:h]
        blank = [Segment(" " * w)]
        lines += [blank] * (h - len(lines))
        for line in lines:
            yield from line
            yield Segment.line()


def card(a: Agent, colors: AgentColors, at: datetime, t: float, width: int = CARD_W, animate: bool = True) -> Panel:
    """An agent card of the cards view: model and status, sparkline, numbers, last action."""
    look = agent_look(a)
    top = Table.grid(expand=True)
    top.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    top.add_column(justify="right", no_wrap=True)
    top.add_row(
        Text(short_model(a.model) or "…", style=C.dim if a.superseded else model_color(a.model)),
        agent_status(a, at, t, compact=width < 30, animate=animate),
    )
    stats = Text(calls_stats(a), style=C.dim, no_wrap=True, overflow="ellipsis")
    last = last_action(a)
    body = Group(
        top,
        sparkline(a, at, width - 4),
        stats,
        Text(
            last or "…",
            style=C.text if a.status == RUNNING and not a.superseded else C.dim,
            no_wrap=True,
            overflow="ellipsis",
        ),
    )
    label = Text(a.label, style=C.dim if a.superseded else bold(colors(a.label)), no_wrap=True, overflow="ellipsis")
    return Panel(
        body,
        title=label,
        title_align="left",
        border_style=status_color(look),
        width=width,
        height=CARD_H,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def card_grid(cards: list[RenderableType], per_row: int, width: int) -> Table:
    g = Table.grid(padding=(0, 1))
    for _ in range(per_row):
        g.add_column(width=width, no_wrap=True)
    for i in range(0, len(cards), per_row):
        row = cards[i : i + per_row]
        g.add_row(*row, *([""] * (per_row - len(row))))
    return g


def card_layout(w: int) -> tuple[int, int]:
    """Card width and cards per row inside a panel of width w: full-width cards, or slightly
    narrower ones when that makes one more fit in a row."""
    inner = max(1, w - 4)
    if inner < CARD_W:
        return max(20, inner), 1
    n = (inner + 1) // (CARD_W + 1)
    tight = (inner + 1) // (n + 1) - 1
    return (tight, n + 1) if tight >= CARD_W_TIGHT else (CARD_W, n)


# ---- a workflow run ----


class Painter:
    """The detailed view of one run: the diagram or cards by phase, the log, the status line."""

    def __init__(self, run: Run, expand_all: bool = False, view: str = "graph", colors: AgentColors | None = None):
        self.run = run
        self.expand_all = expand_all
        self.view = view  # graph – the diagram, cards – cards by phase
        self.phase_sel: int | None = None  # None – follow the current phase
        self.scroll = 0  # cards: sections to shift from the automatic position (PgUp/PgDn)
        self.page = 1  # cards: sections on the last page shown
        self.colors = colors if colors is not None else AgentColors()

    def color(self, label: str) -> str:
        return self.colors(label)

    def anchor(self, phases: list[Phase]) -> int:
        if not phases:
            return 0
        sel = self.phase_sel if self.phase_sel is not None else current_phase(phases)
        return min(max(sel, 0), len(phases) - 1)

    # ---- parts ----

    def header(self, at: datetime) -> RenderableType:
        r = self.run
        state = run_state(r)
        g = Table.grid(expand=True, padding=(0, 2))
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(
            Text.assemble(("WFWATCH  ", bold(C.orange)), (r.name or r.run_id, "bold")),
            Text.assemble((state.text, bold(state.color)), ("  " + run_duration(r, at), "bold")),
        )
        g.add_row(
            Text.assemble(
                (r.project, C.cyan),
                (" · ", C.dim),
                (r.session_name, C.purple),
                (" · " + join(r.run_id, r.description), C.dim),
                no_wrap=True,
                overflow="ellipsis",
            ),
            "",
        )
        return g

    def phase_line(self, i: int, p: Phase, st: str, at: datetime) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(
            f"{status_icon(st)} {i}. {phase_title(p)}",
            style=C.dim if st in (PENDING, SKIPPED) else bold(phase_color(st)),
        )
        if not p.agents:
            t.append(f"  · {phase_note(st, self.run.state == 'running')}", style=C.dim)
        for a in p.agents:
            t.append("   ")
            t.append(a.label, style=C.dim if a.superseded else self.color(a.label))
            t.append(f" {status_icon(agent_look(a))}{fmt_dur(a.elapsed(at))}", style=C.dim)
        return t

    def phase_panel(
        self, i: int, p: Phase, st: str, at: datetime, t: float, w: int, lines: int | None, animate: bool = True
    ) -> Panel:
        """A phase with its agents as cards; ``lines`` – the most rows it may take inside its frame.

        When not every card fits, the most important agents (running, failed, then the latest) get
        as many rows of cards as leave room for the others one line each; when even that does not
        fit, every agent gets a line (and "+N" says how many did not fit)."""
        cw, per_row = card_layout(w)
        agents, n = p.agents, len(p.agents)
        k = n  # agents shown as cards
        if lines is not None and -(-n // per_row) * CARD_H > lines:
            k = 0
            for rows in range(lines // CARD_H, 0, -1):
                if n - rows * per_row <= lines - rows * CARD_H:
                    k = rows * per_row
                    break
        carded = pick(agents, n, False, at) if k >= n else pick(agents, k + 1, True, at)[:-1] if k else []
        parts: list[RenderableType] = []
        if carded:
            parts.append(card_grid([card(a, self.colors, at, t, cw, animate) for a in carded], per_row, cw))
        shown = {id(a) for a in carded}
        rest = [a for a in agents if id(a) not in shown]
        if rest:
            room = (lines if lines is not None else len(rest)) - -(-len(carded) // per_row) * CARD_H
            parts.append(self.agent_list(rest, at, t, max(1, room), animate))
        body: RenderableType = parts[0] if len(parts) == 1 else Group(*parts)
        cnt = counts(p.agents)
        color = phase_color(st)
        return Panel(
            body,
            title=Text(f" {i}. {phase_title(p)} ", style=bold(color)),
            title_align="left",
            subtitle=Text(f" {progress(cnt[DONE], len(p.current))} ", style=C.dim),
            subtitle_align="right",
            border_style=color,
            box=box.HEAVY if st == RUNNING else box.ROUNDED,
        )

    def agent_list(
        self, agents: list[Agent], at: datetime, t: float, rows: int, animate: bool = True
    ) -> RenderableType:
        """Agents one line each (no room for their cards), in start order. When they do not fit, the
        most important ones (running, failed, the latest) are shown and "+N" counts the others."""
        over = len(agents) > rows
        shown = [a for a in pick(agents, rows if over else len(agents), over, at) if a is not None]
        g = Table.grid(padding=(0, 2), expand=True)
        g.add_column(no_wrap=True)
        g.add_column(no_wrap=True, max_width=24, overflow="ellipsis")
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for a in shown:
            g.add_row(
                agent_status(a, at, t, animate=animate),
                Text(a.label, style=C.dim if a.superseded else self.color(a.label)),
                Text(action_text(a.last_action) or "…", style=C.dim),
            )
        if len(shown) == len(agents):
            return g
        ids = {id(a) for a in shown}
        hidden = [a for a in agents if id(a) not in ids]
        cnt = counts(hidden)
        parts = [(status_count(s, cnt[s]), status_color(s)) for s in ATTENTION if cnt[s]]  # errors before done
        if n := sum(a.superseded for a in hidden):
            parts.append((status_count(SUPERSEDED, n), C.dim))
        more = Text(tr("more.plus", n=len(hidden)), style=C.dim, no_wrap=True, overflow="ellipsis")
        for text, style in parts:
            more.append(" · ", style=C.dim)
            more.append(text, style=style)
        return Group(g, more)

    def connector(self, active: bool, t: float, animate: bool) -> Text:
        if active and animate:
            frames = ["  │", "  ╽", "  ●", "  ╿"]
            return Text(frames[int(t * 4) % len(frames)], style=C.orange)
        return Text("  │", style=C.orange if active else C.dim)

    def sections(self, w: int, h: int | None, at: datetime, t: float, animate: bool) -> tuple[list[Section], int]:
        """Phases for the cards view: the running ones, failed ones and the selected one as panels
        of cards (no taller than the screen: the rest goes to a "+N" card), the others one line each."""
        phases = self.run.phases()
        if not phases:
            return [Section(Text("  " + tr("cards.no_agents"), style=C.dim))], 0
        anchor = self.anchor(phases)
        lines = None if h is None else max(1, h - 2 - 2 - 1)  # frame, "more" lines, link
        out = []
        for i, p in enumerate(phases):
            st = phase_status(phases, i)
            expanded = bool(p.agents) and (self.expand_all or st in (RUNNING, STALE, FAILED) or i == anchor)
            body = (
                self.phase_panel(i + 1, p, st, at, t, w, lines, animate)
                if expanded
                else self.phase_line(i + 1, p, st, at)
            )
            out.append(Section(body, self.connector(st == RUNNING and expanded, t, animate) if i else None))
        return out, anchor

    def log_items(self, rows: int, tag: Text | None = None) -> list[tuple[Event, Text | None, str]]:
        return [(e, tag, self.color(e.who)) for e in self.run.events[-rows:]]

    def footer(self) -> Fit:
        """The status line: label [value]. Progress, errors and the phase come first; when the line
        does not fit, the running models fold into one item, then output, calls and models go."""
        ags = self.run.current_agents()
        cnt = counts(ags)
        phases = self.run.phases()
        cur = current_phase(phases) + 1 if any(p.agents for p in phases) else 0
        busy: dict[str, int] = {}
        for a in ags:
            if a.status == RUNNING:
                m = short_model(a.model) or tr("footer.agent")
                busy[m] = busy.get(m, 0) + 1
        head = [(tr("footer.agents"), progress(cnt[DONE], len(ags)), C.green)]
        if cnt[FAILED]:
            head.append((tr("footer.failed"), str(cnt[FAILED]), C.red))
        if cnt[STALE]:
            head.append((tr("footer.stale"), str(cnt[STALE]), C.yellow))
        head.append((tr("footer.phase"), f"{cur}/{len(phases)}", C.orange))
        models = [(m, status_count(RUNNING, k), model_color(m)) for m, k in sorted(busy.items())]
        if len(models) > 1:
            folded = [(plural("footer.models", len(models)), status_count(RUNNING, sum(busy.values())), C.blue)]
        else:
            folded = models
        calls = (tr("footer.calls"), str(sum(a.tool_calls for a in ags)), C.text)
        out = (tr("footer.out"), fmt_k(sum(a.output_tokens for a in ags)), C.text)
        return status_line(
            [*head, *models, calls, out], [*head, *folded, calls, out], [*head, *folded, calls], [*head, *folded], head
        )

    # ---- whole screen ----

    def keys(self, multi: bool = False) -> KeyBar:
        return keybar("detail", multi, self.expand_all, self.view, self.phase_sel is not None)

    def _parts(self, w: int, h: int, tabs, keys, animate: bool) -> list[tuple[RenderableType, int | None]]:
        at, t = now(), time.monotonic()
        fixed = 2 + (1 if tabs is not None else 0)
        if self.view == "graph" and w >= MIN_W:
            gv = GraphView(self, at, t, animate=animate)
            plan = gv.plan(w, h - fixed - LOG_MIN - 2)
            if plan is not None:
                log_rows = log_size(h - fixed - plan.height - 2, len(self.run.events))
                return [
                    *([(tabs, 1)] if tabs is not None else []),
                    (gv, None),
                    (log_panel(self.log_items(log_rows), log_rows), log_rows + 2),
                    (self.footer(), 1),
                    (keys, 1),
                ]
        # cards: chosen, or the diagram does not fit
        log_rows = max(LOG_MIN, min(8, h // 6))
        if h - 6 - (log_rows + 2) < 6:  # a tiny terminal: the cards matter more than the log
            log_rows = 0
        parts: list[tuple[RenderableType, int | None]] = [
            (self.header(at), 2),
            (tabs if tabs is not None else Blank(), 1),
            (CardsView(self, at, t, animate), None),
        ]
        if log_rows:
            parts.append((log_panel(self.log_items(log_rows), log_rows), log_rows + 2))
        return [*parts, (self.footer(), 1), (keys, 1)]

    def screen(
        self,
        height: int,
        tabs: RenderableType | None = None,
        keys: RenderableType | None = None,
        animate: bool = True,
    ) -> RenderableType:
        """The full-screen frame; ``animate`` False – a still one (no spinners, no flowing dots)."""
        keys = keys if keys is not None else self.keys()
        return Sized(lambda w, h: layout(self._parts(w, h, tabs, keys, animate)), height)

    def snapshot(self, log_rows: int = 12, tabs: RenderableType | None = None, height: int = 36) -> RenderableType:
        """A still frame for --once: natural height, no flowing dots, no key bar."""

        def build(w: int, _h) -> RenderableType:
            at, t = now(), 0.0  # a still frame: spinners stand still
            log = log_panel(self.log_items(log_rows), log_rows)
            if self.view == "graph" and w >= MIN_W:
                gv = GraphView(self, at, t, height, animate=False)
                if gv.plan(w, height) is not None:
                    return Group(*([tabs] if tabs is not None else []), gv, log, self.footer())
            return Group(
                self.header(at),
                tabs if tabs is not None else Text(""),
                CardsView(self, at, t, False),
                log,
                self.footer(),
            )

        return Sized(build, fill=False)


def layout(parts: list[tuple[RenderableType, int | None]]) -> Layout:
    lay = Layout()
    lay.split_column(*(Layout(r, size=size) if size is not None else Layout(r, ratio=1) for r, size in parts))
    return lay


# ---- a session ----


class SessionPainter:
    """The detailed view of an ordinary session: its block, background commands, subagents, latest actions."""

    def __init__(self, s: SessionWatch, colors: AgentColors | None = None, view: str = "graph"):
        self.s = s
        self.view = view
        self.scroll = 0
        self.page = 1
        self.colors = colors if colors is not None else AgentColors()

    def color(self, label: str) -> str:
        return self.colors(label)

    def log_items(self, rows: int) -> list[tuple[Event, Text | None, str]]:
        return [(e, None, self.color(e.who)) for e in self.s.events[-rows:]]

    def footer(self) -> Fit:
        """The status line; when it does not fit, the time of the last action goes, then the context."""
        s = self.s
        state = session_state(s)
        subs = list(s.subagents.values())
        head = [
            (short_model(s.main.model) or tr("footer.session"), state.word, state.color),
            (tr("footer.subagents"), running_of(len(s.running_subagents()), len(subs)), C.blue),
        ]
        if running := s.running_bg_tasks():
            head.append((tr("footer.bg"), status_count(RUNNING, len(running)), C.cyan))
        ctx = (tr("footer.ctx"), fmt_k(s.main.context_tokens), C.text)
        last = [(tr("footer.last_action"), ago(s.main.idle(now())), C.text)] if s.main.last_ts else []
        return status_line([*head, ctx, *last], [*head, ctx], head)

    def header(self, at: datetime) -> RenderableType:
        s = self.s
        state = session_state(s)
        g = Table.grid(expand=True, padding=(0, 2))
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(
            Text.assemble(("WFWATCH  ", bold(C.orange)), (f"{SESSION_TAG} {s.name}", bold(state.color))),
            Text.assemble((state.text, bold(state.color)), ("  " + session_since(s, at), "bold")),
        )
        model = short_model(s.main.model)
        g.add_row(
            Text.assemble(
                (s.project, C.cyan),
                (" · " + join(where(s.where), model, "→ " + (action_text(s.main.last_action) or "…")), C.dim),
                no_wrap=True,
                overflow="ellipsis",
            ),
            "",
        )
        return g

    def sections(self, w: int, h: int | None, at: datetime, t: float, animate: bool) -> tuple[list[Section], int]:
        """Cards view of a session: background commands, then its subagents a row of cards at a time
        (running first), so every one of them can be scrolled to."""
        s = self.s
        out: list[Section] = []
        tasks = s.recent_bg_tasks(at)
        if tasks:
            lines = [
                text_of(bg_parts(s, task, at, with_proc=i == 0))
                if task.status == RUNNING
                else text_of(bg_done_parts(task, at))
                for i, task in enumerate(tasks)
            ]
            out.append(
                Section(
                    Panel(
                        Group(*lines),
                        title=Text(f" {tr('bg.title')} ", style=bold(C.cyan)),
                        title_align="left",
                        border_style=C.cyan if s.running_bg_tasks() else C.dim,
                        box=box.ROUNDED,
                    )
                )
            )
        subs = sorted(s.subagents.values(), key=lambda a: (a.status != RUNNING, -(a.last_ts or at).timestamp()))
        title = Text(
            tr("session.subagents", progress=running_of(len(s.running_subagents()), len(subs))), style=bold(C.text)
        )
        if not subs:
            out.append(Section(Text(tr("session.no_subagents"), style=C.dim)))
            return out, 0
        cw, per_row = card_layout(w + 4)  # rows of cards without a surrounding panel
        for i in range(0, len(subs), per_row):
            row = card_grid([card(a, self.colors, at, t, cw, animate) for a in subs[i : i + per_row]], per_row, cw)
            out.append(Section(Group(title, row) if i == 0 else row))
        return out, 0

    def keys(self, multi: bool = False) -> KeyBar:
        return keybar("session", multi, False, self.view)

    def _parts(self, w: int, h: int, tabs, keys, animate: bool) -> list[tuple[RenderableType, int | None]]:
        at, t = now(), time.monotonic()
        fixed = 2 + (1 if tabs is not None else 0)
        if self.view == "graph" and w >= MIN_W:
            sg = SessionGraph(self, at, t, animate=animate)
            plan = sg.plan(w, h - fixed - LOG_MIN - 2)
            if plan is not None:
                log_rows = log_size(h - fixed - plan[3] - 2, len(self.s.events))
                return [
                    *([(tabs, 1)] if tabs is not None else []),
                    (sg, None),
                    (log_panel(self.log_items(log_rows), log_rows), log_rows + 2),
                    (self.footer(), 1),
                    (keys, 1),
                ]
        log_rows = max(LOG_MIN, min(8, h // 6))
        if h - 6 - (log_rows + 2) < 6:
            log_rows = 0
        parts: list[tuple[RenderableType, int | None]] = [
            (self.header(at), 2),
            (tabs if tabs is not None else Blank(), 1),
            (CardsView(self, at, t, animate), None),
        ]
        if log_rows:
            parts.append((log_panel(self.log_items(log_rows), log_rows), log_rows + 2))
        return [*parts, (self.footer(), 1), (keys, 1)]

    def screen(
        self,
        height: int,
        tabs: RenderableType | None = None,
        keys: RenderableType | None = None,
        animate: bool = True,
    ) -> RenderableType:
        """The full-screen frame; ``animate`` False – a still one (no spinners, no flowing dots)."""
        keys = keys if keys is not None else self.keys()
        return Sized(lambda w, h: layout(self._parts(w, h, tabs, keys, animate)), height)

    def snapshot(self, log_rows: int = 12, tabs: RenderableType | None = None, height: int = 30) -> RenderableType:
        def build(w: int, _h) -> RenderableType:
            at, t = now(), 0.0
            log = log_panel(self.log_items(log_rows), log_rows)
            if self.view == "graph" and w >= MIN_W:
                sg = SessionGraph(self, at, t, height, animate=False)
                if sg.plan(w, height) is not None:
                    return Group(*([tabs] if tabs is not None else []), sg, log, self.footer())
            return Group(
                self.header(at),
                tabs if tabs is not None else Text(""),
                CardsView(self, at, t, False),
                log,
                self.footer(),
            )

        return Sized(build, fill=False)


# ---- overview ----

RUN_AGENTS_MAX = 8  # agent lines in a workflow block
SESSION_LINES_MAX = 4  # subagent and extra background lines in a session block


class Overview:
    """All tracked runs and live sessions on one screen. When they do not fit, agent lines go first,
    then blocks fold into one line each, and at last "… N more – Tab"."""

    def __init__(self, runs: list[Run], sessions: list[SessionWatch] | None = None, colors: AgentColors | None = None):
        self.runs = runs
        self.sessions = sessions or []
        self.colors = colors if colors is not None else AgentColors()
        self.animate = True  # False in a still frame (--once): no spinners

    def color(self, label: str) -> str:
        return self.colors(label)

    def header(self) -> RenderableType:
        running = sum(r.state == "running" for r in self.runs)
        finished = len(self.runs) - running
        by_state = {k: sum(s.state == k for s in self.sessions) for k in SESSION_COUNTS}
        colors = dict(zip(SESSION_COUNTS, (bold(C.blue), bold(C.yellow), C.cyan, C.dim), strict=True))
        g = Table.grid(expand=True, padding=(0, 2))
        g.add_column(no_wrap=True)
        g.add_column(ratio=1, justify="right", no_wrap=True, overflow="ellipsis")

        def counts_line(words: bool, with_finished: bool) -> Text:
            """Workflows and sessions by state; ``words`` False – sessions as icon and number."""
            t = Text(no_wrap=True, overflow="ellipsis", justify="right")
            t.append(tr("overview.runs", running=status_count(RUNNING, running)), style=bold(C.orange))
            if finished and with_finished:
                t.append(" · " + plural("count.finished", finished), style=C.dim)
            first = True
            for k in SESSION_COUNTS:
                if by_state[k]:
                    t.append("   " + tr("overview.sessions") if first else " · ", style=C.text if first else C.dim)
                    n = by_state[k]
                    t.append(plural(f"count.{k}", n) if words else f"{session_icon(k)} {n}", style=colors[k])
                    first = False
            return t

        # shorter variants for a narrow terminal, as in the key bar: the finished workflows (their
        # blocks are on the screen anyway) go first, then session states become icons
        variants = [
            counts_line(True, True),
            counts_line(True, False),
            counts_line(False, True),
            counts_line(False, False),
        ]
        right = Fit(variants)
        g.add_row(Text.assemble(("WFWATCH  ", bold(C.orange)), (tr("overview.title"), "bold")), right)
        keep, idle = _model.Tracker.KEEP_SEC // 60, _model.IDLE_KEEP_SEC // 60
        note = Text(tr("overview.note", keep=keep, idle=idle), style=C.dim, no_wrap=True, overflow="ellipsis")
        return Group(g, note)

    def owned(self, s: SessionWatch) -> str:
        nums = [str(n) for n, r in enumerate(self.runs, 1) if r.session_id == s.session_id and r.state == "running"]
        return tr("overview.owns", nums=", ".join(nums)) if nums else tr("overview.no_workflow")

    # ---- sizes ----

    def _items(self) -> list[tuple[str, int, Run | SessionWatch]]:
        return [("run", n, r) for n, r in enumerate(self.runs, 1)] + [
            ("session", n, s) for n, s in enumerate(self.sessions, len(self.runs) + 1)
        ]

    @staticmethod
    def _base(kind: str, obj) -> int:
        return 4 + (1 if kind == "session" and obj.running_bg_tasks() else 0)

    @staticmethod
    def _detail(kind: str, obj) -> int:
        if kind == "run":
            active = sum(a.status == RUNNING for a in obj.current_agents())
            return min(active, RUN_AGENTS_MAX)
        extra_bg = max(0, len(obj.running_bg_tasks()) - 1)
        return min(extra_bg + len(obj.running_subagents()), SESSION_LINES_MAX)

    def plan(self, height: int) -> tuple[list[int | None], int, int]:
        """Lines of detail for each item (None – folded into one line), hidden workflows and sessions."""
        items = self._items()
        base = [self._base(k, o) for k, _, o in items]
        detail = [self._detail(k, o) for k, _, o in items]
        n = len(items)
        if sum(base) + sum(detail) <= height:
            return list(detail), 0, 0
        if sum(base) <= height:
            give, extra = [0] * n, height - sum(base)
            while extra > 0:
                moved = False
                for i in range(n):
                    if give[i] < detail[i] and extra > 0:
                        give[i] += 1
                        extra -= 1
                        moved = True
                if not moved:
                    break
            return list(give), 0, 0
        for k in range(n - 1, -1, -1):
            if sum(base[:k]) + (n - k) <= height:
                return [0] * k + [None] * (n - k), 0, 0
        show = max(0, height - 1)
        hidden = items[show:]
        return ([None] * show, sum(k == "run" for k, _, _ in hidden), sum(k == "session" for k, _, _ in hidden))

    def body_height(self, height: int) -> int:
        if not self.runs and not self.sessions:
            return 1
        plan, hr, hs = self.plan(height)
        items = self._items()
        used = sum(1 if d is None else self._base(k, o) + d for d, (k, _, o) in zip(plan, items, strict=False))
        return used + (1 if hr or hs else 0)

    # ---- blocks ----

    def block(self, n: int, r: Run, at: datetime, t: float, rows: int) -> Panel:
        state = run_state(r)
        top = Table.grid(expand=True, padding=(0, 2))
        top.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        top.add_column(justify="right", no_wrap=True)
        top.add_row(
            Text.assemble(
                (r.project, C.cyan),
                (" · ", C.dim),
                (r.session_name, C.purple),
                (" · " + r.description if r.description else "", C.dim),
            ),
            Text.assemble((state.text, bold(state.color)), ("  " + run_duration(r, at), "bold")),
        )
        lines: list[RenderableType] = [top, phase_chain(r)]
        ags = r.current_agents()
        active = [a for a in ags if a.status == RUNNING]
        shown = active if rows >= len(active) else active[: rows - 1 if rows >= 2 else rows]
        if shown:
            g = Table.grid(padding=(0, 2), expand=True)
            g.add_column(no_wrap=True, min_width=10)
            g.add_column(no_wrap=True, max_width=28, overflow="ellipsis")
            g.add_column(no_wrap=True)
            g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            for a in shown:
                g.add_row(
                    agent_status(a, at, t, animate=self.animate),
                    Text(a.label, style=self.color(a.label)),
                    Text(short_model(a.model), style=model_color(a.model)),
                    Text(action_text(a.last_action) or "…", style=C.text),
                )
            lines.append(g)
        if len(shown) < len(active) and rows >= 2:
            lines.append(Text("  " + plural("more.agents", len(active) - len(shown), key=n), style=C.dim))
        tally = status_counts(ags)  # failed and interrupted agents too: "0 running · 1 done" hid a failure
        return Panel(
            Group(*lines),
            title=Text.assemble(
                (f" {n} ", f"bold reverse {state.color}"), (f" {r.name or r.run_id} ", bold(state.color))
            ),
            title_align="left",
            subtitle=Text.assemble(" ", *tally, " ") if tally else None,
            subtitle_align="right",
            border_style=state.color if r.state == "running" else C.dim,
            box=box.HEAVY if r.state == "running" else box.ROUNDED,
        )

    def session_block(self, n: int, s: SessionWatch, at: datetime, t: float, rows: int) -> Panel:
        state = session_state(s)
        top = Table.grid(expand=True, padding=(0, 2))
        top.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        top.add_column(justify="right", no_wrap=True)
        place = where(s.where)
        model = short_model(s.main.model)
        top.add_row(
            Text.assemble(
                (s.project, C.cyan),
                (f" · {place}" if place else "", C.dim),
                (f" · {model}" if model else "", model_color(model)),
            ),
            session_state_text(s, at, t, self.animate),
        )
        when = "  " + ago(s.main.idle(at)) if s.main.last_ts else ""
        lines: list[RenderableType] = [
            top,
            Text.assemble(
                ("→ ", C.dim),
                (action_text(s.main.last_action) or "…", C.text),
                (when, C.dim),
                no_wrap=True,
                overflow="ellipsis",
            ),
        ]
        bg = s.running_bg_tasks()
        if bg:
            lines.append(text_of(bg_parts(s, bg[0], at)))
        extra: list[RenderableType] = [text_of(bg_parts(s, task, at, with_proc=False)) for task in bg[1:]]
        subs = s.running_subagents()
        extra += [
            Text.assemble(
                (f"  {live_icon(t, self.animate)} ", C.blue),
                (a.label, self.color(a.label)),
                ("  " + (action_text(a.last_action) or "…"), C.dim),
                no_wrap=True,
                overflow="ellipsis",
            )
            for a in subs
        ]
        if len(extra) > rows:
            hidden = len(extra) - max(0, rows - 1)
            more = [Text("  " + tr("more.key", n=hidden, key=n), style=C.dim)] if rows else []
            extra = extra[: max(0, rows - 1)] + more
        lines += extra
        sub = join(self.owned(s), plural("count.subagents", len(subs)) if subs else "")
        return Panel(
            Group(*lines),
            title=Text.assemble(
                (f" {n} ", f"bold reverse {state.color}"), (f" {SESSION_TAG} {s.name} ", bold(state.color))
            ),
            title_align="left",
            subtitle=Text(f" {sub} ", style=C.dim),
            subtitle_align="right",
            border_style=state.color,
            box=box.ROUNDED,
        )

    def line(self, n: int, kind: str, obj, at: datetime, t: float) -> Text:
        """A block folded into one line."""
        num = f" {n:>{len(str(len(self.runs) + len(self.sessions)))}} "
        if kind == "run":
            state = run_state(obj)
            return Text.assemble(
                (num, f"bold reverse {state.color}"),
                " ",
                (obj.name or obj.run_id, bold(state.color)),
                "  ",
                (f"{state.text} {run_duration(obj, at)}", state.color),
                "  ",
                phase_chain(obj),
                no_wrap=True,
                overflow="ellipsis",
            )
        state = session_state(obj)
        return Text.assemble(
            (num, f"bold reverse {state.color}"),
            " ",
            (f"{SESSION_TAG} {obj.name}", bold(state.color)),
            "  ",
            session_state_text(obj, at, t, self.animate),
            ("  · " + obj.project, C.cyan),
            ("  → " + (action_text(obj.main.last_action) or "…"), C.dim),
            no_wrap=True,
            overflow="ellipsis",
        )

    def body(self, at: datetime, t: float, height: int) -> RenderableType:
        if not self.runs and not self.sessions:
            return Text("  " + tr("overview.empty"), style=C.dim)
        plan, hidden_runs, hidden_sessions = self.plan(height)
        out: list[RenderableType] = []
        for (kind, n, obj), d in zip(self._items(), plan, strict=False):  # the plan may drop the tail
            if d is None:
                out.append(self.line(n, kind, obj, at, t))
            elif kind == "run":
                out.append(self.block(n, obj, at, t, d))
            else:
                out.append(self.session_block(n, obj, at, t, d))
        if hidden_runs or hidden_sessions:
            out.append(Text("  " + more_hidden(hidden_runs, hidden_sessions, "Tab"), style=C.dim))
        return Group(*out)

    def log_items(self, rows: int) -> list[tuple[Event, Text | None, str]]:
        items: list[tuple[Event, Text | None, str]] = []
        for n, r in enumerate(self.runs, 1):
            tag = run_tag(n, r)
            items += [(e, tag, self.color(e.who)) for e in r.events[-rows:]]
        for n, s in enumerate(self.sessions, len(self.runs) + 1):
            tag = Text(str(n), style=bold(session_state(s).color))
            items += [(e, tag, self.color(e.who)) for e in s.events[-rows:]]
        items.sort(key=lambda x: x[0].ts)
        return items[-rows:]

    def footer(self) -> Text:
        ags = [a for r in self.runs for a in r.current_agents()]
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" " + tr("overview.agents"), style=C.text)
        for text, style in status_counts(ags) or [(status_count(RUNNING, 0), C.dim)]:
            t.append(text, style=style)
        if bg := sum(len(s.running_bg_tasks()) for s in self.sessions):
            t.append(" · " + tr("overview.bg", n=bg), style=C.cyan)
        return t

    def keys(self) -> KeyBar:
        return keybar("overview", len(self.runs) + len(self.sessions) > 1, False)

    def _parts(self, w: int, h: int) -> list[tuple[RenderableType, int | None]]:
        at, t = now(), time.monotonic()
        fixed = 2 + 1 + 2  # header, blank line, footer and keys
        room = h - fixed - LOG_MIN - 2
        used = min(self.body_height(room), max(room, 1))
        events = sum(len(r.events) for r in self.runs) + sum(len(s.events) for s in self.sessions)
        log_rows = log_size(h - fixed - used - 2, events)
        return [
            (self.header(), 2),
            (Blank(), 1),
            (self.body(at, t, h - fixed - log_rows - 2), None),
            (log_panel(self.log_items(log_rows), log_rows), log_rows + 2),
            (self.footer(), 1),
            (self.keys(), 1),
        ]

    def screen(self, height: int) -> RenderableType:
        """The full-screen frame (a still one when ``animate`` is False)."""
        return Sized(lambda w, h: layout(self._parts(w, h)), height)

    def snapshot(self, log_rows: int = 12) -> RenderableType:
        at, t = now(), 0.0
        self.animate = False
        return Group(
            self.header(),
            Text(""),
            self.body(at, t, 10**6),
            log_panel(self.log_items(log_rows), log_rows),
            self.footer(),
        )
