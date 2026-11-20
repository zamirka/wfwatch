"""Opt-in integration lifecycle without reading the real Codex home."""

import json
from pathlib import Path

import pytest

from wfwatch import cli
from wfwatch import codex_setup as setup


def command(home, *args):
    return cli.main(["codex", *args, "--home", str(home)])


def test_preview_install_update_and_remove_preserve_user_content(tmp_path, capsys):
    home = tmp_path / "codex"
    assert command(home, "setup") == 0
    assert not home.exists()
    home.mkdir()
    original = "# My rules\n\nPreserve кириллица.\n".encode()
    (home / "AGENTS.md").write_bytes(original)
    assert command(home, "setup", "--apply") == 0
    assert (home / "AGENTS.md").read_bytes().startswith(original)
    manifest = json.loads((home / setup.MANIFEST).read_bytes())
    assert manifest["require_approval"] is False
    assert (home / setup.SKILL).read_bytes() == setup.skill()
    backups = list((home / ".wfwatch-backups").iterdir())
    assert (backups[0] / "AGENTS.md").read_bytes() == original
    assert command(home, "setup", "--apply") == 0
    assert list((home / ".wfwatch-backups").iterdir()) == backups
    assert command(home, "setup", "--require-approval", "--apply") == 0
    assert b"Before ANY tool use" in (home / "AGENTS.md").read_bytes()
    # An upgrade without a mode flag must not silently weaken approval policy.
    assert command(home, "setup", "--apply") == 0
    assert json.loads((home / setup.MANIFEST).read_bytes())["require_approval"] is True
    extra = b"\n# New personal rule\nKeep this too.\n"
    with (home / "AGENTS.md").open("ab") as stream:
        stream.write(extra)
    unrelated = home / "skills/other/SKILL.md"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"other skill")
    assert command(home, "remove") == 0
    assert (home / setup.SKILL).exists()
    assert command(home, "remove", "--apply") == 0
    assert (home / "AGENTS.md").read_bytes() == original + extra
    assert unrelated.read_bytes() == b"other skill"
    assert not (home / setup.SKILL).exists()
    assert not (home / setup.MANIFEST).exists()
    assert command(home, "remove", "--apply") == 0


def test_clean_remove_and_status(tmp_path, capsys):
    assert command(tmp_path, "setup", "--require-approval", "--apply") == 0
    capsys.readouterr()
    assert command(tmp_path, "status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["installed"] and status["skill_current"] and status["mode"] == "require-approval"
    assert command(tmp_path, "remove", "--apply") == 0
    assert not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("path", ["AGENTS.md", str(setup.SKILL)])
def test_modified_managed_content_is_not_overwritten(tmp_path, path):
    assert command(tmp_path, "setup", "--apply") == 0
    target = tmp_path / path
    target.write_bytes(target.read_bytes().replace(b"wfwatch", b"custom"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert command(tmp_path, "setup", "--apply") == 2
    assert command(tmp_path, "remove", "--apply") == 2
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_override_and_unmanaged_skill_are_reported(tmp_path):
    (tmp_path / "AGENTS.override.md").write_text("custom", encoding="utf-8")
    assert command(tmp_path, "setup", "--apply") == 2
    assert not (tmp_path / "AGENTS.md").exists()
    (tmp_path / "AGENTS.override.md").unlink()
    target = tmp_path / setup.SKILL
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing user skill")
    assert command(tmp_path, "setup", "--apply") == 2
    assert target.read_bytes() == b"existing user skill"


def test_respects_codex_home_and_updates_bundled_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cli.main(["codex", "setup", "--apply"]) == 0
    newer = setup.skill() + b"\nNew supported behavior.\n"
    monkeypatch.setattr(setup, "skill", lambda: newer)
    assert cli.main(["codex", "setup", "--apply"]) == 0
    assert (tmp_path / setup.SKILL).read_bytes() == newer


def test_failed_apply_rolls_back(tmp_path, monkeypatch):
    original = b"Keep this."
    (tmp_path / "AGENTS.md").write_bytes(original)
    atomic = setup.atomic_bytes

    def failing(path, data):
        if path == tmp_path / setup.SKILL:
            raise OSError("synthetic write failure")
        atomic(path, data)

    monkeypatch.setattr(setup, "atomic_bytes", failing)
    assert command(tmp_path, "setup", "--apply") == 2
    assert (tmp_path / "AGENTS.md").read_bytes() == original
    assert not (tmp_path / setup.MANIFEST).exists()
    assert not (tmp_path / setup.SKILL).exists()


def test_repository_skill_matches_packaged_template():
    path = Path(__file__).resolve().parents[1] / "skills/wfwatch-plan/SKILL.md"
    if path.exists():
        assert path.read_bytes() == setup.skill()


def test_missing_plan_help_and_corrupt_file_are_distinct(tmp_path, monkeypatch):
    from test_codex import rollout
    from test_codex_plan import text

    from wfwatch import codex, model, plans
    from wfwatch.cli import App

    sid = "12345678-1234-4234-8234-123456789abc"
    watch = codex.CodexWatch(rollout(tmp_path, sid))
    watch.poll()
    tracker = model.Tracker()
    tracker.watches[watch.session_id] = watch
    app = App(tracker, False)
    app.open(watch.session_id)
    assert "No structured plan" in text(
        app.current_session().screen(24, animate=False)
    ) or "No structured plan" in text(app.current_session().snapshot())
    app.on_key("p")
    painter = app.current_session()
    assert painter.plan_open
    assert "wfwatch codex setup" in text(painter.snapshot(), width=100)
    dest = plans.plan_path(sid)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"{broken")
    watch.poll()
    assert "Plan file unreadable" in text(painter.snapshot(), width=100)
