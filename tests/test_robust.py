"""Malformed or unexpected data must never stop wfwatch: every entry point keeps working."""

import io
import json
import os
import re
import shutil
import sys

import pytest
from rich.console import Console

from wfwatch import cli, model
from wfwatch.cli import App


def base(h):
    """A running demo workflow with one agent, its session and script."""
    h.session("sess-1", fname="1000-sess-1.json")
    sess = h.root / "projects" / "-proj" / "sess-1"
    run = sess / "subagents" / "workflows" / "wf_abc-123"
    (sess / "workflows" / "scripts").mkdir(parents=True)
    (sess / "workflows" / "scripts" / "demo-wf_abc-123.js").write_text(
        "export const meta = {\n  name: 'demo',\n  phases: [{ title: 'A' }, { title: 'B' }],\n}\n", encoding="utf-8"
    )
    h.jl(sess.with_suffix(".jsonl"), h.launch("wf_abc-123", "t-1", h.ago(minutes=5)))
    h.jl(
        run / "journal.jsonl",
        {"type": "launched"},
        {"type": "started", "agentId": "a1", "label": "Агент 1", "phase": "A"},
    )
    h.jl(
        run / "agent-a1.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=5), "cwd": "/x/proj"},
        h.assistant(h.ago(minutes=1), "m1", [h.tool("Read", file_path="/a/b.py")]),
    )
    return sess, run


def agent(run):
    return run / "agent-a1.jsonl"


def journal(run):
    return run / "journal.jsonl"


def parent(sess):
    return sess.with_suffix(".jsonl")


def reg(h, name):
    return h.root / "sessions" / name


def _chmod0(p):
    os.chmod(p, 0)


def _scripts_is_file(h, s, r):
    shutil.rmtree(s / "workflows" / "scripts")
    (s / "workflows" / "scripts").write_text("x", encoding="utf-8")


def _agent_is_dir(h, s, r):
    agent(r).unlink()
    agent(r).mkdir()


