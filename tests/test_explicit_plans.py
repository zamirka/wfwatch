"""Approved plans survive restarts, isolate sessions and render without native update_plan."""

import json

from test_codex import append, rollout
from test_codex_plan import text, update

from wfwatch import cli, codex, plans
from wfwatch.render import SessionPainter

SID = "12345678-1234-4234-8234-123456789abc"
OTHER = "12345678-1234-4234-8234-123456789def"


def create(session=SID, *extra):
    return cli.main(
        [
            "plan",
            "set",
            "--session",
            session,
            "--approval",
            "User: proceed",
            "--step",
            "Inspect",
            "--step",
            "Verify",
            *extra,
        ]
    )


def test_roundtrip_live_updates_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", SID)
    path = rollout(tmp_path, SID)
    watch = codex.CodexWatch(path)
    watch.poll()
    assert not watch.plan_steps
    assert create() == 0
    assert cli.main(["plan", "step", "1", "in_progress", "--revision", "1"]) == 0
    watch.poll()
    assert watch.plan_steps[0].status == "in_progress"
    assert cli.main(["plan", "step", "1", "completed", "--revision", "2"]) == 0
    assert cli.main(["plan", "step", "2", "in_progress", "--revision", "3"]) == 0
    reopened = codex.CodexWatch(path)
    reopened.poll()
    for view in ("cards", "graph"):
        rendered = text(SessionPainter(reopened, view=view).snapshot(height=45))
        assert "1/2" in rendered and "Inspect" in rendered and "Verify" in rendered
    other = codex.CodexWatch(rollout(tmp_path, OTHER))
    other.poll()
    assert not other.plan_steps
    assert create(OTHER) == 0
    assert plans.read(OTHER)["plan"][0]["status"] == "pending"


def test_rejected_writes_preserve_plan(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", SID)
    assert create() == 0
    original = plans.plan_path(SID).read_bytes()
    for args in (
        ["set", "--approval", "yes", "--step", "Overwrite"],
        ["step", "1", "completed", "--revision", "1"],
        ["step", "1", "in_progress", "--revision", "42"],
        ["step", "0", "in_progress", "--revision", "1"],
        ["set", "--replace", "--approval", "", "--step", "Bad"],
        ["set", "--replace", "--approval", "yes", "--step", ""],
    ):
        assert cli.main(["plan", *args]) == 2
        assert plans.plan_path(SID).read_bytes() == original
    assert cli.main(["plan", "step", "1", "in_progress", "--revision", "1"]) == 0
    assert cli.main(["plan", "step", "2", "in_progress", "--revision", "2"]) == 2
    assert create(SID, "--replace") == 0
    assert plans.read(SID)["revision"] == 3


def test_identity_missing_invalid_and_lock(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert cli.main(["plan", "show"]) == 2
    assert cli.main(["plan", "show", "--session", "../../escape"]) == 2
    assert create() == 0
    with plans.locked(plans.plan_path(SID)):
        assert create(SID, "--replace") == 2
    assert cli.main(["plan", "show", "--session", SID]) == 0


def test_overlay_fallback_and_corruption(tmp_path):
    path = rollout(tmp_path, SID)
    append(path, update([{"step": "Native", "status": "pending"}]))
    watch = codex.CodexWatch(path)
    assert create() == 0
    watch.poll()
    assert watch.plan_steps[0].step == "Inspect"
    append(path, update([{"step": "New native", "status": "in_progress"}]))
    watch.poll()
    assert watch.plan_steps[0].step == "Inspect"
    external = plans.plan_path(SID)
    original = external.read_bytes()
    for bad in (b"{", b"x" * (plans.MAX_BYTES + 1), json.dumps({"schema": 1}).encode()):
        external.write_bytes(bad)
        watch.poll()
        assert watch.plan_steps[0].step == "New native"
    external.write_bytes(original)
    watch.poll()
    assert watch.plan_steps[0].step == "Inspect"
    external.unlink()
    watch.poll()
    assert watch.plan_steps[0].step == "New native"
