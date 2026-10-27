"""Small pieces shared by the views: formatting, states, status lines, cell-aware text fitting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from rich.cells import cell_len, set_cell_size
from rich.console import RenderableType
from rich.segment import Segment
from rich.text import Text

from .i18n import plural
from .i18n import t as tr
from .model import (
    BACKGROUND,
    BUSY,
    DONE,
    IDLE,
    PENDING,
    RUNNING,
    WAITING,
    Action,
    Agent,
    BgTask,
    Event,
    Phase,
    Run,
    SessionWatch,
    first_line,
    short_model,
)
from .theme import (
    COUNTED,
    SKIPPED,
    SUPERSEDED,
    C,
    bold,
    phase_color,
    status_color,
    status_count,
    status_icon,
)

SPINNER = "◐◓◑◒"
SPARK = " ▁▂▃▄▅▆▇█"
IDLE_WARN = 180  # seconds without events before a running agent is shown as silent
SILENT_ICON = "◌"
BG_ICON = "⚙"
SESSION_TAG = "◆"
MESSAGE_ICON = "✎"
# tools shown as a phrase rather than by their name
VERBS = {"WebSearch": "verb.search", "WebFetch": "verb.fetch", "StructuredOutput": "verb.result"}


# ---- numbers and text ----


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def hhmm(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%H:%M") if dt else ""


def fit(s: str, width: int) -> str:
    """Cut to ``width`` terminal cells, with an ellipsis when something was cut."""
    if width <= 0:
        return ""
    if cell_len(s) <= width:
        return s
    return set_cell_size(s, width - 1).rstrip() + "…"


def join(*parts: str, sep: str = " · ") -> str:
    """Non-empty parts joined by a separator (no dangling separators)."""
    return sep.join(p for p in parts if p)


def ago(sec: float) -> str:
    return tr("time.ago", dur=fmt_dur(sec))


def where(entrypoint: str) -> str:
    """Where a session is open: VSCode, the terminal, or the raw entrypoint."""
    if entrypoint == "claude-vscode":
        return "VSCode"
    return tr("where.cli") if entrypoint == "cli" else entrypoint


# ---- actions and events in words ----


def action_text(a: Action | None) -> str:
    """ "Read src/app.py", "Bash Run tests", "✎ first line of a message"."""
    if a is None:
        return ""
    if not a.tool:
        return f"{MESSAGE_ICON} {a.arg}" if a.arg else ""
    verb = tr(VERBS[a.tool]) if a.tool in VERBS else a.tool
    return join(verb, a.arg, sep=" ")


def last_action(a: Agent) -> str:
    """What a card shows: the first line of the result of a finished agent, else its last action."""
    if a.status == DONE and a.result:
        return first_line(a.result)
    return action_text(a.last_action)


def event_text(e: Event) -> str:
    if e.kind == "tool":
        return action_text(e.action)
    if e.kind == "start":
        return tr("log.started")
    if e.kind == "done":
        return e.detail or tr("log.done")
    if e.kind == "failed":
        return tr("log.failed")
    if e.kind == "run":
        if e.code == "finished":
            return tr("log.finished", state=run_state_word(e.detail))
        return tr("log.relaunched" if e.code == "relaunched" else "log.launched")
    return e.detail


# ---- colors and states ----


def model_color(model: str) -> str:
    m = model or ""
    return C.purple if "opus" in m else C.orange if "sonnet" in m else C.blue if "haiku" in m else C.text


def spinner(t: float) -> str:
    return SPINNER[int(t * 6) % len(SPINNER)]


def live_icon(t: float, animate: bool = True) -> str:
    """The icon of something running: a spinner, or in a still frame (--once, screenshots) the
    running dot. A still ◐ would read as "background task", which has that icon."""
    return spinner(t) if animate else status_icon(RUNNING)


@dataclass(frozen=True)
class State:
    icon: str
    word: str
    color: str

    @property
    def text(self) -> str:
        return f"{self.icon} {self.word}"


# state -> (icon, message key of the word, Palette attribute)
_RUN_STATES = {
    "running": ("●", "run.running", "orange"),
    "completed": ("✓", "run.completed", "green"),
    "failed": ("✗", "run.failed", "red"),
    "killed": ("■", "run.killed", "yellow"),
    "stopped": ("■", "run.stopped", "yellow"),
    "unknown": ("?", "run.unknown", "dim"),
}
_SESSION_STATES = {
    BUSY: ("●", "session.busy", "blue"),
    WAITING: ("◷", "session.waiting", "yellow"),
    BACKGROUND: ("◐", "session.background", "cyan"),
}
_IDLE = ("○", "session.idle", "dim")
SESSION_COUNTS = (BUSY, WAITING, BACKGROUND, IDLE)  # the order of "N working · N waiting · ..."


def run_state_word(state: str) -> str:
    """The word of a run state; an unknown one (a new status of Claude Code) as it is."""
    return tr(_RUN_STATES[state][1]) if state in _RUN_STATES else state


def run_state(r: Run) -> State:
    icon, _, color = _RUN_STATES.get(r.state, ("?", "", "dim"))
    return State(icon, run_state_word(r.state), getattr(C, color))


def session_state(s: SessionWatch) -> State:
    icon, key, color = _SESSION_STATES.get(s.state, _IDLE)
    return State(icon, tr(key), getattr(C, color))


def session_icon(state: str) -> str:
    """The icon of a session state (busy, waiting, background, idle)."""
    return _SESSION_STATES.get(state, _IDLE)[0]


def session_state_text(s: SessionWatch, at: datetime, t: float, animate: bool = True) -> Text:
    """State and how long: a working session gets a spinner (a still dot in a still frame)."""
    state = session_state(s)
    since = session_since(s, at)
    if s.state == BUSY:
        icon = live_icon(t, animate)
        return Text(join(f"{icon} {state.word}", since, sep=" "), style=bold(C.blue))
    return Text(join(state.text, since, sep=" "), style=bold(state.color))


def run_duration(r: Run, at: datetime) -> str:
    return fmt_dur(((r.ended or at) - r.started).total_seconds()) if r.started else ""


def agent_look(a: Agent) -> str:
    """The status an agent is drawn with: superseded attempts get their own (muted) look."""
    return SUPERSEDED if a.superseded else a.status


def agent_status(a: Agent, at: datetime, t: float, compact: bool = False, animate: bool = True) -> Text:
    """Status and time of an agent; ``compact`` drops the word of a silent agent (narrow cards);
    ``animate`` False – a still frame: the running dot instead of a spinner."""
    if a.superseded:
        return Text(f"{status_icon(SUPERSEDED)} {fmt_dur(a.elapsed(at))}", style=C.dim)
    if a.status == RUNNING and a.idle(at) > IDLE_WARN:
        word = "" if compact else tr("agent.silent") + " "
        return Text(f"{SILENT_ICON} {word}{fmt_dur(a.idle(at))}", style=C.yellow)
    if a.status == RUNNING:
        return Text(f"{live_icon(t, animate)} {fmt_dur(a.elapsed(at))}", style=bold(C.blue))
    return Text(f"{status_icon(a.status)} {fmt_dur(a.elapsed(at))}", style=status_color(a.status))


def sparkline(a: Agent, at: datetime, width: int) -> Text:
    """Rhythm of the agent's tool calls over its life: an honest stand-in for a percentage."""
    width = max(0, width)
    if not a.started or not a.tool_ts or not width:
        return Text("▁" * width, style=C.dim)
    end = at if a.status == RUNNING else (a.last_ts or at)
    span = max(1.0, (end - a.started).total_seconds())
    buckets = [0] * width
    for ts in a.tool_ts:
        i = int((ts - a.started).total_seconds() / span * width)
        buckets[min(max(i, 0), width - 1)] += 1
    top = max(buckets) or 1
    color = C.dim if a.superseded else status_color(a.status)
    t = Text()
    for b in buckets:
        t.append("▁", style=C.dim) if b == 0 else t.append(SPARK[1 + round(b / top * (len(SPARK) - 2))], style=color)
    return t


