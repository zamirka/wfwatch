"""OpenCode sessions in the tracker: tools, nesting, background jobs, removal and the CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from opencode_home import OpenCodeHome

from wfwatch import model


def test_tracker_reads_tools_tokens_and_a_finished_turn(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session(
        "ses_root",
        title="Cache refactor",
        directory="/home/user/dev/demo",
        busy=h.at(seconds=20),
        tokens=(1200, 90, 20, 2500),
    )
    h.message(
        "ses_root",
        parts=[h.tool("read", "call_1", input={"path": "/home/user/dev/demo/app.py"})],
        model_id="opencode/zen-1",
    )
    h.message("ses_root", text="Done", created=h.at(seconds=5))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["opencode:ses_root"]
    assert watch.project == "demo"
    assert watch.where == "OpenCode"
    assert watch.status == model.BUSY
    assert watch.main.model == "opencode/zen-1"
    assert watch.main.tool_calls == 1
    assert watch.main.last_action == model.Action("", "Done")
    assert (watch.main.context_tokens, watch.main.output_tokens) == (3700, 90)
    assert watch in tracker.active_sessions()

    tracker.poll()
    assert watch.main.tool_calls == 1  # a poll re-reads rows but counts a call once
    h.touch("ses_root", busy=h.at(minutes=2), idle=h.at(seconds=2), outcome="succeeded")
    h.idle("ses_root", at=h.at(seconds=2))
    tracker.poll()
    assert watch.status == model.IDLE
    assert watch.main.status == model.DONE
    assert not watch.running_bg_tasks()


def test_child_sessions_are_grouped_under_their_root(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_root", title="Parent", busy=h.at(seconds=10))
    h.session("ses_child", parent="ses_root", title="Reviewer", agent="explore", busy=h.at(seconds=10))
    h.session("ses_grandchild", parent="ses_child", title="Checker", agent="explore", busy=h.at(seconds=10))
    h.message("ses_child", parts=[h.tool("grep", "call_c", input={"pattern": "TODO"})])
    tracker = model.Tracker()
    tracker.scan()
    root = tracker.watches["opencode:ses_root"]
    assert "opencode:ses_child" not in tracker.watches
    assert "opencode:ses_grandchild" not in tracker.watches
    labels = sorted(a.label for a in root.subagents.values())
    assert labels == ["Checker", "Reviewer"]
    assert len(root.running_subagents()) == 2
    assert any(e.who == "Reviewer" and e.action.tool == "Grep" for e in root.events)
    assert tracker.active_sessions() == [root]
    assert root.state == model.BUSY


def test_a_subagent_report_finishes_the_child(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_root", title="Parent", busy=h.at(seconds=10))
    h.session("ses_child", parent="ses_root", title="Reviewer", idle=h.at(seconds=5), busy=h.at(seconds=20))
    h.message(
        "ses_root",
        kind="synthetic",
        metadata={"source": "subagent", "childID": "ses_child", "state": "completed"},
        text='<subagent sessionID="ses_child" state="completed" description="Reviewer">\nLooks good\n</subagent>',
        description="Reviewer",
    )
    tracker = model.Tracker()
    tracker.scan()
    root = tracker.watches["opencode:ses_root"]
    child = next(iter(root.subagents.values()))
    assert child.status == model.DONE
    assert child.result == "Looks good"
    assert not root.running_subagents()


def test_background_shell_job_and_its_output(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_root", title="Tests", idle=h.at(seconds=5), busy=h.at(seconds=30))
    h.job("sh_1", "ses_root", command="pytest -q", title="Run tests")
    h.shell_output("sh_1", "[1/7] ok\n[7/7] ok\n")
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["opencode:ses_root"]
    task = watch.running_bg_tasks()[0]
    assert task.description == "Run tests"
    assert task.line == "[7/7] ok"
    assert watch.state == model.BACKGROUND
    assert tracker.active_sessions() == [watch]

    h.drop_job("sh_1")
    h.message(
        "ses_root",
        kind="synthetic",
        metadata={"source": "shell", "shellID": "sh_1", "state": "completed", "exit": 0},
        text='<shell id="sh_1" state="completed" command="pytest -q">\n[1/7] ok\n[7/7] ok\n</shell>',
        description="Run tests",
    )
    tracker.poll()
    assert task.status == "completed"
    assert task.exit_code == 0
    assert task.line == "[7/7] ok"
    assert watch.recent_bg_tasks()[0] is task
    tracker.scan()
    assert task.status == "completed"  # a poll cannot bring a finished job back


def test_background_subagent_keeps_the_parent_visible(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_root", title="Parent", idle=h.at(seconds=5), busy=h.at(seconds=30))
    h.session("ses_child", parent="ses_root", title="Reviewer", busy=h.at(seconds=10))
    h.job("sub_1", "ses_root", kind="subagent", child="ses_child", title="Review")
    tracker = model.Tracker()
    tracker.scan()
    root = tracker.watches["opencode:ses_root"]
    assert [a.label for a in root.bg_agents] == ["Reviewer"]
    assert root.state == model.BACKGROUND
    assert root in tracker.active_sessions()


def test_archived_and_stale_sessions_disappear(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_idle", title="Idle", updated=h.at(hours=2), idle=h.at(hours=2), busy=h.at(hours=3))
    h.session("ses_archived", title="Archived", archived=h.at(seconds=1))
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches == {}
    h.session("ses_old", title="Old", updated=h.at(hours=4), idle=h.at(hours=4), busy=h.at(hours=5))
    tracker.scan()
    assert tracker.watches == {}


def test_missing_or_broken_database_never_stops_the_tracker(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a", title="A")
    tracker = model.Tracker()
    tracker.scan()
    assert "opencode:ses_a" in tracker.watches
    h.close()
    h.db.unlink()
    tracker.scan()
    assert not any(sid.startswith("opencode:") for sid in tracker.watches)
    assert tracker.errors() == []


def test_cli_overview_shows_an_opencode_session_end_to_end(tmp_path):
    h = OpenCodeHome(tmp_path / "oc")
    try:
        h.session("ses_a", title="Cache refactor", directory="/home/user/dev/demo", busy=h.at(seconds=20))
        h.message(
            "ses_a", parts=[h.tool("shell", "call_1", input={"command": "pytest -q", "description": "Run tests"})]
        )
        h.message("ses_a", text="All green", created=h.at(seconds=5))
        env = {
            **os.environ,
            "WFWATCH_OPENCODE_DB": str(h.db),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "empty"),
            "COLUMNS": "120",
            "LINES": "40",
        }
        got = subprocess.run(
            [sys.executable, "-m", "wfwatch", "--once", "--overview", "--width", "200"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert got.returncode == 0, got.stderr
        assert "Cache refactor" in got.stdout
        assert "Run tests" in got.stdout
        assert "OpenCode" in got.stdout
        assert "Traceback" not in got.stderr
    finally:
        h.close()


def test_claude_and_opencode_sessions_live_side_by_side(claude_home, opencode_home: OpenCodeHome):
    claude_home.session("s-web", name="Claude session")
    claude_home.transcript(
        "s-web", claude_home.assistant(claude_home.ago(seconds=20), "m1", [claude_home.text("hello")])
    )
    opencode_home.session("ses_oc", title="OpenCode session", busy=opencode_home.at(seconds=10))
    tracker = model.Tracker()
    tracker.scan()
    assert {"s-web", "opencode:ses_oc"} <= set(tracker.watches)
    assert [(w.name, w.where) for w in tracker.active_sessions()] == [
        ("Claude session", "cli"),
        ("OpenCode session", "OpenCode"),
    ]
    assert tracker.errors() == []


def test_shell_process_is_found_by_the_output_file_it_holds_open(opencode_home: OpenCodeHome, monkeypatch):
    h = opencode_home
    h.session("ses_root", title="Render", idle=h.at(seconds=5), busy=h.at(minutes=1))
    h.job("sh_1", "ses_root", command="python worker.py", title="Render frames")
    out = h.shell_output("sh_1", "frame 12\n")
    table = model.ProcTable.parse(
        "500 1 0.0 05:00:00 /usr/bin/opencode serve\n"
        f"510 500 0.0 01:00:00 /bin/zsh -c python worker.py > {out}\n"
        "511 510 87.0 00:59:00 python worker.py\n",
        model.now().timestamp(),
    )
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(
        model,
        "open_files",
        lambda pids: {510: [(10, str(out))], 511: [(1, str(out))]},
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["opencode:ses_root"]
    assert (watch.current_proc.pid, watch.current_proc.cpu) == (511, 87.0)


def test_cli_overview_keeps_claude_and_opencode_sessions_apart(claude_home, tmp_path):
    claude_home.session("s-web", name="Claude session")
    claude_home.transcript(
        "s-web", claude_home.assistant(claude_home.ago(seconds=20), "m1", [claude_home.text("hello")])
    )
    h = OpenCodeHome(tmp_path / "oc")
    try:
        h.session("ses_oc", title="OpenCode session", directory="/home/user/dev/demo", busy=h.at(seconds=10))
        h.message("ses_oc", text="All green", created=h.at(seconds=5))
        env = {
            **os.environ,
            "WFWATCH_OPENCODE_DB": str(h.db),
            "CLAUDE_CONFIG_DIR": str(claude_home.root),
            "COLUMNS": "120",
            "LINES": "40",
        }
        got = subprocess.run(
            [sys.executable, "-m", "wfwatch", "--once", "--overview", "--width", "200"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert got.returncode == 0, got.stderr
        assert "Claude session" in got.stdout
        assert "OpenCode session" in got.stdout
        assert "Traceback" not in got.stderr
    finally:
        h.close()


def test_an_incompatible_database_does_not_stop_other_readers(opencode_home, claude_home):
    """A schema this reader does not know is its own error, not the end of the scan."""
    h = opencode_home
    claude_home.session("s-web", name="Claude session")
    claude_home.transcript(
        "s-web", claude_home.assistant(claude_home.ago(seconds=20), "m1", [claude_home.text("hello")])
    )
    h.session("ses_a", title="A")
    h.conn.execute("alter table session_v2 drop column time_archived")
    h.conn.commit()
    tracker = model.Tracker()
    tracker.scan()
    assert "s-web" in tracker.watches
    assert not any(sid.startswith("opencode:") for sid in tracker.watches)
    assert tracker.errors() == []


def test_a_child_shell_job_is_visible_from_the_root(opencode_home: OpenCodeHome):
    """A child session has no card of its own: its command belongs to the root the user sees."""
    h = opencode_home
    h.session("ses_root", title="Parent", busy=h.at(minutes=5), idle=h.at(minutes=4))
    h.session("ses_child", parent="ses_root", title="Worker", busy=h.at(minutes=5), idle=h.at(minutes=4))
    h.job("sh_1", "ses_child", command="pytest -q", title="Run tests")
    h.shell_output("sh_1", "[1/2] ok\n[2/2] ok\n")
    tracker = model.Tracker()
    tracker.scan()
    root = tracker.watches["opencode:ses_root"]
    assert [t.description for t in root.bg_tasks] == ["Run tests"]
    assert [t.line for t in root.running_bg_tasks()] == ["[2/2] ok"]
    assert root.state == model.BACKGROUND
    assert tracker.active_sessions() == [root]

    h.drop_job("sh_1")
    h.message(
        "ses_child",
        kind="synthetic",
        metadata={"source": "shell", "shellID": "sh_1", "state": "completed", "exit": 0},
        text='<shell id="sh_1" state="completed" command="pytest -q">\n[1/2] ok\n[2/2] ok\n</shell>',
        description="Run tests",
    )
    tracker.scan()
    assert [t.status for t in root.recent_bg_tasks()] == ["completed"]


def test_a_streaming_message_keeps_the_session_visible(opencode_home: OpenCodeHome, frozen_now):
    """A row rewritten now is activity now: the agent may have started its message hours ago."""
    h = opencode_home
    t0 = frozen_now
    h.session(
        "ses_a",
        title="Streaming",
        created=t0 - timedelta(hours=3),
        updated=t0 - timedelta(hours=2),
        busy=t0 - timedelta(minutes=125),
        idle=t0 - timedelta(minutes=126),
    )
    mid = h.message("ses_a", parts=[h.text("old")], created=t0 - timedelta(hours=2), updated=t0 - timedelta(hours=2))
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.active_sessions() == []  # silent for two hours: stuck, even if the turn is open

    h.update_message("ses_a", mid, parts=[h.text("new text")])
    tracker.scan()  # the row itself still says nothing; the message it rewrote is the newest activity
    assert [w.name for w in tracker.active_sessions()] == ["Streaming"]


def test_a_silent_busy_child_does_not_pin_the_root(opencode_home: OpenCodeHome):
    """A child that stopped writing an hour ago is not a reason to keep the parent on screen."""
    h = opencode_home
    h.session("ses_root", title="Parent", updated=h.at(hours=2), busy=h.at(hours=2), idle=h.at(hours=3))
    h.session(
        "ses_child",
        parent="ses_root",
        title="Silent",
        updated=h.at(hours=2),
        busy=h.at(hours=2),
        idle=h.at(hours=3),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.active_sessions() == []


def test_a_grandchild_report_finishes_it_from_its_parent(opencode_home: OpenCodeHome):
    """The report of a nested agent is written to its own parent, not to the root."""
    h = opencode_home
    h.session("ses_root", title="Parent", busy=h.at(seconds=10))
    h.session("ses_child", parent="ses_root", title="Middle", busy=h.at(minutes=5), idle=h.at(minutes=4))
    h.session("ses_grand", parent="ses_child", title="Deep", busy=h.at(minutes=2), idle=h.at(minutes=6))
    tracker = model.Tracker()
    tracker.scan()
    deep = next(a for a in tracker.watches["opencode:ses_root"].subagents.values() if a.label == "Deep")
    assert deep.status == model.RUNNING  # the child writes, its report is not in yet

    h.message(
        "ses_child",
        kind="synthetic",
        metadata={"source": "subagent", "childID": "ses_grand", "state": "completed"},
        text='<subagent sessionID="ses_grand" state="completed" description="Deep">\nAll good\n</subagent>',
        description="Deep",
    )
    tracker.scan()
    deep = next(a for a in tracker.watches["opencode:ses_root"].subagents.values() if a.label == "Deep")
    assert deep.status == model.DONE
    assert deep.result == "All good"


def test_fresh_output_keeps_an_orphan_job_task_running(
    opencode_home: OpenCodeHome, frozen_now, monkeypatch: pytest.MonkeyPatch
):
    """The job row disappears before the command ends: the file it writes still proves it runs."""
    h = opencode_home
    t0 = frozen_now
    h.session(
        "ses_a",
        title="Tests",
        created=t0 - timedelta(hours=2),
        busy=t0 - timedelta(minutes=5),
        idle=t0 - timedelta(minutes=6),
    )
    h.job(
        "sh_1",
        "ses_a",
        command="pytest -q",
        title="Run tests",
        created=t0 - timedelta(minutes=90),
        updated=t0 - timedelta(minutes=90),
    )
    tracker = model.Tracker()
    tracker.scan()
    task = tracker.watches["opencode:ses_a"].running_bg_tasks()[0]
    h.drop_job("sh_1")
    out = h.shell_output("sh_1", "[1/1] ok\n")

    later = t0 + timedelta(hours=2)
    monkeypatch.setattr(model, "now", lambda: later)
    os.utime(out, (later.timestamp(), later.timestamp()))
    tracker.poll()
    assert task.status == model.RUNNING


def test_the_session_limit_keeps_whole_recent_trees(opencode_home: OpenCodeHome, monkeypatch: pytest.MonkeyPatch):
    """The cap must drop old trees, not the newest rows."""
    from wfwatch.opencode import watch as oc

    monkeypatch.setattr(oc, "MAX_SESSIONS", 3)
    h = opencode_home
    h.session("ses_old", title="Old", created=h.at(minutes=40), updated=h.at(minutes=30), busy=h.at(minutes=31))
    h.session("ses_old_child", parent="ses_old", title="Old child", created=h.at(minutes=39), updated=h.at(minutes=30))
    h.session("ses_new", title="New", created=h.at(minutes=2), updated=h.at(minutes=1), busy=h.at(minutes=1))
    h.session("ses_new_a", parent="ses_new", title="New a", created=h.at(minutes=2), updated=h.at(minutes=1))
    h.session("ses_new_b", parent="ses_new", title="New b", created=h.at(minutes=2), updated=h.at(minutes=1))
    tracker = model.Tracker()
    tracker.scan()
    assert set(tracker.watches) == {"opencode:ses_new"}
    assert sorted(a.label for a in tracker.watches["opencode:ses_new"].subagents.values()) == ["New a", "New b"]


def test_the_shell_directory_is_listed_once_per_change(opencode_home: OpenCodeHome, monkeypatch: pytest.MonkeyPatch):
    """Every poll of every session must not walk the whole output directory again."""
    h = opencode_home
    h.session("ses_a", title="A")
    h.session("ses_b", title="B")
    h.shell_output("sh_1", "x\n")
    walked: list[str] = []
    real = Path.glob

    def counting(self, pattern, **kw):
        if pattern == "*/*.out":
            walked.append(str(self))
        return real(self, pattern, **kw)

    monkeypatch.setattr(Path, "glob", counting)
    tracker = model.Tracker()
    tracker.scan()
    assert len(walked) == 1
    tracker.poll()
    assert len(walked) == 1
    h.shell_output("sh_2", "y\n")
    tracker.poll()
    assert len(walked) == 2


def test_a_failed_query_is_reported_instead_of_being_hidden(opencode_home: OpenCodeHome, monkeypatch):
    """A watch that cannot read its database must say so: a card frozen on old data looks alive."""
    from wfwatch.opencode.store import Store, StoreError

    h = opencode_home
    h.session("ses_a", title="A")
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["opencode:ses_a"]

    def broken(self, ids):
        raise StoreError("database is locked")

    monkeypatch.setattr(Store, "jobs", broken)
    tracker.poll()
    assert "locked" in watch.error
    assert any("locked" in e for e in tracker.errors())


def test_a_child_process_is_not_counted_twice_under_the_root(opencode_home: OpenCodeHome, monkeypatch):
    """The root shows its children's commands; their processes must reach it through one path only."""
    h = opencode_home
    h.session("ses_root", title="Parent", busy=h.at(seconds=10))
    h.session("ses_child", parent="ses_root", title="Worker", busy=h.at(minutes=2), idle=h.at(minutes=6))
    h.job("sh_1", "ses_child", command="python worker.py", title="Render frames")
    out = h.shell_output("sh_1", "frame 12\n")
    table = model.ProcTable.parse(
        "500 1 0.0 05:00:00 /usr/bin/opencode serve\n"
        f"510 500 0.0 01:00:00 /bin/zsh -c python worker.py > {out}\n"
        "511 510 87.0 00:59:00 python worker.py\n",
        model.now().timestamp(),
    )
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(model, "open_files", lambda pids: {510: [(10, str(out))], 511: [(1, str(out))]})
    tracker = model.Tracker()
    tracker.scan()
    root = tracker.watches["opencode:ses_root"]
    assert [p.pid for p in root.procs] == [511]
    assert [t.task_id for t in root.running_bg_tasks()] == ["sh_1"]
