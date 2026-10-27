import io
import json
import os
from datetime import timedelta

import pytest
from rich.console import Console

from wfwatch import cli, model
from wfwatch.cli import App, choose_start
from wfwatch.model import Action


def render(renderable, width=120, height=None) -> str:
    con = Console(record=True, width=width, height=height, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


@pytest.fixture
def home(claude_home):
    for sid in ("s-web", "s-api"):
        claude_home.session(sid, status="idle", since_ms=claude_home.ms_ago(hours=2), cwd=f"/home/user/dev/{sid[2:]}")
    return claude_home


def test_tracker_shows_running_and_recent_runs_from_all_projects(home):
    h = home
    a = h.run("s-web", "wf_a", h.ago(minutes=20), project="-p1")
    b = h.run("s-api", "wf_b", h.ago(minutes=10), project="-p2")
    c = h.run("s-api", "wf_c", h.ago(hours=2), finished=h.ago(minutes=5), project="-p2")
    h.run("s-api", "wf_old", h.ago(hours=2), finished=h.ago(hours=1), project="-p2")
    tr = model.Tracker()
    assert len(tr.scan()) == 4
    vis = tr.visible()
    assert [r.run_dir for r in vis] == [c, a, b]  # by start time; the long-finished one is hidden
    assert [r.state for r in vis] == ["completed", "running", "running"]
    assert [r.project for r in vis] == ["api", "web", "api"]
    assert tr.scan() == []  # nothing new on a second scan


def test_runs_of_a_session_share_one_transcript_read(home):
    h = home
    runs = [h.run("s-api", f"wf_{i}", h.ago(minutes=10 - i)) for i in range(3)]
    tr = model.Tracker()
    tr.scan()
    assert len(tr.logs) == 1
    assert len({id(tr.runs[d].log) for d in runs}) == 1
    assert all(len(tr.runs[d].launches) == 1 for d in runs)


def test_project_falls_back_to_agent_cwd_when_session_gone(home):
    run = home.run("s-gone", "wf_x", project="-home-user-dev-thing", cwd="/home/user/dev/thing_x")
    r = model.Run(run)
    r.poll(model.load_sessions())
    assert r.project == "thing_x"


def test_app_keys(home):
    h = home
    a = h.run("s-web", "wf_a", h.ago(minutes=20), project="-p1")
    b = h.run("s-api", "wf_b", h.ago(minutes=19), project="-p2")
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    assert app.mode == "overview"
    assert app.on_key("2") and (app.mode, app.selected) == ("detail", b)
    app.on_key("tab")
    assert app.selected == a  # wraps around
    app.on_key("left")
    assert app.selected == b
    app.on_key("9")  # no such item: nothing changes
    assert app.selected == b
    app.on_key("щ")  # "o" in the Russian layout
    assert app.mode == "overview"
    app.on_key("a")
    assert app.expand_all
    assert not app.on_key("q")

    out = render(app.snapshot())
    for s in ("overview", "flow-wf_a", "flow-wf_b", "web", "name-s-api", "агент wf_b"):
        assert s in out, s
    app.on_key("1")
    assert "2 flow-wf_b" in render(app.snapshot())  # tabs of the other runs


def test_unicode_digits_and_unknown_keys_are_ignored(home):
    h = home
    h.run("s-web", "wf_a")
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    for k in ("²", "٣", "pgup", "pgdn", "home", "end", "[", "~"):
        assert app.on_key(k)
    assert app.mode == "overview"


def test_two_digit_numbers(claude_home):
    h = claude_home
    h.session("s-many", status="idle", since_ms=h.ms_ago(hours=2))
    for i in range(12):
        h.run("s-many", f"wf_{i:02d}", h.ago(minutes=60 - i))
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    items = app.items()
    assert len(items) == 12
    app.on_key("1", at=100.0)
    assert app.selected == items[0]  # opens at once
    app.on_key("2", at=100.3)
    assert app.selected == items[11]  # "1", "2" within 0.7 s -> 12
    app.on_key("2", at=105.0)
    assert app.selected == items[1]  # a lone digit later
    app.on_key("1", at=110.0)
    app.on_key("0", at=110.2)
    assert app.selected == items[9]  # 10
    app.on_key("0", at=120.0)
    assert app.mode == "overview"  # a lone 0 is the overview
    app.on_key("3", at=130.0)
    app.on_key("4", at=130.1)  # 34 does not exist: 4 on its own
    assert app.selected == items[3]


def test_pinned_run_is_not_switched_by_new_runs(home):
    h = home
    a = h.run("s-web", "wf_a", h.ago(minutes=30), finished=h.ago(minutes=25))
    tr = model.Tracker(extra=a)
    tr.scan()
    app = App(tr, expand_all=False, pinned=True)
    app.open(a)
    h.session("s-web", status="busy")
    h.run("s-web", "wf_new", h.ago(minutes=5))
    app.on_new(tr.scan())
    assert app.selected == a
    app.pinned = False
    h.run("s-web", "wf_new2", h.ago(minutes=4))
    app.on_new(tr.scan())
    assert app.selected.name == "wf_new2"


def test_on_new_switches_only_to_running_runs(home):
    h = home
    a = h.run("s-web", "wf_a", h.ago(minutes=30), finished=h.ago(minutes=25))
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    app.open(a)
    h.run("s-api", "wf_done", h.ago(minutes=20), finished=h.ago(minutes=1))
    app.on_new(tr.scan())
    assert app.selected == a


def test_painter_follows_a_replaced_run_object(home):
    h = home
    d = h.run("s-web", "wf_a")
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    app.open(d)
    old = app.painter(tr.runs[d])
    old.phase_sel = 0
    fresh = model.Run(d)  # the run was dropped and found again (e.g. resumed the next day)
    fresh.poll(tr.sessions)
    tr.runs[d] = fresh
    p = app.painter(fresh)
    assert p is not old and p.run is fresh and p.phase_sel == 0
    w_old = model.SessionWatch("s-web", d, tr.sessions["s-web"])
    w_new = model.SessionWatch("s-web", d, tr.sessions["s-web"])
    assert app.session_painter(w_old).s is w_old and app.session_painter(w_new).s is w_new


def test_vanished_items_are_forgotten(home):
    h = home
    d = h.run("s-web", "wf_a")
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    app.open(d)
    render(app.snapshot())
    assert d in app.painters
    del tr.runs[d]
    render(app.snapshot())
    assert d not in app.painters
    assert (app.mode, app.selected) == ("overview", None)  # the selected item is gone: overview, explicitly
    h.run("s-api", "wf_b")
    app.on_new(tr.scan())
    assert app.mode == "overview"  # a new run does not pull the overview into a detailed view


def test_start_opens_the_single_workflow_even_with_its_own_session(claude_home):
    h = claude_home
    h.session("s-own", status="busy")
    h.transcript("s-own", h.assistant(h.ago(seconds=20), "m1", [h.tool("Read", file_path="/a/b.py")]))
    d = h.run("s-own", "wf_one")
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    assert [w.session_id for w in tr.active_sessions()] == ["s-own"]
    app = App(tr, expand_all=False)
    choose_start(app)
    assert (app.mode, app.selected) == ("detail", d)

    h.session("s-other", status="busy")  # another working session: overview
    h.transcript("s-other", h.assistant(h.ago(seconds=20), "m1", [h.tool("Read", file_path="/a/b.py")]))
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    choose_start(app)
    assert app.mode == "overview"
    app = App(tr, expand_all=False)
    choose_start(app, overview=True)
    assert app.mode == "overview"


def test_start_shows_the_latest_run_when_nothing_goes(claude_home):
    h = claude_home
    d = h.run("s-gone", "wf_old", h.ago(hours=10), finished=h.ago(hours=9))
    for f in d.iterdir():
        os.utime(f, (model.now().timestamp() - 9 * 3600,) * 2)
    os.utime(d, (model.now().timestamp() - 9 * 3600,) * 2)
    tr = model.Tracker()
    tr.scan()
    assert tr.visible() == []
    app = App(tr, expand_all=False)
    choose_start(app)
    assert (app.mode, app.selected) == ("detail", d) and tr.extra == d


def test_animating(home):
    h = home
    done = h.run("s-web", "wf_done", h.ago(minutes=20), finished=h.ago(minutes=2))
    tr = model.Tracker()
    tr.scan()
    app = App(tr, expand_all=False)
    assert not app.animating()  # only a finished run: redraw rarely
    app.open(done)
    assert not app.animating()
    running = h.run("s-api", "wf_live")
    tr.scan()
    app.open(running)
    assert app.animating()
    app.mode = "overview"
    assert app.animating()


def test_keybar_shows_only_keys_of_current_screen():
    from wfwatch.render import keybar

    def bar(*a, **kw):
        return " ".join(keybar(*a, **kw).plain.split())

    assert bar("overview", True, False) == "1–9 open Tab next q quit"
    assert bar("detail", True, False) == "1–9 go to Tab next o overview ↑↓ phase v cards q quit"
    assert bar("detail", False, False, manual=True) == "o overview ↑↓ phase f follow v cards q quit"
    assert bar("detail", False, False, view="cards") == "o overview ↑↓ phase PgUp PgDn scroll v diagram a expand q quit"
    assert "a collapse" in bar("detail", False, True, view="cards")
    assert bar("session", False, False) == "o overview v cards q quit"
    assert (
        bar("session", True, False, view="cards") == "1–9 go to Tab next o overview PgUp PgDn scroll v diagram q quit"
    )


def test_live_frames_render(demo_run):
    """The full-screen path (Layout with a fixed height), not only --once snapshots."""
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    app = App(tr, expand_all=False)
    for setup in (lambda: None, lambda: app.on_key("1"), lambda: app.on_key("v"), lambda: app.on_key("2")):
        setup()
        out = render(app.frame(30), width=120, height=30)
        assert "Traceback" not in out and "render error" not in out and "read error" not in out
        assert len(out.splitlines()) == 30


def test_sessions_without_workflow(claude_home):
    h = claude_home

    def session(sid, status, rows, since_ms=None):
        h.session(
            sid,
            status=status,
            cwd="/home/user/dev/demo_app",
            entrypoint="claude-vscode",
            since_ms=since_ms if since_ms is not None else h.ms_ago(hours=1),
        )
        return h.transcript(sid, *rows)

    def tool_at(ago):
        return h.assistant(h.iso(model.now() - ago), "m1", [h.tool("Bash", description="Run tests")])

    tool = tool_at(timedelta(minutes=1))
    session("s-work", "busy", [tool])
    h.jl(h.root / "projects" / "-p" / "s-work" / "subagents" / "agent-x1.jsonl", tool)
    (h.root / "projects" / "-p" / "s-work" / "subagents" / "agent-x1.meta.json").write_text(
        json.dumps({"description": "Explore modules"}), encoding="utf-8"
    )
    session("s-ask", "waiting", [tool])
    stale = session("s-stale", "busy", [tool_at(timedelta(hours=2))])  # "busy", but silent for two hours
    h.jl(stale, {"type": "continued-in"})  # a service line without a time refreshes the file, not the history
    session("s-idle", "idle", [tool])  # went idle long ago: not shown
    session("s-just", "idle", [tool], since_ms=h.ms_ago(minutes=5))  # just went idle
    h.run("s-flow", "wf_f")
    session("s-flow", "busy", [tool])  # runs a workflow: shown as a session too, it works alongside the run

    tr = model.Tracker()
    tr.scan()
    tr.poll()
    names = [w.name for w in tr.active_sessions()]
    assert names == ["name-s-ask", "name-s-flow", "name-s-just", "name-s-work"]
    work = next(w for w in tr.active_sessions() if w.name == "name-s-work")
    assert work.main.last_action == Action("Bash", "Run tests") and work.project == "demo_app"
    assert [a.label for a in work.running_subagents()] == ["Explore modules"]

    app = App(tr, expand_all=False)
    out = render(app.snapshot())
    for s in (
        "2  ◆ name-s-ask",
        "3  ◆ name-s-flow",
        "4  ◆ name-s-just",
        "5  ◆ name-s-work",
        "VSCode",
        "Bash Run tests",
        "Explore modules",
        "waiting for you",
        "○ idle",
        "runs workflow 1",
        "sessions: 2 working · 1 waiting for you · 1 idle",
    ):
        assert s in out, s

    # a digit opens a session; numbers are shared: 1 – the workflow, 2–5 – sessions
    assert app.on_key("5") and (app.mode, app.selected) == ("detail", "s-work")
    out = render(app.snapshot(), width=130)
    for s in (
        "NAME-S-WORK",
        "latest actions",
        "◆ name-s-work · session",
        "session subagents",
        "Explore modules",
        "1 flow-wf_f",
        "2 ◆ name-s-ask",
        "5 ◆ name-s-work",
        "subagents [",
        "ctx [",
    ):
        assert s in out, s
    app.on_key("tab")
    assert app.selected == tr.visible()[0].run_dir  # wraps: after the last session – the first workflow
    app.on_key("shift-tab")
    assert app.selected == "s-work"

    # the session went idle: the detailed view stays, the overview no longer lists it
    reg = h.root / "sessions" / "s-work.json"
    reg.write_text(json.dumps({**json.loads(reg.read_text(encoding="utf-8")), "status": "idle"}), encoding="utf-8")
    tr.scan()
    tr.poll()
    assert "s-work" in tr.watches and [w.name for w in tr.active_sessions()] == [
        "name-s-ask",
        "name-s-flow",
        "name-s-just",
    ]
    assert "IDLE" in render(app.snapshot(), width=130)


def test_watch_survives_one_missing_scan(claude_home):
    h = claude_home
    reg = h.session("s-work", status="busy")
    h.transcript("s-work", h.assistant(h.ago(seconds=20), "m1", [h.tool("Read", file_path="/a/b.py")]))
    tr = model.Tracker()
    tr.scan()
    w = tr.watches["s-work"]
    good = reg.read_text(encoding="utf-8")
    reg.unlink()  # gone for one scan
    tr.scan()
    assert tr.watches.get("s-work") is w
    reg.write_text(good, encoding="utf-8")
    tr.scan()
    assert tr.watches.get("s-work") is w
    reg.unlink()
    tr.scan()
    tr.scan()  # gone for two scans in a row: the session has ended
    assert "s-work" not in tr.watches


def test_list_keeps_state_agents_and_time_on_80_columns(claude_home, monkeypatch):
    h = claude_home
    h.session(
        "s-long",
        name="a-very-long-session-name-that-does-not-fit",
        cwd="/home/user/dev/a-very-long-project-directory-name",
    )
    sess = h.root / "projects" / "-p" / "s-long"
    run = h.run("s-long", "wf_1234abcd-100", h.ago(hours=1, minutes=3))
    h.jl(
        sess.with_suffix(".jsonl"),
        h.launch(run.name, "t-x", h.ago(hours=1, minutes=3), name="an-extremely-long-workflow-name-x"),
    )
    h.run("s-long", "wf_1234abcd-200", h.ago(minutes=50), finished=h.ago(minutes=1))
    for width in (80, 100):
        out = render_list(width)
        rows = [ln for ln in out.splitlines() if ln.startswith("wf_")]
        assert len(rows) == 2, out
        assert all(len(ln.rstrip()) <= width for ln in out.splitlines())
        assert "● running" in rows[1] or "● running" in rows[0]
        assert any("✓ completed" in r for r in rows)
        assert all(r.rstrip().endswith(("1:03:00", "1:03:01", "49:00", "49:01")) for r in rows), rows


def render_list(width: int) -> str:
    con = Console(record=True, width=width, file=io.StringIO())
    cli.list_runs(con, 10)
    return con.export_text()


def test_list_does_not_parse_agent_transcripts(demo_run, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("--list must not parse agent transcripts")

    monkeypatch.setattr(model, "feed_agent", boom)
    out = render_list(120)
    assert "wf_abc-123" in out and "demo-flow" in out and "● running" in out and " 2 " in out