def counts(agents: list[Agent]) -> dict[str, int]:
    """Agents by status (running, done, failed, stale); superseded attempts are not counted."""
    out = dict.fromkeys(COUNTED, 0)
    for a in agents:
        if not a.superseded and a.status in out:
            out[a.status] += 1
    return out


def bracket(t: Text, label: str, value: str, color: str) -> None:
    """An item of a status line: label [value]."""
    t.append(f" {label} ", style=C.text)
    t.append(f"[{value}]", style=bold(color))
    t.append("  ")


Item = tuple[str, str, str]  # an item of a status line: label, value, color of the value


def status_line(*variants: list[Item]) -> Fit:
    """A status line "label [value]  label [value]": the first variant that fits the width
    (the variants go from the fullest to the shortest)."""
    out = []
    for items in variants:
        t = Text(no_wrap=True, overflow="ellipsis")
        for label, value, color in items:
            bracket(t, label, value, color)
        t.rstrip()
        out.append(t)
    return Fit(out)


# ---- phases ----


def phase_status(phases: list[Phase], i: int) -> str:
    """Status to draw a phase with: a phase without agents while a later one has started was skipped."""
    st = phases[i].status
    if st == PENDING and any(p.agents for p in phases[i + 1 :]):
        return SKIPPED
    return st


def phase_note(status: str, running: bool) -> str:
    """The word under a phase that has no agents."""
    return tr("status.skipped" if status == SKIPPED or not running else "status.pending")


def phase_title(p: Phase) -> str:
    return p.title or tr("phase.none")


def phase_chain(r: Run) -> Text:
    """All phases on one line: ✓ Wave 1 → ● Wave 2 → ○ Wave 3."""
    t = Text(no_wrap=True, overflow="ellipsis")
    phases = r.phases()
    for i, p in enumerate(phases):
        st = phase_status(phases, i)
        if i:
            t.append(" → ", style=C.dim)
        t.append(f"{status_icon(st)} {phase_title(p)}", style=bold(C.orange) if st == RUNNING else phase_color(st))
    return t


# ---- sessions ----


