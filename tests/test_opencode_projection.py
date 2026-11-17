"""Deriving an agent, its tool activity and its background tasks from OpenCode rows."""

from __future__ import annotations

from pathlib import Path

from opencode_home import OpenCodeHome

from wfwatch import model
from wfwatch.opencode import Store
from wfwatch.opencode.projection import Projection


def projection(home: OpenCodeHome, sid: str = "ses_a", label: str = "demo") -> Projection:
    agent = model.Agent("main", label, "", model.Tail(Path("/nonexistent")))
    return Projection(agent, label)


def rows(home: OpenCodeHome, sid: str = "ses_a"):
    with Store(home.db) as store:
        return store.messages(sid)


def test_tool_parts_become_actions_and_usage(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.message(
        "ses_a",
        parts=[
            h.reasoning("thinking"),
            h.tool("read", "call_1", input={"path": "/home/user/dev/demo/app.py"}),
            h.tool("shell", "call_2", status="running", input={"command": "pytest -q", "description": "Run tests"}),
            h.text("Готово"),
        ],
        model_id="opencode/zen-1",
    )
    p = projection(h)
    p.apply(rows(h))
    assert p.agent.tool_calls == 2
    assert len(p.agent.tool_ts) == 2
    assert p.agent.last_action == model.Action("", "Готово")
    assert p.agent.model == "opencode/zen-1"
    assert [(e.kind, e.action.tool) for e in p.events] == [("tool", "Read"), ("tool", "Bash")]
    assert p.waiting is False


def test_reapplying_the_same_rows_changes_nothing(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.message("ses_a", parts=[h.tool("read", "call_1", input={"path": "/x"}), h.text("first")])
    p = projection(h)
    p.apply(rows(h))
    before = (p.agent.tool_calls, len(p.events))
    p.apply(rows(h))
    assert (p.agent.tool_calls, len(p.events)) == before


def test_streaming_message_updates_in_place(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    mid = h.message("ses_a", parts=[h.reasoning("thinking")])
    p = projection(h)
    p.apply(rows(h))
    assert p.agent.tool_calls == 0

    h.update_message(
        "ses_a", mid, parts=[h.tool("read", "call_1", status="running", input={"path": "/home/user/dev/demo/app.py"})]
    )
    p.apply(rows(h))
    assert p.agent.tool_calls == 1
    assert p.agent.last_action == model.Action("Read", "demo/app.py")

    h.update_message(
        "ses_a",
        mid,
        parts=[
            h.tool("read", "call_1", status="completed", input={"path": "/home/user/dev/demo/app.py"}, output="content")
        ],
    )
    p.apply(rows(h))
    assert p.agent.tool_calls == 1
    assert len(p.events) == 1

    h.update_message("ses_a", mid, parts=[h.text("done now")])
    p.apply(rows(h))
    assert p.agent.last_action == model.Action("", "done now")


def test_tool_names_and_arguments_are_readable(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    parts = [
        h.tool("write", "c1", input={"path": "/home/user/dev/demo/a.py", "content": "print(1)"}),
        h.tool("glob", "c2", input={"pattern": "**/*.py"}),
        h.tool("grep", "c3", input={"pattern": "TODO"}),
        h.tool("webfetch", "c4", input={"url": "https://example.com"}),
        h.tool("skill", "c5", input={"id": "docs"}),
        h.tool("question", "c6", status="running", input={"questions": [{"question": "Continue?"}]}),
    ]
    h.message("ses_a", parts=parts)
    p = projection(h)
    p.apply(rows(h))
    assert [e.action for e in p.events] == [
        model.Action("Write", "demo/a.py"),
        model.Action("Glob", "**/*.py"),
        model.Action("Grep", "TODO"),
        model.Action("WebFetch", "https://example.com"),
        model.Action("Skill", "docs"),
        model.Action("Question", "Continue?"),
    ]
    assert p.waiting is True
    assert p.waiting_since is not None


def test_idle_failure_becomes_an_event(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.idle("ses_a", outcome="failed")
    p = projection(h)
    p.apply(rows(h))
    assert [(e.kind, e.detail) for e in p.events] == [("failed", "failed")]


def test_synthetic_shell_completion_finishes_a_background_task(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.job("sh_1", "ses_a", command="pytest -q", title="Run tests")
    out = h.shell_output("sh_1", "[1/7] ok\n[7/7] ok\n")
    p = projection(h)
    with Store(h.db) as store:
        p.apply_jobs(store.jobs(["ses_a"]), {"sh_1": out})
    task = p.tasks["sh_1"]
    assert (task.status, task.description) == (model.RUNNING, "Run tests")

    h.message(
        "ses_a",
        kind="synthetic",
        metadata={"source": "shell", "shellID": "sh_1", "state": "completed", "exit": 0},
        text='<shell id="sh_1" state="completed" command="pytest -q">\n[1/7] ok\n[7/7] ok\n</shell>',
        description="Run tests",
    )
    p.apply(rows(h))
    assert task.status == "completed"
    assert task.exit_code == 0
    assert task.last_line == "[7/7] ok"
    assert task.ended is not None


def test_synthetic_subagent_report_and_background_job(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.session("ses_child", parent="ses_a", title="review", agent="explore")
    h.job("sh_sub", "ses_a", kind="subagent", child="ses_child", title="review")
    p = projection(h)
    with Store(h.db) as store:
        p.apply_jobs(store.jobs(["ses_a"]), {})
    assert p.running_children == {"ses_child"}

    h.message(
        "ses_a",
        kind="synthetic",
        metadata={"source": "subagent", "childID": "ses_child", "state": "completed"},
        text='<subagent sessionID="ses_child" state="completed" description="review">\nLooks good\n</subagent>',
        description="review",
    )
    p.apply(rows(h))
    assert p.reports["ses_child"][0] == "completed"
    assert "Looks good" in p.reports["ses_child"][1]


def test_malformed_parts_are_skipped(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.message("ses_a", parts=[{"type": "tool"}, {"type": "tool", "id": "", "name": ""}, {"type": "text"}])
    p = projection(h)
    p.apply(rows(h))
    assert p.agent.tool_calls == 0
    assert p.events == []


def test_two_updates_in_one_millisecond_are_both_seen(opencode_home: OpenCodeHome):
    """OpenCode rewrites a streaming row faster than the clock ticks: the content decides, not the stamp."""
    h = opencode_home
    h.session("ses_a")
    mid = h.message("ses_a", parts=[h.text("first")])
    p = projection(h)
    p.apply(rows(h))
    assert p.agent.last_action == model.Action("", "first")

    same = h.at(seconds=2)
    h.update_message("ses_a", mid, parts=[h.text("second")], updated=same)
    p.apply(rows(h))
    h.update_message("ses_a", mid, parts=[h.text("third")], updated=same)
    p.apply(rows(h))
    assert p.agent.last_action == model.Action("", "third")
