"""OpenCode sessions in the tracker: tools, nesting, background jobs, removal and the CLI."""

from __future__ import annotations

import os
import subprocess
import sys

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