CASES = {
    "baseline": lambda h, s, r: None,
    "null_line_agent": lambda h, s, r: h.jl(agent(r), "null"),
    "list_line_journal": lambda h, s, r: h.jl(journal(r), "[1,2]"),
    "string_line_parent": lambda h, s, r: h.jl(parent(s), '"<task-notification>"'),
    "numeric_ts": lambda h, s, r: h.jl(agent(r), {"type": "user", "timestamp": 1792795104909}),
    "naive_ts": lambda h, s, r: h.jl(agent(r), {"type": "user", "timestamp": "2026-10-27T10:00:00"}),
    "garbage_ts": lambda h, s, r: h.jl(agent(r), {"type": "user", "timestamp": "2026-99-99T99:99:99Z"}),
    "message_is_str": lambda h, s, r: h.jl(
        agent(r), {"type": "assistant", "timestamp": h.ago(seconds=5), "message": "oops"}
    ),
    "usage_is_list": lambda h, s, r: h.jl(agent(r), {"type": "assistant", "message": {"usage": [1]}}),
    "content_is_str": lambda h, s, r: h.jl(agent(r), {"type": "assistant", "message": {"content": "text", "model": 5}}),
    "tool_use_odd": lambda h, s, r: h.jl(
        agent(r),
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": ["Bash"], "input": "ls"}, {"type": "text", "text": 5}, 7]
            },
        },
    ),
    "usage_values_odd": lambda h, s, r: h.jl(
        agent(r),
        {
            "type": "assistant",
            "message": {
                "id": 1,
                "usage": {"input_tokens": "x", "output_tokens": None, "cache_read_input_tokens": 1e300},
            },
        },
    ),
    "non_utf8_agent": lambda h, s, r: h.jl(
        agent(r), b'{"type":"user","timestamp":"2026-10-27T10:00:00Z","x":"\xff\xfe"}'
    ),
    "non_utf8_parent_ts": lambda h, s, r: h.jl(
        parent(s), b'{"timestamp":"\xff","x":"<task-notification><task-id>q</task-id><status>completed</status>"}'
    ),
    "broken_json_parent": lambda h, s, r: h.jl(parent(s), b'{"x":"<task-notification><task-id>t-1</task-id><status>'),
    "toolUseResult_str": lambda h, s, r: h.jl(
        parent(s), {"type": "user", "toolUseResult": "Error", "x": "local_workflow"}
    ),
    "launch_fields_odd": lambda h, s, r: h.jl(
        parent(s),
        {
            "type": "user",
            "timestamp": 5,
            "toolUseResult": {"taskType": "local_workflow", "runId": ["wf_abc-123"], "taskId": {"x": 1}},
        },
    ),
    "bg_fields_odd": lambda h, s, r: h.jl(
        parent(s),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": 5,
                        "input": {"run_in_background": True, "command": ["x"]},
                    }
                ]
            },
        },
        {"type": "user", "toolUseResult": {"backgroundTaskId": 7}, "message": {"content": "run_in_background"}},
    ),
    "session_file_list": lambda h, s, r: reg(h, "999.json").write_text("[1]", encoding="utf-8"),
    "session_file_binary": lambda h, s, r: reg(h, "996.json").write_bytes(b"\xff\xfe\x00{"),
    "session_pid_huge": lambda h, s, r: reg(h, "998.json").write_text(
        json.dumps({"pid": 2**40, "sessionId": "x"}), encoding="utf-8"
    ),
    "session_pid_odd": lambda h, s, r: reg(h, "995.json").write_text(
        json.dumps({"pid": "abc", "sessionId": "y"}), encoding="utf-8"
    ),
    "session_pid_negative": lambda h, s, r: reg(h, "994.json").write_text(
        json.dumps({"pid": -1, "sessionId": "z"}), encoding="utf-8"
    ),
    "session_since_str": lambda h, s, r: reg(h, "997.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "sess-1", "status": "idle", "statusUpdatedAt": "soon", "name": 5}),
        encoding="utf-8",
    ),
    "session_fields_odd": lambda h, s, r: reg(h, "993.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "sessionId": "w", "status": ["busy"], "cwd": 1, "startedAt": "x", "procStart": 5}
        ),
        encoding="utf-8",
    ),
    "journal_agentId_list": lambda h, s, r: h.jl(journal(r), {"type": "started", "agentId": ["x"]}),
    "label_not_str": lambda h, s, r: h.jl(journal(r), {"type": "started", "agentId": "a9", "label": 5, "phase": "A"}),
    "phase_not_str": lambda h, s, r: h.jl(
        journal(r), {"type": "started", "agentId": "a8", "label": "x", "phase": ["A"]}
    ),
    "agent_id_path": lambda h, s, r: h.jl(journal(r), {"type": "started", "agentId": "../../../x", "label": "x"}),
    "result_odd": lambda h, s, r: h.jl(journal(r), {"type": "result", "agentId": "a1", "result": [1, {"a": None}]}),
    "agent_no_permission": lambda h, s, r: _chmod0(agent(r)),
    "parent_no_permission": lambda h, s, r: _chmod0(parent(s)),
    "journal_no_permission": lambda h, s, r: _chmod0(journal(r)),
    "scripts_is_file": _scripts_is_file,
    "agent_transcript_is_dir": _agent_is_dir,
    "script_unterminated": lambda h, s, r: (s / "workflows" / "scripts" / "demo-wf_abc-123.js").write_text(
        "export const meta = { name: 'x', phases: [{ title: ", encoding="utf-8"
    ),
    "script_non_utf8": lambda h, s, r: (s / "workflows" / "scripts" / "demo-wf_abc-123.js").write_bytes(
        b"\xff\xfe\x00"
    ),
    "subagent_meta_odd": lambda h, s, r: (
        h.jl(s / "subagents" / "agent-z.jsonl", {"type": "user", "timestamp": h.ago(seconds=5)}),
        (s / "subagents" / "agent-z.meta.json").write_text('["no"]', encoding="utf-8"),
    ),
    "deep_json": lambda h, s, r: h.jl(agent(r), "[" * 100000 + "]" * 100000),
    "empty_files": lambda h, s, r: (
        agent(r).write_text("", encoding="utf-8"),
        parent(s).write_text("\n\n", encoding="utf-8"),
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_broken_data_never_crashes(claude_home, capsys, name):
    if "permission" in name and (sys.platform == "win32" or os.geteuid() == 0):
        pytest.skip("permissions do not apply")
    h = claude_home
    sess, run = base(h)
    CASES[name](h, sess, run)

    tr = model.Tracker()
    tr.scan()
    tr.poll()
    assert tr.errors() == [], tr.errors()  # data problems are skipped, not even reported as failures
    app = App(tr, False)
    con = Console(record=True, width=120, height=40, file=io.StringIO())
    con.print(app.snapshot())
    app.open(run)
    con.print(app.snapshot())
    con.print(app.frame(40))
    app.on_key("v")
    con.print(app.frame(40))
    out = con.export_text()
    assert "render error" not in out
    assert cli.main(["--once", "--width", "120"]) == 0
    assert cli.main(["--list", "--width", "80"]) == 0
    assert "Traceback" not in capsys.readouterr().err


def test_unexpected_failure_is_recorded_not_fatal(demo_run, monkeypatch, capsys):
    def boom(self, sessions=None):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(model.Run, "poll", boom)
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    assert tr.errors() == ["wf_abc-123: RuntimeError: disk on fire"]
    app = App(tr, False)
    con = Console(record=True, width=120, height=30, file=io.StringIO())
    con.print(app.frame(30))
    out = con.export_text()
    assert "read error – wf_abc-123: RuntimeError: disk on fire" in out
    assert len(out.splitlines()) == 30  # the status line takes one line of the screen

    assert cli.main(["--once"]) == 0
    assert "disk on fire" in capsys.readouterr().out
    with pytest.raises(RuntimeError):
        cli.main(["--once", "--debug"])
    assert model.DEBUG is False  # restored


def test_render_failure_is_shown_not_fatal(demo_run, monkeypatch):
    class Broken:
        def __rich_console__(self, console, options):
            raise ValueError("bad frame")

    tr = model.Tracker()
    tr.scan()
    app = App(tr, False)
    app.open(next(iter(tr.runs)))
    monkeypatch.setattr(cli.Painter, "screen", lambda self, *a, **kw: Broken())
    con = Console(record=True, width=100, height=20, file=io.StringIO())
    con.print(app.frame(20))
    assert "render error: ValueError: bad frame" in con.export_text()
    assert app.frame_error == "ValueError: bad frame"
    monkeypatch.setattr(cli.Overview, "screen", lambda self, height: 1 / 0)  # fails while building the view
    app.on_key("o")
    con.print(app.frame(20))
    assert "ZeroDivisionError" in con.export_text()
    monkeypatch.setattr(model, "DEBUG", True)
    with pytest.raises(ZeroDivisionError):
        app.frame(20)


def test_loop_failure_goes_to_status_line(demo_run, monkeypatch):
    tr = model.Tracker()
    tr.scan()
    app = App(tr, False)
    monkeypatch.setattr(tr, "scan", lambda: (_ for _ in ()).throw(OSError("registry gone")))
    app.safe("scan", tr.scan)
    assert app.status_error().startswith("⚠ read error – scan: OSError: registry gone")
    app.safe("scan", lambda: None)
    assert app.status_error() == ""


# ---- text that would break the terminal or the layout ----

# Control sequences a transcript may carry (written by a model, a tool or a web page it read):
# set the window title and clear the screen; write the clipboard and leave the alternate screen.
TITLE = "\x1b]0;PWNED\x07\x1b[2J"
CLIP = "\x1b]52;c;cHduZWQ=\x1b\\\x1b[?1049l"
BREAK = "\nsecond line\ttab"
CONTROL = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f]")  # anything but the newline between screen rows


