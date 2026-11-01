"""Codex rollout integration: real record shapes, incremental turns, forks and damaged lines."""

import contextlib
import json
from datetime import timedelta

import pytest

from wfwatch import codex, model


def row(kind, payload, at=None):
    return {"timestamp": (at or model.now()).isoformat(), "type": kind, "payload": payload}


def append(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def rollout(root, sid="one", parent="", path=""):
    f = root / "sessions/2026/10/31" / f"rollout-{sid}.jsonl"
    append(
        f,
        row(
            "session_meta",
            {
                "id": sid,
                "cwd": "/home/user/dev/demo",
                "parent_thread_id": parent,
                "agent_path": path,
                "originator": "codex-tui",
            },
        ),
        row("turn_context", {"model": "gpt-6-astra", "cwd": "/home/user/dev/demo"}),
        row("event_msg", {"type": "task_started"}),
    )
    return f


def test_tracker_reads_codex_tools_tokens_and_completed_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call1",
                "arguments": json.dumps({"cmd": "pytest", "workdir": "/tmp"}),
            },
        ),
        row(
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 1200},
                    "total_token_usage": {"output_tokens": 77},
                },
            },
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.project == "demo"
    assert watch.where.startswith("codex")
    assert watch.status == model.BUSY
    assert watch.main.model == "gpt-6-astra"
    assert watch.main.tool_calls == 1
    assert watch.main.last_action == model.Action("exec_command", "pytest")
    assert (watch.main.context_tokens, watch.main.output_tokens) == (1200, 77)
    tracker.poll()
    assert watch.main.tool_calls == 1
    append(path, row("event_msg", {"type": "task_complete", "last_agent_message": "Done"}))
    tracker.poll()
    assert watch.status == model.IDLE
    assert watch.main.status == model.DONE
    assert watch.main.last_action == model.Action("", "Done")
    assert watch in tracker.active_sessions()


def test_subagents_grouped_and_inherited_meta_cannot_replace_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/review")
    append(
        child,
        row("session_meta", {"id": "one", "cwd": "/wrong"}),
        row("response_item", {"type": "custom_tool_call", "name": "functions.exec", "input": "text(1)"}),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert list(tracker.watches) == ["codex:one"]
    child_agent = tracker.watches["codex:one"].running_subagents()[0]
    assert child_agent.label == "/root/review"
    assert child_agent.last_action == model.Action("functions.exec", "text(1)")
    assert tracker.watches["codex:one"].events[-1].who == "/root/review"
    tracker.poll()
    tracker.scan()
    assert len(tracker.watches["codex:one"].events) == 1
    append(child, row("event_msg", {"type": "turn_aborted"}))
    tracker.scan()
    assert not tracker.watches["codex:one"].running_subagents()


def test_malformed_partial_records_and_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write('broken\n{"type":"response_item","payload":null}\n')
        f.write(json.dumps(row("response_item", {"type": "function_call", "name": "exec_command", "arguments": "{"})))
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].main.tool_calls == 0
    with path.open("a", encoding="utf-8") as f:
        f.write("\n")
    tracker.poll()
    assert tracker.watches["codex:one"].main.tool_calls == 1
    path.write_text("", encoding="utf-8")
    rollout(tmp_path)
    tracker.scan()
    assert tracker.watches["codex:one"].main.tool_calls == 0
    assert not tracker.errors()


def test_stale_incomplete_turn_not_permanently_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    path.write_text("", encoding="utf-8")
    old = model.now() - timedelta(hours=2)
    append(path, row("session_meta", {"id": "one"}, old), row("event_msg", {"type": "task_started"}, old))
    tracker = model.Tracker()
    tracker.scan()
    assert not tracker.active_sessions()


def test_finished_parent_keeps_running_nested_child_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/review")
    rollout(tmp_path, "grandchild", "child", "/root/review/check")
    append(parent, row("event_msg", {"type": "task_complete"}))
    append(child, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    assert list(tracker.watches) == ["codex:one"]
    watch = tracker.watches["codex:one"]
    assert watch.state == model.BACKGROUND
    assert [a.label for a in watch.running_subagents()] == ["/root/review/check"]
    assert watch in tracker.active_sessions()


def test_model_survives_context_outside_activity_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    for _ in range(30):
        append(path, row("world_state", {"padding": "x" * 20000}))
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"pytest"}',
            },
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.main.model == "gpt-6-astra"
    assert watch.status == model.BUSY


