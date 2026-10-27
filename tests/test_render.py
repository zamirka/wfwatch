"""The screens as the full-screen mode draws them: sizes, frames, wide characters, themes, scrolling."""

import io
import json
import os
import re
import subprocess
import sys
from datetime import timedelta

import pytest
from rich.cells import cell_len
from rich.console import Console

from wfwatch import cli, i18n, model, theme
from wfwatch.cli import App
from wfwatch.graph import Canvas, Diagram, GraphView
from wfwatch.render import KeyBar, Overview, Painter, keybar, tabs_line
from wfwatch.widgets import _RUN_STATES, _SESSION_STATES, Sized, State

# ---- helpers ----


def render(renderable, width=120, height=None, **kw) -> str:
    con = Console(record=True, width=width, height=height, file=io.StringIO(), **kw)
    con.print(renderable)
    return con.export_text().rstrip("\n")


TL, TR, BL, BR, VERT = "╭┏", "╮┓", "╰┗", "╯┛", "│┃"


def cells(text: str, width: int) -> list[list[str]]:
    """The screen as a grid of terminal cells (a wide character takes two)."""
    rows = []
    for line in text.split("\n"):
        row: list[str] = []
        for ch in line:
            n = cell_len(ch)
            if n == 0 and row:
                row[-1] += ch
                continue
            row.append(ch)
            if n == 2:
                row.append("")
        rows.append(row + [" "] * max(0, width - len(row)))
    return rows


def frame_problems(text: str, width: int) -> list[str]:
    """Broken frames: a box without its right or bottom side, a line wider than the screen,
    Rich's Layout placeholder, a render error."""
    out = [f"row {y} is {cell_len(line)} cells" for y, line in enumerate(text.split("\n")) if cell_len(line) > width]
    if re.search(r"\(\d+ x \d+\)", text):
        out.append("layout placeholder")
    if any(i18n.MESSAGES["error.render"][lang].split("{")[0] in text for lang in i18n.LANGS):
        out.append("render error")
    g = cells(text, width)
    for y, row in enumerate(g):
        for x, ch in enumerate(row):
            if ch not in TL or x + 1 >= len(row) or row[x + 1] not in "─━":
                continue  # not a box (a corner of a dotted arrow)
            yb = y + 1
            while yb < len(g) and g[yb][x] in VERT:
                yb += 1
            if yb >= len(g) or g[yb][x] not in BL:
                out.append(f"box at {x},{y}: no bottom")
                continue
            if not any(
                row[x2] in TR and g[yb][x2] in BR and all(g[yy][x2] in VERT for yy in range(y + 1, yb))
                for x2 in range(x + 1, len(row))
            ):
                out.append(f"box at {x},{y}: no right side")
    return out


PS = """\
    1     0   0.0 61-05:13:33 /sbin/launchd
  500     1   1.0    02:00:00 /usr/local/bin/claude
  520   500   0.0       05:00 /bin/zsh -c source /home/me/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'pytest -q'
  522   520  95.5       04:58 /usr/bin/python3 -m pytest
"""


def bg_command(h, sid, task, desc, minutes, output=None, done=None, project="-p"):
    """A Bash command started in the background `minutes` ago (and finished, if `done` is a status)."""
    text = f"Command running in background with ID: {task}." + (
        f" Output is being written to: {output}" if output else ""
    )
    rows = [
        h.assistant(
            h.ago(minutes=minutes),
            f"m-{task}",
            [h.tool("Bash", f"tu-{task}", command="make", description=desc, run_in_background=True)],
        ),
        {
            "type": "user",
            "timestamp": h.ago(minutes=minutes - 0.1),
            "toolUseResult": {"backgroundTaskId": task},
            "message": {"content": [{"type": "tool_result", "tool_use_id": f"tu-{task}", "content": text}]},
        },
    ]
    if done:
        rows.append(h.notify(task, done, h.ago(minutes=1), summary=f'Background command "{desc}" {done} (exit code 0)'))
    return h.transcript(sid, *rows, project=project)