def hostile(h):
    """Every text field the views show carries control sequences, line breaks or tabs."""
    sess, run = base(h)
    h.session("sess-1", fname="1000-sess-1.json", name="evil" + TITLE + BREAK, entrypoint="cli" + CLIP)
    (sess / "workflows" / "scripts" / "demo-wf_abc-123.js").write_text(
        "export const meta = {\n  name: 'demo" + TITLE + "',\n  description: 'about" + CLIP + "',\n"
        "  phases: [{ title: 'A' }, { title: 'B" + TITLE + "' }],\n}\n",
        encoding="utf-8",
    )
    h.jl(
        journal(run),
        {"type": "started", "agentId": "a2", "label": "lab" + CLIP + BREAK, "phase": "A"},
        {"type": "started", "agentId": "id" + TITLE, "phase": "A"},  # no label: the id is shown
        {"type": "started", "agentId": "a4", "label": "four", "phase": "B" + TITLE},
        {"type": "result", "agentId": "a4", "result": "res" + TITLE + "\nmore"},
    )
    h.jl(
        agent(run),
        h.assistant(
            h.ago(seconds=30),
            "m2",
            [
                h.tool("Bash", "t1", description="desc" + CLIP + BREAK, command="ls"),
                h.tool("Grep", "t2", pattern="a\nb\nc\td" + CLIP),
                h.tool("Read", "t3", file_path="/x/y" + TITLE + "z.py"),
                h.tool("WebFetch", "t4", url="https://example.com/" + CLIP),
                h.tool("mcp__srv__do" + CLIP, "t5"),
                h.text("said" + CLIP + "\x9b2J\N{RIGHT-TO-LEFT OVERRIDE}"),
            ],
            model_id="claude-opus-5-5" + CLIP,
        ),
    )
    out = h.root / "tmp" / "bg1.output"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("step 1\nline" + CLIP + "\x9b2J\n", encoding="utf-8")
    h.jl(
        parent(sess),
        h.assistant(
            h.ago(minutes=2),
            "m3",
            [h.tool("Bash", "tb", description="bench" + CLIP + BREAK, command="make", run_in_background=True)],
        ),
        {
            "type": "user",
            "timestamp": h.ago(minutes=2),
            "toolUseResult": {"backgroundTaskId": "bg1"},
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tb", "content": f"Output is being written to: {out}"}
                ]
            },
        },
        h.assistant(h.ago(seconds=10), "m4", [h.tool("Grep", "tg", pattern="p" + CLIP + "\nq")]),
    )
    h.jl(sess / "subagents" / "agent-s1.jsonl", h.assistant(h.ago(seconds=5), "s1", [h.tool("Glob", pattern="*")]))
    (sess / "subagents" / "agent-s1.meta.json").write_text(
        json.dumps({"description": "sub" + CLIP + BREAK}), encoding="utf-8"
    )
    return sess, run


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("a\nb", "a b"),
        ("a  \n\t  b", "a b"),
        ("x\r\ny\x0bz", "x y z"),
        ("aligned  columns", "aligned  columns"),  # plain spaces stay
        ("\x1b]0;title\x07\x1b[2Jtext", "]0;title[2Jtext"),
        ("a\x9b2Jb\x7f", "a2Jb"),
        ("\N{RIGHT-TO-LEFT OVERRIDE}abc\N{LEFT-TO-RIGHT ISOLATE}", "abc"),
        ("", ""),
    ],
)
def test_clean(raw, shown):
    assert model.clean(raw) == shown


