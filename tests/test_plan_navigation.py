"""Real application key dispatch and rendering of long session plans."""

import pytest
from test_codex import append, rollout
from test_codex_plan import text, update

from wfwatch import codex, model
from wfwatch.cli import App
from wfwatch.graph import SessionGraph


def app_with_plan(tmp_path):
    path = rollout(tmp_path)
    steps = [
        {"step": f"Stage{i:02d} " + "long description " * 24 + f" END{i:02d}", "status": "pending"} for i in range(24)
    ]
    steps[2]["status"] = "in_progress"
    append(path, update(steps))
    watch = codex.CodexWatch(path)
    watch.poll()
    tracker = model.Tracker()
    tracker.watches[watch.session_id] = watch
    app = App(tracker, False)
    app.open(watch.session_id)
    return app, app.current_session()


def test_arrows_select_phases_and_follow(tmp_path):
    app, painter = app_with_plan(tmp_path)
    assert painter.selected_plan() == 2
    app.on_key("down")
    assert painter.selected_plan() == 3
    graph = SessionGraph(painter, model.now(), 0, animate=False)
    assert graph.checklist.selected_index == 3
    app.on_key("up")
    assert painter.selected_plan() == 2
    app.on_key("end")
    assert painter.selected_plan() == 23
    assert "24. Stage23" in text(painter.screen(55, animate=False), width=140)
    app.on_key("home")
    assert painter.selected_plan() == 0
    app.on_key("f")
    assert painter.plan_sel is None and painter.selected_plan() == 2
    assert "p" in [key for key, _, _ in app.keys(False).items]


@pytest.mark.parametrize("width,height", [(40, 10), (60, 12), (80, 24), (140, 24)])
def test_full_plan_every_end_reachable_and_escape_returns(tmp_path, width, height):
    app, painter = app_with_plan(tmp_path)
    app.on_key("p")
    assert painter.plan_open
    seen = ""
    for _ in range(600):
        output = text(painter.screen(height, keys=app.keys(False), animate=False), width=width)
        assert len(output.splitlines()) <= height
        seen += output
        if "END23" in output:
            break
        app.on_key("down")
    assert all(f"END{i:02d}" in seen for i in range(24))
    app.on_key("home")
    assert "Stage00" in text(painter.screen(height, animate=False), width=width)
    app.on_key("end")
    assert "END23" in text(painter.screen(height, animate=False), width=width)
    app.on_key("pgup")
    assert "END23" not in text(painter.screen(height, animate=False), width=width)
    app.on_key("esc")
    assert not painter.plan_open and app.mode == "detail"


def test_wrapped_cards_and_small_graph_fallback_scroll(tmp_path):
    app, painter = app_with_plan(tmp_path)
    for view in ("cards", "graph"):
        painter.view = view
        app.on_key("home")
        first = text(painter.screen(12, animate=False), width=40)
        app.on_key("down")
        assert first != text(painter.screen(12, animate=False), width=40)
        app.on_key("end")
        assert "END23" in text(painter.screen(12, animate=False), width=40)


def test_diagram_wraps_entire_titles_and_keeps_selection_visible(tmp_path):
    from wfwatch.graph import SIDE_W, TOP, session_state_text

    app, painter = app_with_plan(tmp_path)
    title = "Проверить длинное название этапа целиком без обрезки и наложения строк на следующий пункт"
    painter.s.plan_steps = [model.PlanStep(f"{title} FINISH{i}", "pending") for i in range(12)]
    for selected in (0, 5, 11):
        painter.plan_sel = selected
        graph = SessionGraph(painter, model.now(), 0, animate=False)
        assert graph.plan(140, 24) is not None
        canvas = graph.draw(140, 24)
        sidebar = "".join("".join(row[4 : SIDE_W - 2]) for row in canvas.ch[4:-1])
        node_x = SIDE_W + 4 + (140 - SIDE_W - 4) // 2 - 60 // 2 + 2
        title_width = 56 - session_state_text(painter.s, graph.at, 0, False).cell_len - 1
        title_rows = len(graph.title_lines(title_width))
        node = "".join("".join(row[node_x : node_x + title_width]) for row in canvas.ch[TOP + 1 : TOP + 1 + title_rows])
        expected = "".join(painter.s.plan_steps[selected].step.split())
        assert expected in "".join(sidebar.split())
        assert expected in "".join(node.split())
        assert "…" not in sidebar.split(f"FINISH{selected}")[0].split(f"{selected + 1}.")[-1]
        assert len(canvas.to_text().plain.splitlines()) <= 24


def test_wrapping_wide_glyphs_and_unbroken_words(tmp_path):
    from rich.cells import cell_len

    from wfwatch.graph import SIDE_W

    _, painter = app_with_plan(tmp_path)
    title = "界" * 30 + " " + "abcdefghij" * 7 + " END"
    painter.s.plan_steps = [model.PlanStep(title, "in_progress")]
    painter.plan_sel = None
    graph = SessionGraph(painter, model.now(), 0, animate=False)
    assert graph.plan(140, 40) is not None
    assert all(cell_len(line) <= SIDE_W - 6 for line in graph.checklist.title_lines(0))
    canvas = graph.draw(140, 40)
    sidebar = "".join("".join(row[4 : SIDE_W - 2]) for row in canvas.ch[4:-1])
    assert "".join(title.split()) in "".join(sidebar.split())


def test_oversized_title_falls_back_without_losing_text(tmp_path):
    app, painter = app_with_plan(tmp_path)
    painter.s.plan_steps = [model.PlanStep("long title " * 43 + " END", "in_progress")]
    assert SessionGraph(painter, model.now(), 0, animate=False).plan(140, 12) is None
    text(painter.screen(12, animate=False), width=140)
    app.on_key("end")
    assert "END" in text(painter.screen(12, animate=False), width=140)


def test_screen_reserves_height_for_wrapped_sidebar(tmp_path):
    _, painter = app_with_plan(tmp_path)
    painter.s.plan_steps = [model.PlanStep("abcdefghij " * 40 + " END", "in_progress")]
    output = text(painter.screen(42, animate=False), width=140)
    assert "phases" in output and "END" in output
    assert "does not fit" not in output
