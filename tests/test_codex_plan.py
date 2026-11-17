"""Codex plan state through rollout discovery and the rendered terminal."""

import io
import json

from rich.console import Console
from test_codex import append, rollout, row

from wfwatch import codex, i18n, model
from wfwatch.render import Overview, SessionPainter


def update(steps, kind="event_msg"):
    args = {"explanation": "Working through the checklist", "plan": steps}
    if kind == "event_msg":
        return row(kind, {"type": "plan_update", **args})
    return row(kind, {"type": "function_call", "name": "functions.update_plan", "arguments": json.dumps(args)})


def text(body, width=120):
    con = Console(file=io.StringIO(), record=True, width=width, legacy_windows=False)
    con.print(body)
    return con.export_text()


def test_plan_is_replayed_updated_and_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    i18n.use_lang("en")
    path = rollout(tmp_path)
    append(
        path,
        update(
            [
                {"step": "Inspect code", "status": "completed"},
                {"step": "Run tests", "status": "in_progress"},
                {"step": "Ship", "status": "pending"},
            ],
            "response_item",
        ),
    )
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert [(s.step, s.status) for s in watch.plan_steps] == [
        ("Inspect code", "completed"),
        ("Run tests", "in_progress"),
        ("Ship", "pending"),
    ]
    for view in ("cards", "graph"):
        output = text(SessionPainter(watch, view=view).snapshot(height=45))
        assert "1/3" in output and "Inspect code" in output and "Run tests" in output and "Ship" in output
    assert "1/3" in text(Overview([], [watch]).snapshot())
    append(path, update([{"step": "Ship", "status": "completed"}]))
    tracker.scan()
    assert [(s.step, s.status) for s in watch.plan_steps] == [("Ship", "completed")]
    reopened = codex.CodexWatch(path)
    reopened.poll()
    assert reopened.plan_steps == watch.plan_steps


def test_bad_plan_records_do_not_erase_plan_and_empty_plan_clears_it(tmp_path):
    path = rollout(tmp_path)
    append(path, update([{"step": "Keep this", "status": "pending"}]))
    watch = codex.CodexWatch(path)
    watch.poll()
    for bad in [None, "plan", [{"step": "bad", "status": "unknown"}], [{"step": 7, "status": "pending"}], [None]]:
        append(path, row("event_msg", {"type": "plan_update", "plan": bad}))
    # Text mentioning the protocol is not an update.
    append(path, row("event_msg", {"type": "agent_message", "message": 'update_plan {"plan": []}'}))
    watch.poll()
    assert [s.step for s in watch.plan_steps] == ["Keep this"]
    append(path, update([]))
    watch.poll()
    assert watch.plan_steps == []
    assert "Plan ·" not in text(SessionPainter(watch).snapshot())


def test_child_plan_is_separate_and_turn_end_does_not_complete_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    parent, child = rollout(tmp_path), rollout(tmp_path, "child", parent="one")
    append(parent, update([{"step": "Parent work", "status": "in_progress"}]))
    append(child, update([{"step": "Child work", "status": "completed"}]))
    append(parent, row("event_msg", {"type": "task_complete"}))
    tracker = model.Tracker()
    tracker.scan()
    watch = tracker.watches["codex:one"]
    assert [(s.step, s.status) for s in watch.plan_steps] == [("Parent work", "in_progress")]
    assert watch.children[0].plan_steps[0].step == "Child work"


def test_long_plan_is_safe_localized_and_reachable_in_small_terminal(tmp_path):
    path = rollout(tmp_path)
    append(
        path, update([{"step": f"Stage {i:02d} [red]literal[/red]\x1b\nnext", "status": "pending"} for i in range(24)])
    )
    watch = codex.CodexWatch(path)
    watch.poll()
    assert "\x1b" not in watch.plan_steps[0].step
    i18n.use_lang("ru")
    painter = SessionPainter(watch, view="cards")
    assert "План" in text(painter.snapshot(), width=60)
    assert "Stage 23" in text(painter.snapshot(), width=60)
    assert "[red]literal[/red]" in text(painter.snapshot(), width=60)
    for width, height in [(60, 12), (80, 24), (120, 40)]:
        for view in ("cards", "graph"):
            painter = SessionPainter(watch, view=view)
            output = text(painter.screen(height, animate=False), width=width)
            assert len(output.splitlines()) <= height
    painter.scroll = 100
    assert "Stage 23" in text(painter.screen(24, animate=False), width=60)


def test_replaced_rollout_drops_old_plan(tmp_path):
    path = rollout(tmp_path)
    append(path, update([{"step": "Old plan", "status": "in_progress"}]))
    watch = codex.CodexWatch(path)
    watch.poll()
    assert watch.plan_steps
    path.write_text("", encoding="utf-8")
    append(path, row("event_msg", {"type": "task_started"}))
    watch.poll()
    assert watch.plan_steps == []


def test_plan_before_activity_tail_survives_replay_and_cli(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.setattr(codex, "CODEX_DIR", tmp_path)
    path = rollout(tmp_path)
    append(
        path,
        update([{"step": "Historical step", "status": "completed"}, {"step": "Current step", "status": "in_progress"}]),
    )
    append(path, *(row("event_msg", {"type": "token_count", "padding": "x" * 2000}) for _ in range(270)))
    assert path.stat().st_size > model.SessionWatch.TAIL_BYTES
    watch = codex.CodexWatch(path)
    watch.poll()
    assert len(watch.plan_steps) == 2
    env = {
        **os.environ,
        "CODEX_HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "empty"),
        "WFWATCH_OPENCODE_DB": str(tmp_path / "missing.db"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "wfwatch", "--once", "--overview", "--lang", "en", "--width", "120"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    assert "Plan · 1/2" in result.stdout and "Current step" in result.stdout


def test_plan_graph_uses_workflow_phase_column(tmp_path):
    path = rollout(tmp_path)
    append(
        path,
        update(
            [
                {"step": "Discover", "status": "completed"},
                {"step": "Implement", "status": "in_progress"},
                {"step": "Verify", "status": "pending"},
            ]
        ),
    )
    watch = codex.CodexWatch(path)
    watch.poll()
    i18n.use_lang("en")
    output = text(SessionPainter(watch).snapshot(height=35))
    assert "phases" in output
    assert "1. Discover" in output and "2. Implement" in output and "3. Verify" in output
    assert "latest actions" not in output
