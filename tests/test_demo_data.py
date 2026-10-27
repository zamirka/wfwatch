"""The demo data (scripts/demo_data.py) and the screenshots made from it (scripts/screenshots.py).

The README tells people to try wfwatch on the demo data, and the screenshots in docs/ are made
from it, so it has to keep matching what wfwatch reads."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from wfwatch import cli, i18n, model, theme

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NAMES = ["release-prep", "cache-refactor", "docs-rewrite", "nightly-bench", "deps-bump"]


@pytest.fixture(scope="module")
def dd():
    path = SCRIPTS / "demo_data.py"
    if not path.exists():
        pytest.skip("scripts/ is not part of this source tree")
    spec = importlib.util.spec_from_file_location("demo_data", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def clock(monkeypatch, frozen_now):
    """A clock the test moves: seconds after frozen_now."""

    class Clock:
        t = 0.0

        def now(self):
            return frozen_now + timedelta(seconds=self.t)

    c = Clock()
    monkeypatch.setattr(model, "now", c.now)
    return c


def render(renderable, width: int, height: int | None = None) -> str:
    con = Console(record=True, width=width, height=height, file=io.StringIO())
    con.print(renderable)
    return con.export_text().rstrip("\n")


def build(dd, home: Path, lang: str = "en", start=None):
    demo = dd.Demo(home, lang, dict.fromkeys(NAMES, os.getpid()), start=start)
    demo.build()
    return demo


def test_the_start_shows_every_state(dd, claude_home, frozen_now):
    build(dd, claude_home.root, start=frozen_now)
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    runs = {r.name: r for r in tr.visible()}
    assert {n: r.state for n, r in runs.items()} == {"api-hardening": "running", "dependency-audit": "completed"}
    hardening = runs["api-hardening"]
    assert hardening.run_id.startswith("wf_1a2b")  # the README example `wfwatch wf_1a2b` opens it
    assert [p.title for p in hardening.phases()] == ["Survey", "Fix", "Review"]
    assert [p.status for p in hardening.phases()] == [model.DONE, model.RUNNING, model.PENDING]
    assert sorted(a.status for a in hardening.current_agents()) == [model.DONE] * 3 + [model.RUNNING] * 3
    assert all(a.model and a.tool_calls for a in hardening.current_agents())

    sessions = {s.name: s for s in tr.active_sessions()}
    assert {n: s.state for n, s in sessions.items()} == {
        "cache-refactor": model.BUSY,
        "docs-rewrite": model.WAITING,
        "nightly-bench": model.BACKGROUND,
        "release-prep": model.IDLE,
    }
    assert len(sessions["cache-refactor"].running_subagents()) == 2
    (bg,) = sessions["nightly-bench"].running_bg_tasks()
    assert bg.description == "Run benchmark suite" and bg.last_line.startswith("[")
    assert [t.status for t in sessions["nightly-bench"].bg_tasks] == ["completed", model.RUNNING]
    assert model.find_run("hardening") == hardening.run_dir


def test_the_story_plays_to_the_end(dd, claude_home, frozen_now, clock):
    demo = build(dd, claude_home.root, start=frozen_now)
    tr = model.Tracker()
    tr.scan()
    for step in range(0, 201, 5):
        clock.t = step
        demo.advance(step)
        if step % 15 == 0:
            tr.scan()
        tr.poll()
    assert demo.done == len(demo.ops)
    runs = {r.name: r for r in tr.visible()}
    hardening = runs["api-hardening"]
    assert hardening.state == "completed"
    assert [p.status for p in hardening.phases()] == [model.DONE] * 3
    assert len(hardening.current_agents()) == 8
    sessions = {s.name: s for s in tr.active_sessions()}
    bench = sessions["nightly-bench"]
    assert bench.running_bg_tasks() == [] and bench.bg_tasks[-1].exit_code == 0
    assert sessions["cache-refactor"].running_subagents() == []


def test_russian_content(dd, claude_home, frozen_now):
    build(dd, claude_home.root, lang="ru", start=frozen_now)
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    run = next(r for r in tr.visible() if r.name == "api-hardening")
    assert [p.title for p in run.phases()] == ["Анализ", "Исправления", "Ревью"]
    for f in claude_home.root.rglob("*"):
        if f.is_file():
            assert "\u2014" not in f.read_text(encoding="utf-8"), f  # no em dash: en dash only


def test_the_target_directory_is_checked(dd, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        dd.main([str(foreign)])
    assert (foreign / "keep.txt").exists()

    made = tmp_path / "made"
    assert dd.main([str(made), "--pid", str(os.getpid())]) == 0
    (made / "projects" / "stray.txt").write_text("x", encoding="utf-8")
    assert dd.main([str(made), "--pid", str(os.getpid())]) == 0  # made by the script: rebuilt from scratch
    assert not (made / "projects" / "stray.txt").exists() and (made / dd.MARKER).exists()


def test_once_on_the_demo_data(dd, tmp_path, monkeypatch, capsys):
    """What the README suggests: CLAUDE_CONFIG_DIR=<demo> wfwatch --once."""
    home = tmp_path / "demo"
    assert dd.main([str(home), "--pid", str(os.getpid())]) == 0
    monkeypatch.setattr(model, "CLAUDE_DIR", home)
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: None))
    capsys.readouterr()
    assert cli.main(["--once", "--width", "120"]) == 0
    out = capsys.readouterr().out
    for name in ("api-hardening", "dependency-audit", "cache-refactor", "docs-rewrite", "nightly-bench"):
        assert name in out
    assert "background: Run benchmark suite" in out


def test_screenshots_are_made_from_demo_data(dd, tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "screenshots.py"), "--no-gif", "--out", str(tmp_path)],
        capture_output=True,
        encoding="utf-8",  # explicit: CI makes EncodingWarning an error on Python 3.11+
        errors="replace",
        timeout=120,
        check=False,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "40"},  # the caller's terminal settings do not matter
    )
    assert r.returncode == 0, r.stderr
    svgs = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.svg")}
    assert set(svgs) == {"overview.svg", "workflow.svg", "session.svg", "overview.ru.svg"}
    for name, svg in svgs.items():
        assert "WFWATCH" in svg and "x&#160;1)" not in svg and "\u2014" not in svg, name
        assert theme.DARK.orange in svg  # in color, with the dark palette
        # a picture is a still frame: ● for what runs; ◐ is only the "background task" of nightly-bench
        assert not set("◓◑◒") & set(svg) and svg.count("◐") == (0 if name == "session.svg" else 1), name


def test_overview_header_fits_the_width(dd, claude_home, frozen_now):
    """All four session states and both kinds of workflows: in words when they fit, as icons when not."""
    build(dd, claude_home.root, start=frozen_now)
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    app = cli.App(tr, False)
    app.mode = "overview"
    for lang in ("en", "ru"):
        i18n.use_lang(lang)
        for w in (80, 100, 120, 200):
            first = render(app.frame(30), w, 30).split("\n")[0]
            assert "…" not in first, (lang, w, first)
            assert first.rstrip().endswith(("idle", "○ 1", "свободна", "свободны")), (lang, w, first)
    i18n.use_lang("en")
    first = render(app.frame(30), 120, 30).split("\n")[0]
    assert "1 waiting for you · 1 in background" in first, first