def test_thread_spawn_metadata_groups_nested_agent(tmp_path, monkeypatch):
    """Codex versions that put ancestry only in source must not show a child as a root."""
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    rollout(tmp_path)
    child = tmp_path / "sessions/2026/10/31/rollout-child.jsonl"
    append(
        child,
        row(
            "session_meta",
            {
                "id": "child",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "one",
                            "agent_nickname": "Reviewer",
                            "depth": 1,
                        }
                    }
                },
            },
        ),
        row("event_msg", {"type": "task_started"}),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert list(tracker.watches) == ["codex:one"]
    assert [a.label for a in tracker.watches["codex:one"].running_subagents()] == ["Reviewer"]


def call(path, name, args, call_id="call"):
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(args),
            },
        ),
    )


def result(path, output, call_id="call"):
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        ),
    )


def test_background_command_survives_turn_completion_and_ends_on_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "pytest -q"})
    result(path, json.dumps({"session_id": 123, "output": "collecting\n10%\n"}))
    append(path, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.state == model.BACKGROUND
    (task,) = watch.running_bg_tasks()
    assert (task.task_id, task.description, task.line) == ("123", "pytest -q", "10%")
    call(path, "write_stdin", {"session_id": 123, "chars": ""}, "poll")
    result(path, json.dumps({"exit_code": 2, "output": "1 failed\n"}), "poll")
    tracker.poll()
    assert watch.running_bg_tasks() == []
    assert (task.status, task.exit_code, task.line) == ("failed", 2, "1 failed")
    assert task.ended is not None


def test_code_mode_output_blocks_track_multiple_commands_and_native_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "functions.exec",
                "call_id": "batch",
                "input": (
                    "text(await tools.exec_command({cmd:'sleep 10'})); "
                    "text(await tools.exec_command({cmd:'sleep 20'}));"
                ),
            },
        ),
    )
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "batch",
                "output": [
                    {"type": "input_text", "text": json.dumps({"session_id": 11, "output": "first\n"})},
                    {"type": "input_text", "text": json.dumps({"session_id": 22, "output": "second\n"})},
                ],
            },
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert [t.task_id for t in watch.running_bg_tasks()] == ["11", "22"]
    append(
        path,
        row(
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-1",
                    "process_id": "11",
                    "command": "sleep 10",
                    "cwd": "/tmp",
                    "source": "unified_exec_startup",
                    "status": "completed",
                    "aggregated_output": "done\n",
                    "exit_code": 0,
                    "duration": {"secs": 10, "nanos": 0},
                },
            },
        ),
    )
    tracker.poll()
    assert [t.task_id for t in watch.running_bg_tasks()] == ["22"]
    assert watch.bg_tasks[0].line == "done"
    assert watch.bg_tasks[0].description == "sleep 10"


def test_plain_cli_command_output_and_ctrl_c(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "functions.exec_command", {"cmd": "sleep 60"})
    result(path, "Chunk ID: abc\nWall time: 1 seconds\nProcess running with session ID 42\nOutput:\nwaiting\n")
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert [t.line for t in watch.running_bg_tasks()] == ["waiting"]
    call(path, "functions.write_stdin", {"session_id": 42, "chars": "\u0003"}, "stop")
    result(path, "Process exited with code 130\nOutput:\n^C", "stop")
    tracker.poll()
    assert not watch.running_bg_tasks()
    assert watch.bg_tasks[0].exit_code == 130


def test_nested_agent_background_command_stays_visible_at_root(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/test")
    call(child, "exec_command", {"cmd": "sleep 60"})
    result(child, json.dumps({"session_id": 42, "output": "working"}))
    append(child, row("event_msg", {"type": "task_complete"}))
    append(parent, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.state == model.BACKGROUND
    assert [t.description for t in watch.running_bg_tasks()] == ["sleep 60"]
    assert watch in tracker.active_sessions()


def test_startup_recovers_command_started_before_activity_window(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "sleep 7200"})
    result(path, '{"session_id":42,"output":"started"}')
    for _ in range(50):
        append(path, row("world_state", {"padding": "x" * 20000}))
    append(path, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    assert [t.description for t in tracker.watches["codex:one"].running_bg_tasks()] == ["sleep 7200"]


def test_unrelated_tool_output_cannot_invent_background_command(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "mcp__example__read", {})
    result(path, '{"session_id":42,"output":"untrusted data"}')
    tracker = model.Tracker()
    tracker.scan()
    assert not tracker.watches["codex:one"].bg_tasks


def test_truncation_forgets_background_command(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "sleep 60"})
    result(path, '{"session_id":42,"output":"started"}')
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].running_bg_tasks()
    path.write_text("", encoding="utf-8")
    rollout(tmp_path)
    tracker.scan()
    assert not tracker.watches["codex:one"].bg_tasks