def session_since(s: SessionWatch, at: datetime) -> str:
    """How long the session has been in its state; for a background command – how long it runs."""
    if s.state == BACKGROUND and (running := s.running_bg_tasks()):
        return fmt_dur(max(t.elapsed(at) for t in running))
    return fmt_dur((at - s.since).total_seconds()) if s.since else ""


def bg_parts(s: SessionWatch, task: BgTask, at: datetime, with_proc: bool = True) -> list[tuple[str, str]]:
    """A running background command: description · time · what runs now · last output line."""
    parts = [
        (f"{BG_ICON} {tr('bg.label')}", bold(C.cyan)),
        (task.description or task.task_id, C.text),
        (f" · {fmt_dur(task.elapsed(at))}", C.dim),
    ]
    proc = s.current_proc if with_proc else None
    if proc is not None:
        parts.append((" · " + tr("bg.now", proc=proc.name, cpu=f"{proc.cpu:.0f}"), C.cyan))
    if task.last_line:
        parts.append((f" · «{task.last_line}»", C.dim))
    return parts


def bg_done_parts(task: BgTask, at: datetime) -> list[tuple[str, str]]:
    """A finished background command: status, description, exit code, when."""
    icon, color = {"completed": ("✓", C.green), "failed": ("✗", C.red), "killed": ("■", C.yellow)}.get(
        task.status, ("⚠", C.yellow)
    )
    if task.status == "stale":
        note = tr("bg.lost")
    elif task.exit_code is not None:
        note = tr("bg.exit", code=task.exit_code)
    else:
        note = tr({"killed": "bg.killed", "failed": "bg.failed"}.get(task.status, "bg.done"))
    when = ago((at - task.ended).total_seconds()) if task.ended else ""
    parts = [(f"{icon} ", bold(color)), (task.description or task.task_id, C.text), (" · " + join(note, when), C.dim)]
    if task.last_line:
        parts.append((f" · «{task.last_line}»", C.dim))
    return parts


def text_of(parts: list[tuple[str, str]]) -> Text:
    return Text.assemble(*parts, no_wrap=True, overflow="ellipsis")


# ---- counts in words ----


def status_counts(agents: list[Agent], sep: str = " · ") -> list[tuple[str, str]]:
    """ "1 running · 3 done · 1 replaced" as (text, style) parts, zero counts left out."""
    out: list[tuple[str, str]] = []
    items = [(s, m, status_color(s)) for s, m in counts(agents).items()]
    items.append((SUPERSEDED, sum(a.superseded for a in agents), C.dim))
    for s, m, color in items:
        if m:
            if out:
                out.append((sep, C.dim))
            out.append((status_count(s, m), color))
    return out


def progress(done: int, total: int) -> str:
    return tr("progress.done", done=done, total=total)


def running_of(n: int, total: int) -> str:
    """ "1 running / 3"."""
    return plural("progress.running", n, total=total)


def calls_stats(a: Agent, with_out: bool = True) -> str:
    calls = plural("count.calls", a.tool_calls)
    if with_out:
        return tr("card.stats", calls=calls, ctx=fmt_k(a.context_tokens), out=fmt_k(a.output_tokens))
    return tr("card.stats_short", calls=calls, ctx=fmt_k(a.context_tokens))


def more_hidden(runs: int, sessions: int, key: str) -> str:
    """ "… 3 workflows and 2 sessions not shown – Tab"."""
    parts = [plural("count.workflows", runs) if runs else "", plural("count.sessions", sessions) if sessions else ""]
    what = tr("more.and", a=parts[0], b=parts[1]) if all(parts) else join(*parts)
    return tr("more.items", what=what, key=key)


# ---- renderables ----


class Fit:
    """The first of several variants of a one-line Text that fits the width; the last (shortest)
    one is cut with an ellipsis when even it is too wide."""

    def __init__(self, variants: list[Text]):
        self.variants = variants

    @property
    def plain(self) -> str:
        return self.variants[0].plain

    def fit(self, width: int) -> Text:
        for v in self.variants:
            if v.cell_len <= width:
                return v
        return self.variants[-1]

    def __rich_console__(self, console, options):
        yield self.fit(options.max_width)


class Blank:
    """Nothing (or blank lines): a Layout region that must stay empty. An empty Text would not do:
    it is falsy, and Layout draws its placeholder box for a falsy renderable."""

    def __rich_console__(self, console, options):
        for _ in range(options.height or 0):
            yield Segment.line()


class Sized:
    """Builds its renderable at render time, when the real width (and height) are known.

    ``fill`` – a full-screen view: the height is the region's (or ``height``, or the console's);
    otherwise the renderable takes its natural height and ``build`` gets None."""

    def __init__(
        self, build: Callable[[int, int | None], RenderableType], height: int | None = None, fill: bool = True
    ):
        self.build = build
        self.height = height
        self.fill = fill

    def __rich_console__(self, console, options):
        if not self.fill:
            yield from console.render(self.build(options.max_width, None), options)
            return
        h = options.height or self.height or console.height
        yield from console.render(self.build(options.max_width, h), options.update(height=h))


def short_models(agents: list[Agent]) -> list[str]:
    return sorted({short_model(a.model) for a in agents if a.model})