def subagent(h, sid, aid, desc, ago_sec=20):
    p = h.root / "projects" / "-p" / sid / "subagents" / f"agent-{aid}.jsonl"
    h.jl(p, h.assistant(h.ago(seconds=ago_sec), f"s-{aid}", [h.tool("Grep", pattern=aid)]))
    p.with_name(f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")


def run_with(h, run_id, phases, agents, session="s-run", launched_min=30):
    """A running workflow: `agents` – (label, phase, status: running | done | failed)."""
    sess = h.root / "projects" / "-p" / session
    run = sess / "subagents" / "workflows" / run_id
    titles = ", ".join(json.dumps({"title": p}, ensure_ascii=False) for p in phases)
    script = f"export const meta = {{\n  name: 'flow-{run_id}',\n  phases: [{titles}],\n}}\n"
    (sess / "workflows" / "scripts").mkdir(parents=True, exist_ok=True)
    (sess / "workflows" / "scripts" / f"flow-{run_id}.js").write_text(script, encoding="utf-8")
    h.jl(sess.with_suffix(".jsonl"), h.launch(run_id, f"t-{run_id}", h.ago(minutes=launched_min)))
    h.jl(run / "journal.jsonl", {"type": "launched"})
    for i, (label, phase, status) in enumerate(agents):
        h.jl(run / "journal.jsonl", {"type": "started", "agentId": f"a{i}", "label": label, "phase": phase})
        h.jl(
            run / f"agent-a{i}.jsonl",
            {"type": "user", "timestamp": h.ago(minutes=launched_min - 1, seconds=-i)},
            h.assistant(
                h.ago(seconds=30 + i), f"m{i}", [h.tool("Read", file_path=f"/a/{i}.py")], model_id="claude-sonnet-5"
            ),
        )
        if status == "done":
            h.jl(run / "journal.jsonl", {"type": "result", "agentId": f"a{i}", "result": f"result {i}"})
        elif status == "failed":
            h.jl(run / "journal.jsonl", {"type": "failed", "agentId": f"a{i}"})
    return run


def tracker() -> model.Tracker:
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    return tr


@pytest.fixture
def busy_home(demo_run, monkeypatch):
    """The demo workflow plus three sessions: working with subagents, waiting, running a background command."""
    h, _, run = demo_run
    h.session("s-work", status="busy", name="worker", entrypoint="claude-vscode")
    h.transcript("s-work", h.assistant(h.ago(seconds=30), "w1", [h.tool("Read", file_path="/a/b.py")]))
    for i in range(2):
        subagent(h, "s-work", f"x{i}", f"Explore part {i}")
    h.session("s-ask", status="waiting", name="asker")
    h.transcript("s-ask", h.assistant(h.ago(seconds=50), "q1", [h.text("Which one?")]))
    out = h.root / "tmp" / "bgA.output"
    out.parent.mkdir(parents=True)
    out.write_text("tests/test_a.py ....  [ 63%]\n", encoding="utf-8")
    h.session("s-bg", status="idle", name="builder", since_ms=h.ms_ago(hours=2), pid=500)
    bg_command(h, "s-bg", "bgA", "Run the test suite", 5, output=out)
    bg_command(h, "s-bg", "bgB", "Build the wheel", 8, done="completed")
    table = model.ProcTable.parse(
        PS + f"{__import__('os').getpid():>5}     1   0.0       10:00 python -m pytest\n", model.now().timestamp()
    )
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    return h, run


def open_screen(app: App, run, which: str) -> None:
    if which == "overview":
        app.mode = "overview"
    elif which in ("graph", "cards"):
        app.open(run)
    else:
        app.open({"session": "s-work", "session-cards": "s-work", "background": "s-bg"}[which])
    if which.endswith("cards"):
        app.on_key("v")


# ---- every screen at the usual sizes ----


@pytest.mark.parametrize("size", [(80, 24), (120, 40), (200, 60)], ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("which", ["overview", "graph", "cards", "session", "session-cards", "background"])
def test_full_screen_frames_are_whole(busy_home, which, size):
    w, h = size
    _, run = busy_home
    app = App(tracker(), expand_all=False)
    open_screen(app, run, which)
    text = render(app.frame(h), w, h)
    assert frame_problems(text, w) == [], text
    assert len(text.split("\n")) == h
    lines = text.split("\n")
    assert "q" in lines[-1] and "quit" in lines[-1]  # the way out is always on the key bar
    expect = {
        "overview": "overview",
        "graph": "DEMO-FLOW",
        "cards": "Архитектор",
        "session": "WORKER",
        "session-cards": "Explore part 1",
        "background": "Run the test suite",
    }[which]
    assert expect in text, text


def test_no_layout_placeholder_without_tabs(demo_run):
    """An empty Text in a Layout region makes Rich draw a "(W x 1)" placeholder box."""
    tr = tracker()
    del tr.watches["sess-1"]  # only the workflow: no tab line
    app = App(tr, expand_all=False)
    for setup in (lambda: None, lambda: app.open(next(iter(tr.runs))), lambda: app.on_key("v")):
        setup()
        for w, h in ((100, 30), (80, 24)):
            text = render(app.frame(h), w, h)
            assert not re.search(r"\(\d+ x \d+\)", text), text


# ---- both languages ----

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# words of the English interface that must not show up in the Russian one (the data below avoids them)
ENGLISH_UI = re.compile(
    r"\b(running|done|failed|interrupted|replaced|pending|never ran|quit|overview|phases?|agents?|"
    r"subagents?|sessions?|log|background|waiting|idle|working|calls|more|ago|exit code|next|"
    r"scroll|cards|diagram|follow|launch|finish|time|silent|not shown|latest actions|key)\b",
    re.IGNORECASE,
)


@pytest.fixture
def english_home(claude_home, monkeypatch):
    """Everything the screens can show, with English data only: a workflow with done, running, failed,
    silent and phase-less agents; sessions working with subagents, waiting, idle and running a background
    command (plus a finished and a failed one)."""
    h = claude_home
    h.session("s-run", name="release")
    run = run_with(
        h,
        "wf_en",
        ["Collect", "Review", "Fix", "Ship"],
        [
            ("Collector", "Collect", "done"),
            ("Reviewer A", "Review", "running"),
            ("Reviewer B", "Review", "failed"),
            ("Reviewer C", "Review", "running"),
            ("Helper", "", "done"),
        ],
    )
    h.jl(run / "journal.jsonl", {"type": "started", "agentId": "a9", "label": "Sleeper", "phase": "Review"})
    h.jl(
        run / "agent-a9.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=20)},
        h.assistant(h.ago(minutes=10), "m9", [h.tool("WebFetch", url="https://example.com")]),
    )
    h.session("s-work", status="busy", name="worker", entrypoint="claude-vscode")
    h.transcript("s-work", h.assistant(h.ago(seconds=30), "w1", [h.tool("WebSearch", query="rich cells")]))
    for i in range(2):
        subagent(h, "s-work", f"x{i}", f"Explore part {i}")
    h.session("s-ask", status="waiting", name="asker")
    h.transcript("s-ask", h.assistant(h.ago(seconds=50), "q1", [h.text("Which one?")]))
    h.session("s-idle", status="idle", name="resting", since_ms=h.ms_ago(minutes=3))
    h.transcript("s-idle", h.assistant(h.ago(minutes=3), "i1", [h.tool("StructuredOutput")]))
    out = h.root / "tmp" / "bgA.output"
    out.parent.mkdir(parents=True)
    out.write_text("tests/test_a.py ....  [ 63%]\n", encoding="utf-8")
    h.session("s-bg", status="idle", name="builder", since_ms=h.ms_ago(hours=2), pid=500)
    bg_command(h, "s-bg", "bgA", "Run the test suite", 5, output=out)
    bg_command(h, "s-bg", "bgB", "Build the wheel", 8, done="completed")
    bg_command(h, "s-bg", "bgC", "Lint", 9, done="failed")
    table = model.ProcTable.parse(
        PS + f"{os.getpid():>5}     1   0.0       10:00 python -m pytest\n", model.now().timestamp()
    )
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    return h, run


LANG_SCREENS = {  # what each screen must say, in each language
    "overview": {
        "en": [
            "WFWATCH  overview",
            "sessions: 2 working · 1 waiting for you · 1 in background · 1 idle",
            "⚙ background: Run the test suite",
        ],
        "ru": [
            "WFWATCH  обзор",
            "сессии: 2 работают · 1 ждёт вас · 1 в фоне · 1 свободна",
            "⚙ фон: Run the test suite",
        ],
    },
    "graph": {
        "en": ["FLOW-WF_EN", "● RUNNING", "◇ launch", "agents [2/6 done]", "failed [1]"],
        "ru": ["FLOW-WF_EN", "● ИДЁТ", "◇ запуск", "агенты [2/6 готово]", "ошибки [1]"],
    },
    "cards": {
        "en": ["2. Review", "0/4 done", "1 call · ctx 1k · out 10"],
        "ru": ["2. Review", "0/4 готово", "1 вызов · ctx 1k"],
    },
    "session": {"en": ["WORKER", "session subagents"], "ru": ["WORKER", "субагенты сессии"]},
    "session-cards": {
        "en": ["Explore part 1", "session subagents · 2 running / 2"],
        "ru": ["Explore part 1", "субагенты сессии · 2 идут / 2"],
    },
    "background": {
        "en": ["BACKGROUND TASK", "background tasks", "Build the wheel · exit code 0", "bg [1 running]"],
        "ru": ["ФОНОВАЯ ЗАДАЧА", "фоновые задачи", "Build the wheel · код выхода 0", "фон [1 идёт]"],
    },
}


@pytest.mark.parametrize("lang", i18n.LANGS)
@pytest.mark.parametrize("which", list(LANG_SCREENS))
def test_screens_in_both_languages(english_home, which, lang):
    _, run = english_home
    i18n.use_lang(lang)
    app = App(tracker(), expand_all=False)
    open_screen(app, run, which)
    for w, h in ((80, 24), (120, 40), (200, 60)):
        text = render(app.frame(h), w, h)
        assert frame_problems(text, w) == [], text
        assert text.split("\n")[-1].rstrip().endswith("q  " + i18n.t("key.quit")), text
        if lang == "en":
            assert not CYRILLIC.search(text), text
        else:
            assert not ENGLISH_UI.search(text), ENGLISH_UI.search(text)
    text = render(app.frame(60), 200, 60)
    for s in LANG_SCREENS[which][lang]:
        assert s in text, (s, text)


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_once_list_and_messages_in_both_languages(english_home, lang, capsys):
    _, run = english_home
    i18n.use_lang(lang)
    tr = tracker()
    app = App(tr, expand_all=False)
    texts = [render(app.snapshot(), 140)]
    app.open(run)
    texts.append(render(app.snapshot(), 140))
    app.on_key("v")
    texts.append(render(app.snapshot(), 140))
    for sid in ("s-idle", "s-bg"):
        app.open(sid)
        texts.append(render(app.snapshot(), 140))
    app.failures["scan"] = "OSError: gone"
    texts.append(app.status_error())
    for argv in (["--list", "--width", "140"], ["--once", "--width", "140"], ["--once", "-o", "-c", "--width", "100"]):
        assert cli.main([*argv, "--lang", lang]) == 0
        texts.append(capsys.readouterr().out)
    for text in texts:
        if lang == "en":
            assert not CYRILLIC.search(text), text
        else:
            assert not ENGLISH_UI.search(text), (ENGLISH_UI.search(text), text)
    joined = "\n".join(texts)
    expect = {
        "en": [
            "no phase",
            "Search rich cells",
            "returning the result",
            "✎ Which one?",
            "Fetch https://example.com",
            "silent",
            "read error – scan: OSError: gone",
            "state",
            "✗ Lint · exit code 0",
        ],
        "ru": [
            "без этапа",
            "Поиск rich cells",
            "отдаёт результат",
            "✎ Which one?",
            "Загрузка https://example.com",
            "тишина",
            "ошибка чтения – scan: OSError: gone",
            "состояние",
            "✗ Lint · код выхода 0",
        ],
    }[lang]
    for s in expect:
        assert s in joined, s


def test_russian_numbers_agree(english_home):
    """Russian has three plural forms: 1 агент, 2 агента, 5 агентов."""
    i18n.use_lang("ru")
    tr = tracker()
    ov = render(App(tr, expand_all=False).snapshot(), 140)
    assert " 3 идут · 2 готовы " in ov  # the subtitle of the workflow block
    assert "агенты во всех запусках: 3 идут · 2 готовы" in ov
    assert "ведёт workflow 1" in ov and "2 субагента" in ov
    runs = Overview(tr.visible(), [], theme.AgentColors())  # room for one agent line of three
    assert "… ещё 2 агента – клавиша 1" in render(Sized(lambda w, h: runs.body(model.now(), 0.0, 6), fill=False), 140)


# ---- wide characters ----


def test_canvas_counts_terminal_cells():
    c = Canvas(20, 3)
    c.box(0, 0, 20, 3, "")
    c.text(2, 1, "日本語 ✅ ok 🎉 and a long tail", 16)
    assert all(cell_len(c.row(y)) == 20 for y in range(3))
    assert c.row(1).startswith("│ 日本語 ✅ ok 🎉…") and c.row(1).endswith(" │")
    c.put(3, 1, "x")  # over the second half of 日: the first half becomes a blank
    assert c.row(1).startswith("│  x本語") and cell_len(c.row(1)) == 20
    c.put(19, 2, "語")  # no room for the second half
    assert cell_len(c.row(2)) == 20
    assert all(cell_len(line) == 20 for line in c.to_text().plain.split("\n"))


def test_wide_labels_keep_the_frames_straight(claude_home):
    h = claude_home
    h.session("s-run", name="emoji ✅ session")
    run_with(
        h,
        "wf_emo",
        ["Этап ✅ проверка", "日本語フェーズ"],
        [
            ("Агент ✅", "Этап ✅ проверка", "done"),
            ("エージェント", "Этап ✅ проверка", "running"),
            ("🚀 ракета", "日本語フェーズ", "running"),
        ],
    )
    app = App(tracker(), expand_all=False)
    for setup in (lambda: app.open(next(iter(app.tracker.runs))), lambda: app.on_key("v"), lambda: app.on_key("o")):
        setup()
        for w, h in ((120, 40), (80, 24), (100, 30)):
            text = render(app.frame(h), w, h)
            assert frame_problems(text, w) == [], text


def test_all_icons_are_one_cell_wide():
    icons = [lk.icon for lk in theme.STATUS.values()] + [i for i, _, _ in _RUN_STATES.values()]
    icons += [i for i, _, _ in _SESSION_STATES.values()] + ["◌", "⚙", "↺", "◆", "◇", "▸", "►", "▼", "┊", "┆"]
    assert [i for i in icons if cell_len(i) != 1] == []


# ---- themes ----


def contrast(fg: str, bg: str = "#ffffff") -> float:
    def lum(h):
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))

        def f(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_detection_and_switching():
    assert theme.detect_theme({"COLORFGBG": "0;15"}) == "light"
    assert theme.detect_theme({"COLORFGBG": "0;default;7"}) == "light"
    assert theme.detect_theme({"COLORFGBG": "15;0"}) == "dark"
    assert theme.detect_theme({"COLORFGBG": "12;8"}) == "dark"
    assert theme.detect_theme({}) == "dark" and theme.detect_theme({"COLORFGBG": "x"}) == "dark"
    try:
        assert theme.use_theme("light") == "light" and theme.C.blue == theme.LIGHT.blue
        assert theme.use_theme("auto", {"COLORFGBG": "15;0"}) == "dark" and theme.C.blue == theme.DARK.blue
    finally:
        theme.use_theme("dark")
    accents = ("orange", "green", "blue", "purple", "red", "yellow", "cyan", "pink", "lime")
    assert min(contrast(getattr(theme.LIGHT, a)) for a in accents) >= 4.5  # readable on a white background
    assert theme.C.text == "" and theme.C.dim == "dim"  # main text: the terminal's own colors


def test_theme_option_is_applied_and_restored(demo_run, capsys, monkeypatch):
    seen = []
    real = cli._run

    def spy(args, console):
        seen.append(theme.current())
        return real(args, console)

    monkeypatch.setattr(cli, "_run", spy)
    assert cli.main(["--once", "--theme", "light"]) == 0
    assert seen == ["light"] and theme.current() == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert cli.main(["--once"]) == 0 and seen[-1] == "light"


def test_main_text_has_no_fixed_white(demo_run):
    tr = tracker()
    app = App(tr, expand_all=False)
    app.open(next(iter(tr.runs)))
    for snapshot in (app.snapshot(), App(tr, False).snapshot()):
        con = Console(width=120, file=io.StringIO(), force_terminal=True, color_system="truecolor")
        con.print(snapshot)
        out = con.file.getvalue()
        assert "38;2;255;255;255" not in out and "\x1b[37m" not in out and "\x1b[97m" not in out


def test_no_color_is_honoured(busy_home, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    _, run = busy_home
    app = App(tracker(), expand_all=False)
    con = Console(width=120, height=40, file=io.StringIO(), force_terminal=True, color_system="truecolor")
    con.print(app.frame(40))
    app.open(run)
    con.print(app.frame(40))
    out = con.file.getvalue()
    assert "\x1b[1m" in out  # bold stays
    assert not re.search(r"\x1b\[[0-9;]*(38|48);", out)  # no colors


# ---- structure ----


def test_graph_does_not_import_the_screens():
    code = "import sys, wfwatch.graph; print('wfwatch.render' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", check=True).stdout
    assert out.strip() == "False"


def test_agent_colors_are_kept_across_frames_and_screens(busy_home):
    h, run = busy_home
    tr = tracker()
    app = App(tr, expand_all=False)
    render(app.frame(40), 120, 40)
    before = app.colors("Explore part 1")
    subagent(h, "s-ask", "y0", "A newcomer")  # a label appears in a session listed above
    tr.poll()
    render(app.frame(40), 120, 40)
    app.open(run)
    render(app.frame(40), 120, 40)
    assert app.colors("Explore part 1") == before
    assert app.painter(tr.runs[run]).colors is app.colors
    assert app.session_painter(tr.watches["s-work"]).colors is app.colors


def test_one_current_phase_for_diagram_and_status_line(claude_home):
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_pipe", ["Alpha", "Beta", "Gamma"], [("A", "Alpha", "running"), ("B", "Beta", "running")])
    r = model.Run(d)
    r.poll(model.load_sessions())
    out = render(Painter(r).snapshot(height=40), 130)
    assert "▸● 1. Alpha" in out and "phase [1/3]" in out


# ---- overview ----


def test_overview_folds_what_does_not_fit(claude_home):
    h = claude_home
    for i in range(9):
        h.session(f"s{i}", status="busy", name=f"sess-{i}")
        h.transcript(f"s{i}", h.assistant(h.ago(seconds=20), f"m{i}", [h.tool("Read", file_path="/a/b.py")]))
        run_with(h, f"wf_{i}", ["A", "B"], [(f"agent {i}.{j}", "A", "running") for j in range(3)], session=f"s{i}")
    app = App(tracker(), expand_all=False)
    for w, hh in ((80, 24), (120, 40), (200, 60)):
        text = render(app.frame(hh), w, hh)
        assert frame_problems(text, w) == [], text
        assert "WFWATCH" in text and "quit" in text and "flow-wf_0" in text
        if hh == 24:  # 18 items on 14 rows: the rest is counted
            assert re.search(r"… \d+ sessions not shown – Tab", text), text
        else:  # blocks fold into lines, but everything is there
            assert "not shown" not in text and "sess-8" in text and "┏" in text, text


def test_overview_shows_agent_lines_when_there_is_room(busy_home):
    app = App(tracker(), expand_all=False)
    text = render(app.frame(40), 120, 40)
    assert "Архитектор" in text and "Explore part 0" in text
    assert "sessions: 2 working · 1 waiting for you · 1 in background" in text
    assert "all projects" in text.split("\n")[1] and "(last 30 min)" in text  # the subtitle is a line of its own


# ---- the diagram ----


def test_sidebar_lists_every_phase_when_there_is_room(claude_home):
    h = claude_home
    h.session("s-run", status="idle", since_ms=h.ms_ago(minutes=1))
    d = run_with(h, "wf_two", ["Этап 1", "Этап 2"], [("А", "Этап 1", "done"), ("Б", "Этап 2", "done")])
    h.jl(d.parents[2].with_suffix(".jsonl"), h.notify("t-wf_two", "completed", h.ago(minutes=1)))
    r = model.Run(d)
    r.poll(model.load_sessions())
    for height in (24, 34):
        out = render(Painter(r).snapshot(height=height), 120)
        assert "◇ launch" in out and "1. Этап 1" in out and "2. Этап 2" in out and not re.search(r"… \d+ more", out), (
            out
        )


def test_skipped_phase_is_not_the_previous_block(claude_home):
    h = claude_home
    h.session("s-run")
    d = run_with(
        h,
        "wf_gap",
        ["Сбор", "Пустой", "Проверка"],
        [("Сборщик", "Сбор", "done"), ("Проверяющий", "Проверка", "running")],
    )
    r = model.Run(d)
    r.poll(model.load_sessions())
    p = Painter(r)
    out = render(p.snapshot(height=60), 120)
    assert out.count("1. Сбор") == 2 and out.count("2. Пустой") == 1  # the block above is the last phase that ran
    assert "○ Пустой" in render(App(tracker(), False).snapshot(), 120)
    p.phase_sel = 1
    assert "never ran" in render(p.snapshot(height=40), 120)


@pytest.mark.parametrize("n", range(1, 8))
def test_fan_is_centred_on_its_trunk(claude_home, n):
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_fan", ["P"], [(f"a{i}", "P", "running") for i in range(n)])
    r = model.Run(d)
    r.poll(model.load_sessions())
    p = Painter(r)
    for w in range(70, 241, 3):
        gv = GraphView(p, model.now(), 0.0, animate=False)
        c = gv.draw(w, 60, gv.plan(w, 60))
        rows = [c.row(y) for y in range(c.h)]
        ya = next(y for y, row in enumerate(rows) if "▼" in row)
        bar, arrows = rows[ya - 1], rows[ya]
        xs = [x for x, ch in enumerate(arrows) if ch == "▼"]
        assert all(bar[x] in "┌┐┬┼│" for x in xs), (w, bar)
        assert sum(ch in "┴┼" for ch in bar) == (1 if len(xs) > 1 else 0), (w, bar)
        assert not any("┬┴" in row or "┴┬" in row for row in rows), w


def test_diagram_adapts_to_the_screen(claude_home):
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_big", ["P", "Q"], [(f"agent {i}", "P", "running") for i in range(20)])
    app = App(tracker(), expand_all=False)
    app.open(d)
    text = render(app.frame(60), 220, 60)
    assert frame_problems(text, 220) == []
    assert all(f"agent {i}" in text for i in range(20))  # several rows of cards: every agent fits
    small = render(app.frame(24), 80, 24)
    assert frame_problems(small, 80) == [] and re.search(r"\+\d+ more", small)  # compact, nothing cut


def test_silent_agent_keeps_its_model(claude_home):
    a = model.Agent(
        "a",
        "Long label",
        "P",
        model.Tail(claude_home.root / "none"),
        model="claude-sonnet-5",
        started=model.now() - timedelta(minutes=30),
        last_ts=model.now() - timedelta(minutes=20),
    )
    c = Canvas(30, 6)
    Diagram(theme.AgentColors(), model.now(), 0.0).agent_box(c, a, 0, 0, 22, 6)
    assert "sonnet 5" in c.row(2) and "◌ 20:00" in c.row(2)


def test_dots_run_only_on_live_links(claude_home):
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_live", ["Done", "Live"], [("A", "Done", "done"), ("B", "Live", "running")])
    r = model.Run(d)
    r.poll(model.load_sessions())
    p = Painter(r)
    at = model.now()

    def frames(animate):
        return {GraphView(p, at, t / 7, animate=animate).draw(130, 40).to_text().plain for t in range(15)}

    assert len(frames(True)) > 1  # the running phase: dots flow
    assert len(frames(False)) == 1  # a still frame (--once): no dots
    p.phase_sel = 0
    assert len(frames(True)) == 1  # a finished phase of a running workflow: nothing flows


def test_superseded_attempts_are_muted_and_not_counted(claude_home):
    h = claude_home
    h.session("s-r")
    sess = h.root / "projects" / "-p" / "s-r"
    run = sess / "subagents" / "workflows" / "wf_res"
    h.jl(
        sess.with_suffix(".jsonl"),
        h.launch("wf_res", "t1", h.ago(minutes=30)),
        h.notify("t1", "killed", h.ago(minutes=24)),
        h.launch("wf_res", "t2", h.ago(minutes=20)),
    )
    h.jl(
        run / "journal.jsonl",
        {"type": "launched"},
        {"type": "started", "agentId": "old", "label": "X", "phase": "P"},
        {"type": "started", "agentId": "new", "label": "X", "phase": "P"},
    )
    h.jl(
        run / "agent-old.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=29)},
        {"type": "user", "timestamp": h.ago(minutes=25)},
    )
    h.jl(
        run / "agent-new.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=19)},
        h.assistant(h.ago(seconds=10), "m1", [h.tool("Read", file_path="/a/b.py")]),
    )
    r = model.Run(run)
    r.poll(model.load_sessions())
    assert r.agents["old"].superseded
    out = render(Painter(r).snapshot(height=40), 130)
    assert "1 running · 1 replaced" in out and "↺ replaced" in out and "↺ 4:00" in out
    assert "agents [0/1 done]" in out and "interrupted" not in out
    out = render(Painter(r, view="cards").snapshot(), 130)
    assert "0/1 done" in out and "↺ 4:00" in out


# ---- cards ----


def test_cards_follow_the_running_phase_and_scroll(claude_home):
    h = claude_home
    h.session("s-run")
    phases = [f"Этап {i}" for i in range(1, 11)]
    agents = [(f"Исп {i}.{j}", phases[i - 1], "done") for i in range(1, 7) for j in (1, 2)]
    agents += [(f"Исп 7.{j}", "Этап 7", "running") for j in (1, 2)]
    d = run_with(h, "wf_many", phases, agents)
    app = App(tracker(), expand_all=False, view="cards")
    app.open(d)

    def frame() -> str:
        text = render(app.frame(30), 100, 30)
        assert frame_problems(text, 100) == [], text
        return text

    text = frame()
    assert "7. Этап 7" in text and "Исп 7.1" in text and "more – PgUp" in text  # anchored to the running phase
    app.on_key("home")
    text = frame()
    assert "1. Этап 1" in text and "– PgUp" not in text and re.search(r"↓ \d+ more – PgDn", text)
    app.on_key("end")
    text = frame()
    assert "10. Этап 10" in text and "– PgDn" not in text
    app.on_key("f")
    assert "Исп 7.1" in frame()
    app.on_key("up")  # in cards, ↑↓ pick the phase to show
    text = frame()
    assert "Исп 6.1" in text and app.current().phase_sel == 5


def phase_box(text: str) -> str:
    """The expanded phase of the cards view (between its heavy frame lines): the log is left out."""
    lines = text.split("\n")
    top = next(i for i, ln in enumerate(lines) if "┏" in ln)
    bottom = next(i for i, ln in enumerate(lines) if "┗" in ln and i > top)
    return "\n".join(lines[top : bottom + 1])


def test_a_phase_taller_than_the_screen_shows_or_counts_every_agent(claude_home):
    """Cards for the most important agents, a line for each of the others; "+N" only for those that
    really do not fit, and then the numbers add up."""
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_wide", ["Волна"], [(f"Агент {i}", "Волна", "running" if i % 2 else "done") for i in range(20)])
    app = App(tracker(), expand_all=False, view="cards")
    app.open(d)
    for w, hh, all_seen in ((80, 24, False), (120, 40, True)):
        text = render(app.frame(hh), w, hh)
        assert frame_problems(text, w) == [], text
        box = phase_box(text)
        seen = {i for i in range(20) if re.search(rf"Агент {i}\b", box)}
        more = re.search(r"\+(\d+) more", box)
        assert len(seen) + (int(more[1]) if more else 0) == 20, box
        assert (len(seen) == 20) == all_seen, box
        assert all(i in seen for i in range(1, 20, 2)[: min(len(seen), 10)]), box  # running ones first
    assert all(f"Агент {i}" in render(app.snapshot(), 120) for i in range(20))  # --once: everything


@pytest.mark.parametrize("size", [(80, 24), (100, 24), (120, 24), (80, 30), (70, 15), (100, 15)])
def test_cards_of_the_running_phase_are_all_seen(claude_home, size):
    """The common 80x24 terminal: a running phase of four agents shows every one of them (two cards
    in a row, the others a line each), never a lone "+4 more" card."""
    h = claude_home
    h.session("s-run")
    agents = [("Survey", "Survey", "done")] + [(f"Fixer {i}", "Fix", "done" if i == 3 else "running") for i in range(4)]
    d = run_with(h, "wf_fix", ["Survey", "Fix", "Review"], agents)
    app = App(tracker(), expand_all=False, view="cards")
    app.open(d)
    w, hh = size
    text = render(app.frame(hh), w, hh)
    assert frame_problems(text, w) == [], text
    box = phase_box(text)
    assert all(f"Fixer {i}" in box for i in range(4)), box
    assert "more" not in box, box
    if size == (80, 24):
        assert any(ln.count("╭─") == 2 for ln in box.split("\n")), box  # two cards side by side


# ---- sessions ----


def test_session_subagents_beyond_the_fan_are_reachable(claude_home):
    h = claude_home
    h.session("s-work", status="busy", name="worker")
    h.transcript("s-work", h.assistant(h.ago(seconds=30), "w1", [h.tool("Read", file_path="/a/b.py")]))
    for i in range(14):
        subagent(h, "s-work", f"x{i:02d}", f"Explore {i:02d}")
    app = App(tracker(), expand_all=False)
    app.open("s-work")
    text = render(app.frame(40), 120, 40)
    assert re.search(r"\+\d+ more", text) and "all – key v" in text
    app.on_key("v")
    seen = set()
    for _ in range(6):
        text = render(app.frame(30), 120, 30)
        assert frame_problems(text, 120) == [], text
        seen |= {i for i in range(14) if f"Explore {i:02d}" in text}
        app.on_key("pgdn")
    assert seen == set(range(14))


def test_background_commands_are_shown(busy_home):
    app = App(tracker(), expand_all=False)
    text = render(app.snapshot(), 140)
    assert "◐ background task" in text and "○ idle" not in text
    assert "⚙ background: Run the test suite" in text and "now python3 96%" in text
    assert "«tests/test_a.py ....  [ 63%]»" in text
    app.open("s-bg")
    text = render(app.frame(40), 140, 40)
    assert (
        "background tasks" in text and "Build the wheel · exit code 0 · 1:00 ago" in text and "bg [1 running]" in text
    )
    assert "BACKGROUND TASK  5:" in text  # how long the command runs, not how long the session is idle
    app.on_key("v")
    assert "Run the test suite" in render(app.frame(40), 140, 40)


def test_background_command_of_a_workflow_owner(busy_home):
    h, _ = busy_home
    bg_command(h, "sess-1", "bgO", "Download the corpus", 3, project="-proj")  # the session running the demo workflow
    app = App(tracker(), expand_all=False)
    text = render(app.snapshot(), 140)
    block = text[text.index("◆ demo-session") :]
    assert "runs workflow 1" in block and "⚙ background: Download the corpus" in block


# ---- key bar and tabs ----


def test_key_bar_keeps_quit_when_narrow():
    bar = keybar("detail", True, False, "graph", manual=True)
    assert cell_len(bar.plain) > 80
    for width in (80, 60, 40, 24):
        t = bar.fit(width)
        assert t.cell_len <= width and " q " in t.plain, (width, t.plain)
    assert "quit" in bar.fit(80).plain and "o  overview" in bar.fit(80).plain
    assert KeyBar([("q", "quit", 0)]).fit(5).plain.strip() == "q"


def test_selected_tab_is_always_visible(claude_home):
    h = claude_home
    h.session("s-many", status="idle", since_ms=h.ms_ago(hours=2))
    for i in range(15):
        h.run("s-many", f"wf_{i:02d}", h.ago(minutes=60 - i))
    tr = tracker()
    runs = tr.visible()
    line = tabs_line(runs, runs[11].run_dir)
    for width in (60, 100):
        t = line.fit(width)
        assert t.cell_len <= width and "12 flow-wf_11" in t.plain, t.plain
    assert "‹" in line.fit(60).plain
    assert line.fit(1000).plain == line.plain  # room enough: every tab in full


def test_states_are_not_sliced_strings():
    st = State("◷", "waiting for you", "yellow")
    assert st.text == "◷ waiting for you" and st.icon == "◷" and st.word == "waiting for you"


# ---- counts, still frames, status lines ----


def agents_with(*statuses):
    at = model.now()
    return [
        model.Agent(f"a{i}", f"A{i}", "W", model.Tail(model.CLAUDE_DIR / "none"), status=st, started=at)
        for i, st in enumerate(statuses)
    ]


def test_errors_come_before_done_in_what_did_not_fit():
    """When a "+N" card has no room for every count, it is "done" that is cut, never "failed";
    the "+N" line of a phase lists failed and interrupted agents before the finished ones."""
    rest = agents_with(*[model.RUNNING] * 2, *[model.DONE] * 3, *[model.FAILED] * 3, model.STALE)
    c = Canvas(30, 6)  # the diagram's card: "+N", two lines of counts, the hint
    Diagram(theme.AgentColors(), model.now(), 0.0).more_box(c, rest, [], 0, 0, 30, 6)
    text = render(c.to_text(), 30)
    assert "+9 more" in text and "2 running" in text and "3 failed" in text and "done" not in text, text
    text = render(Painter(None).agent_list(rest, model.now(), 0.0, rows=2), 80)
    assert "+8 more · 1 running · 3 failed · 1 interrupted · 3 done" in text, text


def test_failed_agents_are_counted_in_the_overview(claude_home):
    h = claude_home
    h.session("s-run")
    run_with(h, "wf_bad", ["W"], [("A", "W", "done"), ("B", "W", "failed")])
    text = render(App(tracker(), expand_all=False).snapshot(), 120)
    assert "0 running" not in text
    assert text.count("1 done · 1 failed") == 2, text  # the subtitle of the block and the line under the log


def busy_screens(app: App, run):
    for which in ("overview", "graph", "cards", "session", "session-cards", "background"):
        if app.view == "cards":
            app.on_key("v")
        open_screen(app, run, which)
        yield which


def test_still_frames_have_no_spinners(busy_home):
    """--once and the screenshots: ● for what runs (as the README says), ◐ only for "background task"."""
    _, run = busy_home
    app = App(tracker(), expand_all=False)
    for which in busy_screens(app, run):
        text = render(app.snapshot(), 140)
        assert not set("◓◑◒") & set(text), which
        # the icon of the state, with its word or on the tab of that session ("builder" runs a command)
        lines = text.lower().split("\n")  # the title of the session screen is in capitals
        odd = [ln for ln in lines if ln.count("◐") > ln.count("◐ background task") + ln.count("builder ◐")]
        assert odd == [], (which, odd)
    app.mode = "overview"
    assert set("◐◓◑◒") & set(render(app.frame(40), 140, 40))  # the live view spins


def test_status_line_keeps_progress_and_phase_when_narrow(claude_home):
    h = claude_home
    h.session("s-run")
    d = run_with(h, "wf_m", ["W", "X"], [(f"A{i}", "W", "running") for i in range(3)])
    for i, m in enumerate(("claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-4-5")):
        h.jl(d / f"agent-a{i}.jsonl", h.assistant(h.ago(seconds=20), f"z{i}", [h.text("…")], model_id=m))
    app = App(tracker(), expand_all=False)
    app.open(d)
    for lang in ("en", "ru"):
        i18n.use_lang(lang)
        for view in ("graph", "cards"):
            if app.view != view:
                app.on_key("v")
            line = render(app.frame(24), 80, 24).split("\n")[-2]
            agents, phase = ("agents [", "phase [1/2]") if lang == "en" else ("агенты [", "этап [1/2]")
            assert agents in line and phase in line and "…" not in line, line
            assert ("3 models [3 running]" if lang == "en" else "3 модели [3 идут]") in line, line
    i18n.use_lang("en")
    line = render(app.frame(40), 200, 40).split("\n")[-2]
    assert "haiku 4.5 [1 running]" in line and "opus 5.5 [1 running]" in line and "out [" in line, line