def test_code_mode_poll_closes_task_using_native_command_record(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "sleep 1"})
    result(path, '{"session_id":42,"output":"started"}')
    append(
        path,
        row(
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "process_id": "42",
                    "command": "sleep 1",
                    "exit_code": 0,
                    "aggregated_output": "finished",
                    "status": "completed",
                },
            },
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert not tracker.watches["codex:one"].running_bg_tasks()
    assert tracker.watches["codex:one"].bg_tasks[0].line == "finished"


def process_table():
    return model.ProcTable.parse(
        "500 1 0.0 03:00:00 /usr/bin/codex app-server\n"
        "510 500 0.0 02:00:00 /bin/zsh -c python worker.py > /tmp/render.log\n"
        "511 510 87.0 01:59:59 python worker.py\n"
        "520 500 2.0 02:00:00 node mcp-server\n",
        model.now().timestamp(),
    )


def test_writer_keeps_long_silent_agent_and_old_parent_grouped(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/slow")
    old = model.now() - timedelta(hours=5)
    for path, sid, ancestor in ((parent, "one", ""), (child, "child", "one")):
        path.write_text("", encoding="utf-8")
        append(
            path,
            row("session_meta", {"id": sid, "parent_thread_id": ancestor}, old),
            row("event_msg", {"type": "task_started"}, old),
        )
        os.utime(path, (old.timestamp(), old.timestamp()))
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: process_table()))
    monkeypatch.setattr(model, "open_files", lambda pids: {500: [(10, str(parent)), (11, str(child))]})
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert [a.status for a in watch.subagents.values()] == [model.RUNNING]
    assert watch in tracker.active_sessions()
    assert watch.info.pid == 500


def test_codex_processes_cpu_redirected_log_and_process_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    log = tmp_path / "render.log"
    log.write_text("frame 12\nframe 13\n", encoding="utf-8")
    command = "python worker.py > /tmp/render.log"
    call(path, "exec_command", {"cmd": command})
    result(path, '{"session_id":42,"output":""}')
    table = process_table()
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(codex, "process_threads", lambda pids: {510: "one"}, raising=False)
    monkeypatch.setattr(
        model,
        "open_files",
        lambda pids: {
            500: [(10, str(path))],
            510: [(1, str(log))],
            511: [(1, str(log))],
        },
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert (watch.current_proc.pid, watch.current_proc.cpu) == (511, 87)
    (task,) = watch.running_bg_tasks()
    assert (task.log_path, task.line) == (str(log), "frame 13")
    table.procs.pop(510)
    table.procs.pop(511)
    table.kids.clear()
    tracker.scan()
    assert not watch.running_bg_tasks()
    assert task.status == "stale"  # disappearance proves exit, not an exit code
    assert task.log_path == ""


def test_code_mode_single_command_keeps_description(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "functions.exec",
                "call_id": "wrapped",
                "input": 'text(await tools.exec_command({cmd: "python render.py", yield_time_ms: 1000}));',
            },
        ),
    )
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "wrapped",
                "output": [
                    {"type": "input_text", "text": '{"session_id":42,"output":"frame 1"}'},
                ],
            },
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].bg_tasks[0].description == "python render.py"


def test_yielded_code_cell_is_background_work_until_wait_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "functions.exec",
                "call_id": "start",
                "input": "await work()",
            },
        ),
    )
    append(
        path,
        row(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "start",
                "output": [{"type": "input_text", "text": "Script running with cell ID cell-1\nOutput:\nworking"}],
            },
        ),
    )
    append(path, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.state == model.BACKGROUND
    assert [t.task_id for t in watch.running_bg_tasks()] == ["cell:cell-1"]
    call(path, "functions.wait", {"cell_id": "cell-1"}, "wait")
    result(path, [{"type": "input_text", "text": "Script completed\nOutput:\nfinished"}], "wait")
    tracker.poll()
    assert not watch.running_bg_tasks()
    assert watch.bg_tasks[0].status == "completed"
    assert watch.bg_tasks[0].line == "finished"


def test_unobserved_old_command_without_writer_becomes_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    old = model.now() - timedelta(hours=2)
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "old",
                "arguments": '{"cmd":"sleep 60"}',
            },
            old,
        ),
    )
    append(
        path,
        row(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "old",
                "output": '{"session_id":42,"output":"started"}',
            },
            old,
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    assert not tracker.watches["codex:one"].running_bg_tasks()
    assert tracker.watches["codex:one"].bg_tasks[0].status == "stale"


def test_large_image_record_does_not_hide_recent_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "mcp__browser__screenshot", {"title": "Open preview"})
    result(path, [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 1_500_000}])
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.main.last_action.arg == "Open preview"
    assert len(watch.events) == 1
    assert not tracker.errors()


