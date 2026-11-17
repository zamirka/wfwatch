"""Read-only access to the OpenCode database: rows, windows and damaged data."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from opencode_home import OpenCodeHome

from wfwatch import model
from wfwatch.opencode import Store, StoreError, database_path


def test_database_path_follows_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("WFWATCH_OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert database_path() == tmp_path / "opencode" / "opencode.db"


def test_database_path_can_be_overridden(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "opencode.db"
    monkeypatch.setenv("WFWATCH_OPENCODE_DB", str(target))
    assert database_path() == target


def test_missing_database_is_a_store_error(tmp_path):
    with pytest.raises(StoreError):
        Store(tmp_path / "nope.db")


def test_corrupt_database_is_a_store_error(tmp_path):
    p = tmp_path / "opencode.db"
    p.write_bytes(b"not a database at all" * 100)
    with pytest.raises(StoreError):
        Store(p)


def test_database_without_the_session_tables_is_a_store_error(tmp_path):
    p = tmp_path / "opencode.db"
    c = sqlite3.connect(p)
    c.execute("create table unrelated (x)")
    c.commit()
    c.close()
    with pytest.raises(StoreError):
        Store(p)


def test_reads_recent_sessions_with_model_tokens_and_project(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session(
        "ses_a",
        title="Cache refactor",
        directory="/home/user/dev/demo",
        model_id="opencode/zen-1",
        busy=h.at(seconds=20),
        idle=h.at(minutes=2),
        tokens=(1200, 340, 90, 5000),
        cost=0.25,
    )
    with Store(h.db) as store:
        rows = store.recent_sessions(model.now() - timedelta(hours=3))
    assert [r.id for r in rows] == ["ses_a"]
    row = rows[0]
    assert row.title == "Cache refactor"
    assert row.directory == "/home/user/dev/demo"
    assert row.model == "opencode/zen-1"
    assert row.tokens_input == 1200
    assert row.tokens_output == 340
    assert row.tokens_reasoning == 90
    assert row.tokens_cache_read == 5000
    assert row.cost == pytest.approx(0.25)
    assert row.busy and row.idle and row.busy > row.idle
    assert row.worktree == "/home/user/dev/demo"


def test_recent_sessions_exclude_archived_and_old(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_live", created=h.at(minutes=1), updated=h.at(seconds=2))
    h.session("ses_old", created=h.at(hours=5), updated=h.at(hours=4))
    h.session("ses_archived", created=h.at(minutes=1), updated=h.at(seconds=2), archived=h.at(seconds=1))
    with Store(h.db) as store:
        rows = store.recent_sessions(model.now() - timedelta(hours=3))
    assert [r.id for r in rows] == ["ses_live"]


def test_sessions_by_ids_and_children(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_root", updated=h.at(seconds=2))
    h.session("ses_child", parent="ses_root", updated=h.at(seconds=2))
    h.session("ses_grandchild", parent="ses_child", created=h.at(hours=4), updated=h.at(hours=4))
    h.session("ses_other", updated=h.at(seconds=2))
    with Store(h.db) as store:
        children = store.children_of(["ses_root"])
        assert [r.id for r in children] == ["ses_child"]
        parents = store.sessions_by_ids(["ses_child"])
        assert [r.id for r in parents] == ["ses_child"]
        assert store.children_of(["ses_missing"]) == []


def test_messages_tail_and_incremental_window(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    ids = [h.message("ses_a", parts=[h.text(f"step {i}")], created=h.at(seconds=60 - i)) for i in range(8)]
    with Store(h.db) as store:
        tail = store.messages("ses_a", tail=3)
        assert [r.id for r in tail] == ids[-3:]
        since = model.now() - timedelta(seconds=55)
        got = store.messages("ses_a", since_ms=int(since.timestamp() * 1000))
        assert ids[-1] in [r.id for r in got]
        assert ids[0] not in [r.id for r in got]
        assert [r.seq for r in got] == sorted(r.seq for r in got)


def test_jobs_and_shell_outputs(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.session("ses_child", parent="ses_a")
    h.job("sh_1", "ses_a", command="pytest -q", title="tests")
    h.job("sh_2", "ses_a", child="ses_child", kind="subagent", title="review")
    out = h.shell_output("sh_1", "[3/7] ok\n")
    with Store(h.db) as store:
        jobs = store.jobs(["ses_a"])
        outputs = store.shell_outputs()
    assert [j.id for j in jobs] == ["sh_1", "sh_2"]
    assert jobs[0].command == "pytest -q"
    assert jobs[1].child_session_id == "ses_child"
    assert outputs["sh_1"] == out


def test_malformed_rows_do_not_break_the_store(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.conn.execute(
        "insert into session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " values ('bad', 'ses_a', 'assistant', 99, 1, 1, '{not json')"
    )
    h.conn.execute(
        "insert into session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " values ('nonobject', 'ses_a', 'assistant', 100, 1, 1, '[1,2]')"
    )
    h.conn.commit()
    with Store(h.db) as store:
        rows = store.messages("ses_a")
    assert [r.id for r in rows] == ["bad", "nonobject"]
    assert rows[0].data == {}
    assert rows[1].data == {}


def test_store_never_writes(opencode_home: OpenCodeHome):
    h = opencode_home
    h.session("ses_a")
    h.message("ses_a", parts=[h.text("hello")])
    before = hashlib.sha256(h.db.read_bytes()).hexdigest()
    with Store(h.db) as store:
        store.recent_sessions(model.now() - timedelta(hours=1))
        store.messages("ses_a", tail=10)
        assert store.read_only is True
    assert hashlib.sha256(h.db.read_bytes()).hexdigest() == before


def test_a_database_without_a_required_column_is_a_store_error(tmp_path):
    h = OpenCodeHome(tmp_path / "oc")
    try:
        h.conn.execute("alter table job drop column recovery")
        h.conn.commit()
        with pytest.raises(StoreError, match="recovery"):
            Store(h.db)
    finally:
        h.close()


def test_committed_rows_in_the_wal_are_visible(tmp_path):
    """OpenCode runs in WAL mode: a writer that has not checkpointed yet is normal, not corrupt."""
    h = OpenCodeHome(tmp_path / "oc")
    try:
        h.conn.execute("pragma journal_mode=wal")
        h.session("ses_a", title="A")
        h.message("ses_a", text="hello")
        with Store(h.db) as store:
            assert [r.id for r in store.recent_sessions(model.now() - timedelta(hours=1))] == ["ses_a"]
            assert [m.data.get("content") for m in store.messages("ses_a")] == [[{"type": "text", "text": "hello"}]]
    finally:
        h.close()


def test_output_files_outside_the_shell_directory_are_refused(tmp_path):
    """The output path comes from the database: it must never lead out of the shell directory."""
    shell = tmp_path / "data" / "shell"
    (shell / "project").mkdir(parents=True)
    inside = shell / "project" / "sh_1.out"
    inside.write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "secret.out"
    outside.write_text("no\n", encoding="utf-8")
    link = shell / "project" / "sh_link.out"
    link.symlink_to(outside)
    roots = (shell,)
    assert model.output_allowed(inside, roots, ".out") is True
    assert model.output_allowed(outside, roots, ".out") is False
    assert model.output_allowed(shell / ".." / "secret.out", roots, ".out") is False
    assert model.output_allowed(link, roots, ".out") is False
    assert model.output_allowed(inside, roots, ".output") is False
    assert model.output_allowed(Path("sh_1.out"), roots, ".out") is False
