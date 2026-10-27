import io
import os
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from wfwatch import model
from wfwatch.model import Action
from wfwatch.render import Painter
from wfwatch.widgets import action_text, event_text


def render(renderable, width=130) -> str:
    con = Console(record=True, width=width, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


# ---- workflow script meta ----


def test_parse_meta(demo_script):
    name, desc, phases = model.parse_meta(demo_script)
    assert (name, desc, phases) == ("demo-flow", "Демо: два этапа", ["Аналитика", "Ревью", "Доработка"])


@pytest.mark.parametrize(
    ("js", "expected"),
    [
        # one line: the code after the object must not add phases
        (
            (
                "export const meta = { name: 'x', phases: [{ title: 'A' }, { title: 'B' }] }\n"
                "const r = await agent('p', { title: 'not a phase' })\nfunction f() {\n}\n"
            ),
            ("x", "", ["A", "B"]),
        ),
        (
            (
                "export const meta = {\n  name: 'x',\n  phases: [{ title: 'A' }],\n  };\n"
                "async function run() {\n  await agent('p', { title: 'not a phase' })\n}\n"
            ),
            ("x", "", ["A"]),
        ),
        (
            "export const meta = {\n  name: 'x',\n}\nconst steps = { phases: [{ title: 'not a phase' }] }\n",
            ("x", "", []),
        ),
        ("export const meta = {\n  name: 'it\\'s',\n  phases: [{ title: 'A \\'q\\'' }],\n}\n", ("it's", "", ["A 'q'"])),
        ("export const meta = {\n  description: 'name: fake',\n  name: 'real',\n}\n", ("real", "name: fake", [])),
        (
            "export const meta = {\n  name: 'x',\n  phases: [{ title: 'A', detail: \"see title: 'B'\" }],\n}\n",
            ("x", "", ["A"]),
        ),
        ("export const meta = {\n  name: 'x',\n  phases: [{ title: 'A', subtitle: 'B' }],\n}\n", ("x", "", ["A"])),
        (
            (
                "export const meta: Meta = { name: `t-${1 + 1}`, // comment\n"
                " /* c */ phases: ['S1', { title: \"T\\u00e9\" }, ...more],"
                " fn() { return { title: 'no' } }, [k]: 2 }"
            ),
            ("t-${1 + 1}", "", ["S1", "Té"]),
        ),
        ("export const meta = { ) }", ("", "", [])),
        ("export const meta = { name: 'x', phases: [{ title: 'A' ", ("", "", [])),
        ("const meta = { name: 'not exported' }", ("", "", [])),
    ],
)
def test_parse_meta_variants(js, expected):
    assert model.parse_meta(js) == expected


# ---- primitives ----


def test_tail_reads_only_complete_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nnull\n[1]\n"s"\n{"b"', encoding="utf-8")
    t = model.Tail(p)
    assert t.read() == [{"a": 1}]  # only objects
    with open(p, "a", encoding="utf-8") as f:
        f.write(": 2}\n")
    assert t.read() == [{"b": 2}]
    assert t.read() == []


def test_tail_notices_truncation_and_replacement(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    t = model.Tail(p)
    assert len(t.read()) == 2 and not t.reset
    p.write_text('{"a": 3}\n', encoding="utf-8")  # truncated
    assert t.read() == [{"a": 3}] and t.reset
    t.reset = False
    q = tmp_path / "y.jsonl"
    q.write_text('{"a": 4}\n{"a": 5}\n{"a": 6}\n', encoding="utf-8")
    os.replace(q, p)  # replaced by a bigger file (new inode)
    assert t.read() == [{"a": 4}, {"a": 5}, {"a": 6}] and t.reset


def test_tail_missing_and_tail_only(tmp_path):
    assert model.Tail(tmp_path / "nope.jsonl").read() == []
    p = tmp_path / "big.jsonl"
    p.write_text("".join(f'{{"n": {i}}}\n' for i in range(1000)), encoding="utf-8")
    got = model.Tail(p, last_bytes=100).read()
    assert got and got[-1] == {"n": 999} and len(got) < 20  # only the tail; its cut first line is skipped


def test_tail_needles_and_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(model.Tail, "CHUNK", 7)  # lines span many chunks
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"x":"skip"}\n{"type":"api-request-blob","k":"NEEDLE"}\n{"y":"NEEDLE 1"}\n{"z":"NEEDLE 2"}\n{"w"')
    t = model.Tail(p, needles=(b"NEEDLE",), skip_prefixes=(b'{"type":"api-request-',))
    assert list(t.lines()) == [b'{"y":"NEEDLE 1"}', b'{"z":"NEEDLE 2"}']


@pytest.mark.parametrize(
    ("s", "ok"),
    [
        ("2026-10-27T10:00:00.000Z", True),
        ("2026-10-27T10:00:00Z", True),
        ("2026-10-27T10:00:00", True),
        ("2026-10-27T10:00:00.123456789+03:00", True),
        ("2026-10-27T10:00:00.5Z", True),
        (1792795104909, False),
        (None, False),
        ("", False),
        ("soon", False),
        (["x"], False),
        ("�", False),
    ],
)
def test_parse_ts(s, ok):
    ts = model.parse_ts(s)
    assert (ts is not None) == ok
    if ts:
        assert ts.tzinfo is not None


def test_parse_ts_naive_is_utc():
    assert model.parse_ts("2026-10-27T10:00:00") == model.parse_ts("2026-10-27T10:00:00Z")
    assert model.parse_ts("2026-10-27T10:00:00").tzinfo == timezone.utc


def test_describe_tool():
    assert model.describe_tool("Edit", {"file_path": "/a/b/c/d.py"}) == Action("Edit", "c/d.py")
    assert model.describe_tool("Bash", {"command": "ls -la", "description": ""}) == Action("Bash", "ls -la")
    assert model.describe_tool("mcp__srv__do_thing", {}) == Action("do_thing")
    assert model.describe_tool("Task", {"description": "Explore"}) == Action("Agent", "Explore")
    assert model.describe_tool(5, "x") == Action("5")
    assert model.describe_tool("Read", {"file_path": ["x"]}) == Action("Read")


def test_actions_in_words():
    """Tool names are shown as they are; the few tools shown as a phrase are translated."""
    assert action_text(Action("Read", "a/b.py")) == "Read a/b.py" and action_text(Action("Read")) == "Read"
    assert action_text(Action("WebSearch", "rich cells")) == "Search rich cells"
    assert action_text(Action("StructuredOutput")) == "returning the result"
    assert action_text(Action("", "Done, see below")) == "✎ Done, see below"
    assert action_text(None) == "" and action_text(Action()) == ""


# ---- runs ----


def test_running_run(demo_run):
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    assert run.name == "demo-flow"
    assert run.session.name == "demo-session" and run.session_name == "demo-session"
    assert run.state == "running"
    a1, a2 = run.agents["a1"], run.agents["a2"]
    assert (a1.status, a1.result, a1.tool_calls) == (model.DONE, "Сценарии готовы\nподробности", 1)
    assert a1.output_tokens == 60 and a1.context_tokens == 1005
    assert (a2.status, a2.tool_calls, a2.last_action) == (model.RUNNING, 2, Action("Grep", "TODO"))
    assert a2.output_tokens == 10  # one message written as two lines
    assert [p.title for p in run.phases()] == ["Аналитика", "Ревью", "Доработка"]
    assert [p.status for p in run.phases()] == [model.RUNNING, model.PENDING, model.PENDING]
    kinds = [e.kind for e in run.events]
    assert kinds[0] == "run" and "done" in kinds and kinds.count("tool") == 3
    assert [e.ts for e in run.events] == sorted(e.ts for e in run.events)


def test_completion_marks_unfinished_agents_stale(demo_run):
    h, sess, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    t_end = h.at(minutes=5)
    h.jl(sess.with_suffix(".jsonl"), h.notify("t1", "completed", h.iso(t_end)))
    run.poll()
    assert run.state == "completed"
    assert run.agents["a2"].status == model.STALE
    assert abs((run.ended - t_end).total_seconds()) < 1


def test_resume_reopens_run(demo_run):
    h, sess, run_dir = demo_run
    h.jl(
        sess.with_suffix(".jsonl"),
        h.notify("t1", "completed", h.ago(minutes=5)),
        h.launch("wf_abc-123", "t2", h.ago(minutes=4)),
    )
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    assert run.state == "running"
    run_events = [e for e in run.events if e.kind == "run"]
    assert [(e.code, e.detail) for e in run_events] == [("launched", ""), ("finished", "completed"), ("relaunched", "")]
    assert [event_text(e) for e in run_events] == ["workflow launched", "workflow completed", "workflow resumed"]


def test_dead_session_without_notification_is_stopped(demo_run):
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll({})
    assert run.state == "stopped"
    assert run.agents["a2"].status == model.STALE


def test_stale_is_not_sticky(demo_run):
    """A registry record missing for one scan must not mark working agents interrupted for good."""
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    sessions = model.load_sessions()
    run.poll({})  # the session is not in the registry right now
    assert run.agents["a2"].status == model.STALE
    run.poll(sessions)  # back
    assert run.state == "running" and run.agents["a2"].status == model.RUNNING


def test_registry_misread_keeps_last_good_record(demo_run):
    h, _, _ = demo_run
    cache = {}
    assert model.load_sessions(cache=cache)["sess-1"].alive
    reg = next((h.root / "sessions").glob("*.json"))
    good = reg.read_text(encoding="utf-8")
    reg.write_text(good[: len(good) // 2], encoding="utf-8")  # read in the middle of a rewrite
    assert model.load_sessions(cache=cache)["sess-1"].name == "demo-session"
    assert "sess-1" not in model.load_sessions()  # without the cache the record is just unreadable


def resumed_run(h, failed_first=False, parallel=False):
    """Agent X starts and is interrupted (or fails); a new attempt of X finishes."""
    h.session("s-r")
    sess = h.root / "projects" / "-p" / "s-r"
    run = sess / "subagents" / "workflows" / "wf_res"
    h.jl(sess.with_suffix(".jsonl"), h.launch("wf_res", "t1", h.ago(minutes=30)))
    h.jl(
        run / "journal.jsonl",
        {"type": "launched"},
        {"type": "started", "agentId": "old", "label": "X", "phase": "P"},
        {"type": "started", "agentId": "keep", "label": "Y", "phase": "P"},
        {"type": "result", "agentId": "keep", "result": "ok"},
    )
    h.jl(
        run / "agent-old.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=29)},
        {"type": "user", "timestamp": h.ago(minutes=25)},
    )
    h.jl(run / "agent-keep.jsonl", {"type": "user", "timestamp": h.ago(minutes=29)})
    if failed_first:
        h.jl(
            run / "journal.jsonl",
            {"type": "failed", "agentId": "old"},
            {"type": "started", "agentId": "new", "label": "X", "phase": "P"},
            {"type": "result", "agentId": "new", "result": "ok"},
        )
        h.jl(
            run / "agent-new.jsonl", {"type": "user", "timestamp": h.ago(minutes=27) if parallel else h.ago(minutes=20)}
        )
    else:
        h.jl(
            sess.with_suffix(".jsonl"),
            h.notify("t1", "killed", h.ago(minutes=24)),
            h.launch("wf_res", "t2", h.ago(minutes=20)),
        )
        h.jl(
            run / "journal.jsonl",
            {"type": "started", "agentId": "new", "label": "X", "phase": "P"},
            {"type": "result", "agentId": "new", "result": "ok"},
        )
        # a resumed agent may start a moment before its launch record is written
        h.jl(run / "agent-new.jsonl", {"type": "user", "timestamp": h.ago(minutes=20, seconds=0.3)})
        h.jl(sess.with_suffix(".jsonl"), h.notify("t2", "completed", h.ago(minutes=10)))
    r = model.Run(run)
    r.poll(model.load_sessions())
    return r


def test_resume_supersedes_left_behind_agents(claude_home):
    r = resumed_run(claude_home)
    assert r.state == "completed"
    old, new = r.agents["old"], r.agents["new"]
    assert old.superseded and old.status == model.STALE and not new.superseded
    assert [p.status for p in r.phases()] == [model.DONE]
    assert {a.agent_id for a in r.current_agents()} == {"keep", "new"}
    out = render(Painter(r).snapshot())
    assert "interrupted [" not in out and "agents [2/2 done]" in out


def test_resumed_agents_are_not_superseded_while_running(claude_home):
    h = claude_home
    h.session("s-r")
    sess = h.root / "projects" / "-p" / "s-r"
    run = sess / "subagents" / "workflows" / "wf_res"
    h.jl(
        sess.with_suffix(".jsonl"),
        h.launch("wf_res", "t1", h.ago(minutes=30)),
        h.notify("t1", "killed", h.ago(minutes=24)),
        h.launch("wf_res", "t2", h.ago(minutes=2)),
    )
    h.jl(run / "journal.jsonl", {"type": "launched"}, {"type": "started", "agentId": "new", "label": "X", "phase": "P"})
    h.jl(run / "agent-new.jsonl", {"type": "user", "timestamp": h.ago(minutes=2, seconds=0.3)})
    r = model.Run(run)
    r.poll(model.load_sessions())
    assert r.state == "running" and r.agents["new"].status == model.RUNNING and not r.agents["new"].superseded


def test_failed_agent_retried_is_superseded(claude_home):
    r = resumed_run(claude_home, failed_first=True)
    assert r.agents["old"].superseded and r.phases()[0].status == model.DONE


def test_parallel_agent_with_same_label_does_not_supersede(claude_home):
    r = resumed_run(claude_home, failed_first=True, parallel=True)  # "new" started before "old" failed
    assert not r.agents["old"].superseded and r.phases()[0].status == model.FAILED


def test_failed_wins_over_stale():
    t = model.Tail(Path("/nonexistent"))
    agents = [model.Agent("a", "a", "", t, status=model.STALE), model.Agent("b", "b", "", t, status=model.FAILED)]
    assert model.Phase("p", agents).status == model.FAILED


def test_replaced_transcript_does_not_double_count(demo_run):
    h, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    assert run.agents["a2"].tool_calls == 2
    tmp = run_dir / "new.jsonl"
    h.jl(tmp, h.assistant(h.ago(minutes=1), "m9", [h.tool("Read", file_path="/a/b.py")]))
    os.replace(tmp, run_dir / "agent-a2.jsonl")
    run.poll()
    assert run.agents["a2"].tool_calls == 1 and run.agents["a2"].last_action == Action("Read", "a/b.py")


def test_find_and_render(demo_run):
    _, _, run_dir = demo_run
    assert model.find_run("wf_abc") == run_dir
    assert model.find_run("demo") == run_dir
    run = model.Run(run_dir)
    run.poll(model.load_sessions())

    out = render(Painter(run).snapshot())  # the diagram is the default view
    for s in (
        "DEMO-FLOW",
        "phases",
        "◇ launch",
        "1. Аналитика",
        "2. Ревью",
        "Аналитик",
        "Архитектор",
        "log",
        "Grep TODO",
        "phase [1/3]",
        "opus 5.5 [1 running]",
        "Сценарии готовы",
    ):
        assert s in out, s

    out = render(Painter(run, view="cards").snapshot(), width=110)
    for s in ("demo-flow", "demo-session", "Аналитик", "Архитектор", "Ревью", "log", "Grep TODO"):
        assert s in out, s


def test_graph_on_narrow_terminal_falls_back_to_cards(demo_run):
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    out = render(Painter(run).snapshot(), width=60)
    assert "phases" not in out and "Аналитик" in out


def test_graph_without_sidebar_and_manual_phase(demo_run):
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    p = Painter(run)
    p.phase_sel = 1  # the second phase: no agents yet
    out = render(p.snapshot(height=30), width=90)
    assert "phases" not in out  # the phase column appears from 100 columns
    assert "no agents in this phase yet" in out and "pending" in out


def test_frozen_clock_reaches_rendering(demo_run, frozen_now):
    _, _, run_dir = demo_run
    run = model.Run(run_dir)
    run.poll(model.load_sessions())
    run.agents["a2"].last_ts = frozen_now - timedelta(seconds=200)
    assert "silent 3:20" in render(Painter(run).snapshot())


# ---- sessions and names ----


def test_load_sessions_types(claude_home):
    h = claude_home
    h.session(
        "s1", name="Проект", since_ms=h.ms_ago(minutes=3), started_ms=h.ms_ago(hours=1), entrypoint="claude-vscode"
    )
    info = model.load_sessions()["s1"]
    assert isinstance(info, model.SessionInfo)
    assert (info.name, info.status, info.entrypoint, info.alive) == ("Проект", "busy", "claude-vscode", True)
    assert info.since.tzinfo is not None and 170 < (model.now() - info.since).total_seconds() < 190
    assert info.started_at is not None and info.display_name() == "Проект"
    assert model.SessionInfo("abcdef1234").display_name() == "abcdef12"


def test_session_events_are_ordered_by_time(claude_home):
    h = claude_home
    h.session("s1")
    path = h.transcript("s1", h.assistant(h.ago(seconds=10), "m1", [h.tool("Read", file_path="/a/newest.py")]))
    w = model.SessionWatch("s1", path, model.load_sessions()["s1"])
    w.poll()
    sub = path.with_suffix("") / "subagents" / "agent-x.jsonl"
    h.jl(
        sub,
        *(h.assistant(h.ago(seconds=s), f"s{s}", [h.tool("Read", file_path=f"/a/old{s}.py")]) for s in (50, 40, 30)),
    )
    w.poll()
    assert [e.action.arg for e in w.events] == ["a/old50.py", "a/old40.py", "a/old30.py", "a/newest.py"]


def test_project_from_slug(tmp_path, monkeypatch):
    base = tmp_path / "code"
    (base / "my_app").mkdir(parents=True)
    model.project_from_slug.cache_clear()
    assert model.project_from_slug(model._slugify(str(base / "my_app"))) == "my_app"  # the directory is found again
    assert model.project_from_slug(model._slugify(str(base)) + "-gone-project") == "gone-project"  # the unmatched rest
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    model.project_from_slug.cache_clear()
    assert model.project_from_slug(model._slugify(str(tmp_path)) + "-src-tool") == "src-tool"  # no user name in it
    model.project_from_slug.cache_clear()


def test_project_falls_back_to_launch_cwd(claude_home):
    h = claude_home
    run = h.run("s-gone", "wf_x")
    parent = run.parents[2].with_suffix(".jsonl")
    parent.write_text("", encoding="utf-8")
    h.jl(parent, h.launch("wf_x", "t-wf_x", h.ago(minutes=5), cwd="/home/user/dev/launched_here"))
    (run / "agent-a1.jsonl").write_text("", encoding="utf-8")
    r = model.Run(run)
    r.poll(model.load_sessions())
    assert r.project == "launched_here"