def test_cli_codex_background_and_nested_agents_end_to_end(tmp_path):
    import os
    import subprocess
    import sys

    path = rollout(tmp_path, "parent")
    child = rollout(tmp_path, "child", "parent", "/root/reviewer")
    call(child, "exec_command", {"cmd": "python render.py"})
    result(child, '{"session_id":42,"output":"frame 12"}')
    append(path, row("event_msg", {"type": "task_complete"}))
    append(child, row("event_msg", {"type": "task_complete"}))
    env = {**os.environ, "CODEX_HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    got = subprocess.run(
        [sys.executable, "-m", "wfwatch", "--once", "--overview", "--width", "200"],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert got.returncode == 0, got.stderr
    assert "python render.py" in got.stdout
    assert "frame 12" in got.stdout
    assert "Codex" in got.stdout
    assert "Traceback" not in got.stderr


def test_process_thread_identity_of_real_child(tmp_path):
    import os
    import subprocess
    import sys

    import pytest

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "CODEX_THREAD_ID": "wfwatch-test-thread"},
    )
    try:
        if model.ProcTable.snapshot() is None:
            pytest.skip("OS process inspection is denied in this sandbox")
        assert codex.process_threads([child.pid]) == {child.pid: "wfwatch-test-thread"}
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_mcp_action_shows_server_and_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "mcp__blender__execute_blender_code", {"code": "print(1)"})
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].main.last_action.tool == "blender · execute_blender_code"


def test_native_completion_before_tool_result_does_not_resurrect_command(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "sleep 1"})
    append(
        path,
        row(
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "process_id": "42",
                    "command": "sleep 1",
                    "exit_code": 0,
                    "aggregated_output": "done",
                    "status": "completed",
                },
            },
        ),
    )
    result(path, '{"session_id":42,"output":""}')
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert not watch.running_bg_tasks()
    assert (watch.bg_tasks[0].exit_code, watch.bg_tasks[0].line) == (0, "done")


def test_old_parent_object_is_retained_while_child_updates(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    rollout(tmp_path, "child", "one", "/root/child")
    old = (model.now() - timedelta(hours=5)).timestamp()
    os.utime(parent, (old, old))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    tracker.scan()
    assert tracker.watches["codex:one"] is watch


def test_nested_command_process_is_visible_in_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/render")
    call(child, "exec_command", {"cmd": "python worker.py > /tmp/render.log"})
    result(child, '{"session_id":42,"output":"working"}')
    table = process_table()
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(model, "open_files", lambda pids: {500: [(10, str(parent)), (11, str(child))]})
    monkeypatch.setattr(codex, "process_threads", lambda pids: {510: "child"})
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].current_proc.pid == 511


@pytest.mark.parametrize("linux_ticks", [False, True])
def test_reused_process_id_is_not_proof_that_command_still_runs(tmp_path, monkeypatch, linux_ticks):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "python worker.py > /tmp/render.log"})
    result(path, '{"session_id":42,"output":"working"}')
    table = process_table()
    # A synthetic ps table must use synthetic /proc identities too: those PIDs
    # can belong to unrelated real runner processes on Linux.
    ticks = {510: 100, 511: 101} if linux_ticks else {}
    monkeypatch.setattr(model, "proc_start_ticks", lambda pid: ticks.get(pid))
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(model, "open_files", lambda pids: {500: [(10, str(path))]})
    monkeypatch.setattr(codex, "process_threads", lambda pids: {510: "one"})
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert watch.running_bg_tasks()
    table.procs[510].args = "unrelated-process"
    table.procs[511].args = "another-process"
    table.procs[510].elapsed = 1
    table.procs[511].elapsed = 1
    if linux_ticks:
        ticks.update({510: 200, 511: 201})
    tracker.scan()
    assert not watch.running_bg_tasks()