def test_control_sequences_in_the_data_never_reach_the_terminal(claude_home, capsys):
    h = claude_home
    _, run = hostile(h)
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    app = App(tr, False)
    assert len(app.items()) == 2  # the workflow and its session
    seen = ""
    for view in ("graph", "cards"):
        if view == "cards":
            app.on_key("v")
        for item in (None, *app.items()):
            if item is None:
                app.mode = "overview"
            else:
                app.open(item)
            for w, hh in ((80, 24), (120, 40), (200, 60)):
                con = Console(width=w, height=hh, color_system=None, file=io.StringIO())
                con.print(app.frame(hh))
                text = con.file.getvalue()
                assert not CONTROL.search(text), (view, item, w, repr(CONTROL.findall(text)))
                assert len(text.rstrip("\n").split("\n")) == hh  # a line break in the data adds no row
                con = Console(width=w, color_system=None, file=io.StringIO())
                con.print(app.snapshot())
                assert not CONTROL.search(con.file.getvalue()), (view, item, w)
                seen += text + con.file.getvalue()
    # the values are still shown, made into one line
    for s in ("evil]0;PWNED[2J second line tab", "lab]52;c;", "desc]52;c;", "a b c d", "said]52;c;", "sub]52"):
        assert s in seen, s

    for args in (["--once", "--width", "120"], ["--once", "-o", "--width", "200"], ["--once", "-c", "--width", "100"]):
        assert cli.main(args) == 0
        out = capsys.readouterr().out
        assert not CONTROL.search(out), (args, repr(CONTROL.findall(out)))
    assert cli.main(["--list", "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert not CONTROL.search(out)
    assert len(out.rstrip("\n").split("\n")) == 2  # the header and the run: a name with a line break stays one row