def test_command_output_removes_ansi_and_keeps_last_progress_line(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "python render.py"})
    result(path, json.dumps({"session_id": 42, "output": "frame 1\r\x1b[32mframe 2\x1b[0m\n"}))
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].bg_tasks[0].line == "frame 2"


def test_cli_real_writer_child_process_and_redirected_output(tmp_path):
    import os
    import subprocess
    import sys

    import pytest

    if sys.platform not in ("linux", "darwin") or model.ProcTable.snapshot() is None:
        pytest.skip("requires POSIX process inspection")
    path = rollout(tmp_path)
    command = "echo $$ >&4; printf 'frame 13\\n'; sleep 30"
    call(path, "exec_command", {"cmd": command})
    result(path, '{"session_id":42,"output":""}')
    append(path, row("event_msg", {"type": "task_complete"}))
    log = tmp_path / "render.log"
    driver_code = 'exec 3>>"$1"\nexec 4>&1\nCODEX_THREAD_ID=one /bin/sh -c "$3" > "$2" 2>&1 &\nwait'
    driver = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            'exec -a codex "$@"',
            "writer",
            "/bin/bash",
            "-c",
            driver_code,
            "writer",
            str(path),
            str(log),
            command,
        ],
        stdout=subprocess.PIPE,
        encoding="utf-8",
        start_new_session=True,
    )
    assert int(driver.stdout.readline()) > 0
    try:
        env = {**os.environ, "CODEX_HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
        got = subprocess.run(
            [sys.executable, "-m", "wfwatch", "--once", "--overview", "--width", "240"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert got.returncode == 0, got.stderr
        assert "«frame 13»" in got.stdout, got.stdout
        assert "sleep" in got.stdout
        assert "CPU" in got.stdout or "%" in got.stdout, got.stdout
    finally:
        import signal

        # Terminate the shell and its children, including an exec-optimized sleep.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(driver.pid, signal.SIGTERM)
        driver.wait(timeout=5)
        driver.stdout.close()


def test_hidden_process_environment_uses_unique_launch_match(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    call(path, "exec_command", {"cmd": "sleep 60"})
    result(path, '{"session_id":42,"output":"working"}')
    table = model.ProcTable.parse("500 1 0.0 01:00:00 codex\n510 500 5.0 00:00:01 sleep 60\n", model.now().timestamp())
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(model, "open_files", lambda pids: {500: [(10, str(path))]})
    monkeypatch.setattr(codex, "process_threads", lambda pids: {})
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].current_proc.pid == 510


def test_hidden_environment_never_assigns_ambiguous_process_to_two_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent = rollout(tmp_path)
    child = rollout(tmp_path, "child", "one", "/root/child")
    for path in (parent, child):
        call(path, "exec_command", {"cmd": "sleep 60"})
        result(path, '{"session_id":42,"output":"working"}')
    table = model.ProcTable.parse("500 1 0.0 01:00:00 codex\n510 500 5.0 00:00:01 sleep 60\n", model.now().timestamp())
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: table))
    monkeypatch.setattr(model, "open_files", lambda pids: {500: [(10, str(parent)), (11, str(child))]})
    monkeypatch.setattr(codex, "process_threads", lambda pids: {})
    tracker = model.Tracker()
    tracker.scan()
    assert tracker.watches["codex:one"].current_proc is None


def test_opaque_cell_ids_with_colons_complete_independently(tmp_path, monkeypatch):
    """Code-mode cell IDs include a generation and ordinal; neither may be dropped."""
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    for index, cell in enumerate(("g5:4", "g5:26")):
        call_id = f"start-{index}"
        append(
            path,
            row(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "name": "functions.exec",
                    "call_id": call_id,
                    "input": "await work()",
                },
            ),
        )
        append(
            path,
            row(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": call_id,
                    "output": [{"type": "input_text", "text": f"Script running with cell ID {cell}\nOutput:\n"}],
                },
            ),
        )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert {t.task_id for t in watch.running_bg_tasks()} == {"cell:g5:4", "cell:g5:26"}
    for index, cell in enumerate(("g5:4", "g5:26")):
        call(path, "functions.wait", {"cell_id": cell}, f"wait-{index}")
        result(path, [{"type": "input_text", "text": "Script completed\nOutput:\nfinished"}], f"wait-{index}")
        tracker.poll()
        assert len(watch.running_bg_tasks()) == 1 - index
    assert all(t.status == "completed" for t in watch.bg_tasks)
