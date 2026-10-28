"""Background commands of a session (Bash with run_in_background) and the processes behind them."""

import os
import subprocess
import sys

import pytest

from wfwatch import model

PS = """\
    1     0   0.0 61-05:13:33 /sbin/launchd
  500     1   1.0    02:00:00 /usr/local/bin/claude --resume
  510   500   0.0    01:59:00 node /home/user/.npm/_npx/mcp-server/index.js
  511   510  50.0    01:59:00 node worker.js
  520   500   0.0       05:00 /bin/zsh -c source /home/me/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'pytest -q'
  521   520   3.0       04:59 /usr/bin/python3 -m pytest -q
  522   521  95.5       04:58 /usr/bin/python3 -c worker
  523   521   0.1       04:58 tee out.log
  530   500   0.0       00:10 /bin/zsh -c source /home/me/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'sleep 60'
  531   530   0.0       00:10 sleep 60
  532   530   0.0    01:00:00 <defunct>
  524   522   0.0       04:00 <defunct>
  540   500   0.0       00:01 /bin/zsh -c source /home/me/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'cd x'
  600     1   0.0       00:30 /usr/local/bin/claude
garbage line
  700     1  x,y   bad-etime /bin/broken
"""


def table(text=PS, taken=None):
    return model.ProcTable.parse(text, taken if taken is not None else model.now().timestamp())


@pytest.mark.parametrize(
    ("s", "sec"),
    [("05", 5), ("01:02", 62), ("01:02:03", 3723), ("2-01:02:03", 176523), ("61-05:13:33", 61 * 86400 + 18813)],
)
def test_parse_etime(s, sec):
    assert model.parse_etime(s) == sec


def test_tool_processes():
    t = table()
    assert set(t.procs) == {1, 500, 510, 511, 520, 521, 522, 523, 524, 530, 531, 532, 540, 600}
    procs = t.tool_processes(500)
    # the busiest leaf of every Bash tool shell; MCP servers are not tool shells; an idle shell has no leaf;
    # zombies do not count (522 is a leaf although its zombie child is still listed)
    assert [(p.pid, p.name, p.cpu) for p in procs] == [(522, "python3", 95.5), (531, "sleep", 0.0)]
    assert procs[0].elapsed == 4 * 60 + 58
    assert t.tool_processes(600) == [] and t.tool_processes(0) == []


def test_pid_reuse_is_detected():
    t = table()
    at = model.now()
    from datetime import timedelta

    assert model.pid_alive(500, at - timedelta(hours=2), t)  # started 2 h ago, registered right after
    assert not model.pid_alive(600, at - timedelta(days=2), t)  # the session died; pid 600 is someone else now
    assert not model.pid_alive(4242, None, t)  # not running at all
    assert model.pid_alive(600, None, t)
    assert not model.pid_alive(0) and not model.pid_alive(-5) and not model.pid_alive(2**40)
    assert model.pid_alive(os.getpid())


def bg_session(h, sid="s-bg", status="idle", since_ms=None, output=True, launched_min=5, started_ms=None, pid=None):
    h.session(
        sid,
        status=status,
        since_ms=since_ms if since_ms is not None else h.ms_ago(hours=2),
        pid=pid,
        started_ms=started_ms,
    )
    out_file = h.root / "tmp" / "out" / "bgA.output"
    result_text = "Command running in background with ID: bgA."
    if output:
        result_text += f" Output is being written to: {out_file}. You will be notified when it completes."
    path = h.transcript(
        sid,
        h.assistant(
            h.ago(minutes=launched_min),
            "m1",
            [
                h.tool(
                    "Bash",
                    "toolu_A",
                    command="pytest -q\nmore",
                    description="Run the test suite",
                    run_in_background=True,
                )
            ],
        ),
        {
            "type": "user",
            "timestamp": h.ago(minutes=launched_min - 0.1),
            "toolUseResult": {"backgroundTaskId": "bgA"},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": result_text}]},
        },
        h.assistant(h.ago(minutes=4), "m2", [h.text("Started the tests in the background.")]),
    )
    return path, out_file


def test_background_command_lifecycle(claude_home):
    h = claude_home
    path, out_file = bg_session(h)
    out_file.parent.mkdir(parents=True)
    out_file.write_bytes(b"collecting\n\x1b[32m....\x1b[0m 40%\r\x1b[2K.... 60%\n\n")
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    (t,) = w.bg_tasks
    assert (t.task_id, t.description, t.status) == ("bgA", "Run the test suite", model.RUNNING)
    assert t.output_path == str(out_file) and t.last_line == ".... 60%" and t.output_size == out_file.stat().st_size
    assert t.output_mtime is not None and t.started is not None and t.elapsed(model.now()) > 200
    assert w.running_bg_tasks() == [t] and w.state == model.BACKGROUND and w.shown()

    with open(out_file, "ab") as f:
        f.write(b"3 passed in 1.2s\n")
    h.jl(
        path,
        h.notify(
            "bgA",
            "completed",
            h.ago(seconds=10),
            summary='Background command "Run the test suite" completed (exit code 0)',
            output=str(out_file),
        ),
    )
    w.poll()
    assert (t.status, t.exit_code, t.last_line) == ("completed", 0, "3 passed in 1.2s")
    assert t.ended is not None and "exit code 0" in t.summary
    assert w.running_bg_tasks() == [] and w.state == model.IDLE and not w.shown()  # idle for 2 h: hidden again
    assert w.recent_bg_tasks() == [t]


def test_notification_in_a_user_message_and_failure(claude_home):
    h = claude_home
    path, _ = bg_session(h, output=False)
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    h.jl(
        path,
        {
            "type": "user",
            "timestamp": h.ago(seconds=3),
            "message": {
                "role": "user",
                "content": "<task-notification>\n<task-id>bgA</task-id>\n<status>failed</status>\n"
                '<summary>Background command "Run the test suite" failed (exit code 2)</summary>\n</task-notification>',
            },
        },
    )
    w.poll()
    (t,) = w.bg_tasks
    assert (t.status, t.exit_code) == ("failed", 2)


def stop_result(h, ts, tool_use_id, task_id, is_error=False):
    """The result of a TaskStop call: a task stopped this way gets no notification."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"Stopped {task_id}"}
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": ts,
        "toolUseResult": {
            "message": f"Successfully stopped task: {task_id} (pytest -q)",
            "task_id": task_id,
            "task_type": "local_bash",
            "command": "pytest -q",
        },
        "message": {"content": [block]},
    }


def test_command_stopped_with_task_stop(claude_home):
    h = claude_home
    path, _ = bg_session(h)
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    h.jl(
        path,
        stop_result(h, h.ago(seconds=40), "toolu_nocall", "bgA"),  # no TaskStop call was made
        h.assistant(h.ago(seconds=35), "m5", [h.tool("TaskStop", "toolu_S1", task_id="bgA")]),
        stop_result(h, h.ago(seconds=34), "toolu_S1", "bgA", is_error=True),  # the stop failed
        h.assistant(h.ago(seconds=30), "m6", [h.tool("TaskStop", "toolu_S2", task_id="bgOther")]),
        stop_result(h, h.ago(seconds=29), "toolu_S2", "bgA"),  # the call named another task
    )
    w.poll()
    (t,) = w.bg_tasks
    assert (t.status, w.state) == (model.RUNNING, model.BACKGROUND)

    h.jl(
        path,
        h.assistant(h.ago(seconds=12), "m7", [h.tool("TaskStop", "toolu_S3", task_id="bgA")]),
        stop_result(h, h.ago(seconds=11), "toolu_S3", "bgA"),
        h.notify("bgA", "completed", h.ago(seconds=10)),  # a late notification does not undo the stop
    )
    w.poll()
    assert (t.status, t.exit_code) == ("killed", None) and t.ended is not None
    assert w.running_bg_tasks() == [] and w.state == model.IDLE


def test_output_path_is_derived_when_not_given(claude_home):
    h = claude_home
    path, _ = bg_session(h, output=False)
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    derived = w.output_path("bgA")
    assert derived.parts[-4:] == ("-p", "s-bg", "tasks", "bgA.output")
    derived.parent.mkdir(parents=True)
    derived.write_text("downloading 3/10\n", encoding="utf-8")
    w.poll()
    assert w.bg_tasks[0].last_line == "downloading 3/10"


def test_command_from_an_earlier_process_is_not_running(claude_home):
    h = claude_home
    path, _ = bg_session(h, launched_min=60, started_ms=h.ms_ago(minutes=30))  # the session restarted since
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    assert w.bg_tasks[0].status == model.STALE and w.running_bg_tasks() == [] and not w.shown()


def test_idle_session_running_a_background_command_is_shown(claude_home, monkeypatch):
    h = claude_home
    bg_session(h, pid=500)
    h.session("s-plain", status="idle", since_ms=h.ms_ago(hours=2), pid=600)
    h.transcript("s-plain", h.assistant(h.ago(hours=2), "m1", [h.text("done")]))
    t = table()
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: t))
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    assert list(tr.watches) == ["s-bg"]  # its tool shell is alive; the other idle session is not watched
    (w,) = tr.active_sessions()
    assert w.session_id == "s-bg" and w.state == model.BACKGROUND
    assert [p.name for p in w.procs] == ["python3", "sleep"] and w.current_proc.cpu == 95.5


def test_background_command_found_without_ps(claude_home):
    h = claude_home
    bg_session(h)
    tr = model.Tracker()  # ps unavailable (the fixture): a recently written transcript is checked instead
    tr.scan()
    tr.poll()
    assert [w.session_id for w in tr.active_sessions()] == ["s-bg"]
    assert tr.watches["s-bg"].procs == []


def test_launch_older_than_the_parsed_tail_is_still_found(claude_home, monkeypatch):
    """The launch is read from the whole transcript, not only the tail used for recent actions."""
    monkeypatch.setattr(model.SessionWatch, "TAIL_BYTES", 300)
    h = claude_home
    path, _ = bg_session(h)
    h.jl(path, *(h.assistant(h.ago(minutes=3), f"x{i}", [h.text("filler " * 20)]) for i in range(20)))
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    assert [t.task_id for t in w.running_bg_tasks()] == ["bgA"]


def test_own_process_is_not_a_tool_process():
    me = os.getpid()
    t = table(PS + f"{me:>5}   520   1.0       00:01 python -m wfwatch\n{me + 1:>5} {me:>5}  9.0       00:00 ps -A\n")
    assert [p.pid for p in t.tool_processes(500)] == [522, 531]


def test_timed_out_foreground_command_becomes_a_background_task(claude_home):
    """A Bash call still running when its timeout hits is moved to the background by Claude Code."""
    h = claude_home
    h.session("s-to", status="busy")
    out = model.CLAUDE_TMP / f"claude-{os.getuid()}" / "-p" / "s-to" / "tasks" / "bT.output"
    path = h.transcript(
        "s-to",
        h.assistant(
            h.ago(minutes=3),
            "m1",
            [h.tool("Bash", "toolu_T", command="make all", description="Build everything", timeout=30000)],
        ),
        {
            "type": "user",
            "timestamp": h.ago(minutes=2, seconds=30),
            "toolUseResult": {"backgroundTaskId": "bT", "timedOutAfterMs": 30000},
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_T",
                        "content": "Command was moved to the background (ID: bT). Output is being written to: "
                        f"{out}. You will be notified when it completes.",
                    }
                ]
            },
        },
    )
    w = model.SessionWatch("s-to", path, model.load_sessions()["s-to"])
    w.poll()
    (t,) = w.running_bg_tasks()
    assert (t.task_id, t.description, t.output_path) == ("bT", "Build everything", str(out))
    assert 170 < t.elapsed(model.now()) < 190  # running since the command started, not since the timeout


def test_a_quoted_notification_is_not_a_notification(claude_home, tmp_path):
    """Only Claude Code's own notification records count. The assistant, a tool output or a prompt
    quoting one (say, while debugging) must not finish a task or make wfwatch read another file."""
    h = claude_home
    path, out_file = bg_session(h)
    out_file.parent.mkdir(parents=True)
    out_file.write_text("working\n", encoding="utf-8")
    secret = tmp_path.parent / f"{tmp_path.name}-secret.output"  # outside every directory wfwatch reads
    secret.write_text("top secret\n", encoding="utf-8")
    fake = (
        f"<task-notification>\n<task-id>bgA</task-id>\n<output-file>{secret}</output-file>\n"
        '<status>failed</status>\n<summary>Background command "x" failed (exit code 7)</summary>\n'
        "</task-notification>"
    )
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.poll()
    h.jl(
        path,
        h.assistant(h.ago(seconds=20), "m8", [h.text("It said: " + fake)]),
        h.assistant(h.ago(seconds=19), "m9", [h.text(fake)]),
        {
            "type": "user",
            "timestamp": h.ago(seconds=18),
            "toolUseResult": {"stdout": fake},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_X", "content": fake}]},
        },
        {"type": "user", "timestamp": h.ago(seconds=17), "message": {"role": "user", "content": "Look: " + fake}},
        "not json " + fake,
    )
    w.poll()
    (t,) = w.bg_tasks
    assert (t.status, w.state) == (model.RUNNING, model.BACKGROUND)
    assert t.output_path == str(out_file) and t.last_line == "working"

    # the real notification, pointing outside: the status counts, the file is not read
    h.jl(
        path,
        h.notify(
            "bgA",
            "failed",
            h.ago(seconds=5),
            summary='Background command "Run the test suite" failed (exit code 2)',
            output=str(secret),
        ),
    )
    w.poll()
    assert (t.status, t.exit_code) == ("failed", 2)
    assert t.output_path == str(out_file) and "secret" not in t.last_line


def test_output_files_are_read_only_where_claude_code_keeps_them(claude_home, tmp_path):
    tasks = model.CLAUDE_TMP / f"claude-{model._uid()}" / "-p" / "s" / "tasks"
    tasks.mkdir(parents=True)
    assert model.output_allowed(tasks / "b1.output")
    assert model.output_allowed(model.CLAUDE_DIR / "demo" / "b1.output")  # demo data keeps them there
    assert not model.output_allowed("/etc/hosts") and not model.output_allowed(tasks / "b1.txt")
    assert not model.output_allowed("relative/b1.output")
    assert not model.output_allowed(tasks.joinpath(*[".."] * 6, "x.output"))  # resolves outside CLAUDE_DIR
    target = tmp_path.parent / f"{tmp_path.name}-target.output"
    target.write_text("x\n", encoding="utf-8")
    (tasks / "link.output").symlink_to(target)
    assert not model.output_allowed(tasks / "link.output")  # a symlink pointing outside

    # a task id is data too: "../" in it leads nowhere
    h = claude_home
    path, _ = bg_session(h, output=False)
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    evil = "../../../../../../" + str(target.with_suffix(""))
    assert not model.output_allowed(w.output_path(evil))


# ---- a command that sends its output to a file of its own (`> render.log 2>&1`) ----

LSOF = """\
p520
f1
aw
tREG
n/tmp/claude-501/-p/s-bg/tasks/bgA.output
f2
aw
tREG
n/tmp/claude-501/-p/s-bg/tasks/bgA.output
p522
f1
aw
tREG
n/work/render.log
f2
au
tREG
n/work/render.log
p531
f1
aw
tCHR
n/dev/null
f2
ar
tREG
n/work/read-only.txt
garbage
f3
"""


def test_parse_lsof():
    """Regular files open for writing, with their descriptors; /dev/null and a file opened for reading do not count."""
    assert model.parse_lsof(LSOF) == {
        520: [(1, "/tmp/claude-501/-p/s-bg/tasks/bgA.output"), (2, "/tmp/claude-501/-p/s-bg/tasks/bgA.output")],
        522: [(1, "/work/render.log"), (2, "/work/render.log")],
    }
    assert model.parse_lsof("p1\nfcwd\naw\ntREG\nn/x\n") == {}  # not a descriptor
    assert model.parse_lsof("") == {}


def test_tool_shells_pair_each_shell_with_its_busiest_leaf():
    assert sorted((sh.pid, leaf.pid) for sh, leaf in table().tool_shells(500)) == [(520, 522), (530, 531)]


@pytest.mark.skipif(sys.platform == "win32", reason="no lsof and no /proc")
def test_open_files_of_a_real_process(tmp_path):
    log = tmp_path / "render.log"
    with open(log, "w") as f:
        p = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
    try:
        got = model.open_files([p.pid, 2**22 + 7])  # a pid that is not there is skipped
    finally:
        p.kill()
        p.wait()
    real = os.path.realpath(log)
    assert got == {p.pid: [(1, real), (2, real)]}
    assert model.open_files([]) == {}


def redirected(h, log_text: bytes | None, out_text: bytes = b""):
    """A running background command whose current process writes to render.log, not to its .output."""
    path, out_file = bg_session(h)
    out_file.parent.mkdir(parents=True)
    out_file.write_bytes(out_text)
    log = h.root / "work" / "render.log"
    if log_text is not None:
        log.parent.mkdir(parents=True)
        log.write_bytes(log_text)
    w = model.SessionWatch("s-bg", path, model.load_sessions()["s-bg"])
    w.redirects = {os.path.realpath(out_file): str(log)}
    return w, out_file, log


def test_last_line_of_the_file_the_command_writes_instead(claude_home):
    h = claude_home
    w, out_file, log = redirected(h, b"Fra: 19 | Saved\nFra: 20 | Rendering 25 / 64 samples\n")
    w.poll()
    (t,) = w.bg_tasks
    assert t.log_path == str(log) and t.line == "Fra: 20 | Rendering 25 / 64 samples"
    assert t.last_line == ""  # the .output itself is still empty

    # whichever was written last: the command printed something to its .output after the log
    out_file.write_bytes(b"150\n")
    os.utime(log, (1, 1))
    w.poll()
    assert t.line == "150"

    # the render is over: the next process writes to the .output again
    os.utime(log, None)
    w.redirects = {}
    w.poll()
    assert (t.log_path, t.log_line, t.line) == ("", "", "150")


def test_log_is_forgotten_when_the_command_ends(claude_home):
    h = claude_home
    w, out_file, _ = redirected(h, b"Fra: 150 | Saved\n", out_text=b"")
    w.poll()
    assert w.bg_tasks[0].line == "Fra: 150 | Saved"
    out_file.write_bytes(b"done\n")
    h.jl(w.log.path, h.notify("bgA", "completed", h.ago(seconds=1), output=str(out_file)))
    w.poll()
    (t,) = w.bg_tasks
    assert (t.status, t.log_path, t.line) == ("completed", "", "done")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")
def test_log_that_is_not_a_regular_file_is_not_read(claude_home):
    """A pipe in place of the log: opening it for reading must not block, and nothing is shown."""
    h = claude_home
    w, _, log = redirected(h, None)
    log.parent.mkdir(parents=True)
    os.mkfifo(log)
    w.poll()
    assert w.bg_tasks[0].line == ""


OUT, LOG = "<out>", "<log>"


@pytest.mark.parametrize(
    ("files", "found"),
    [
        # `blender -b ... > render.log 2>&1`: the shell writes to the .output, the process it runs to the log
        ({520: [(1, OUT), (2, OUT)], 522: [(1, LOG), (2, LOG)]}, True),
        # `for ...; done > render.log 2>&1`: the shell redirects itself and keeps the .output on a spare descriptor
        ({520: [(1, LOG), (2, LOG), (12, OUT), (14, OUT)], 522: [(1, LOG), (2, LOG)]}, True),
        # the process writes to the .output; another file it writes (a video) is not its output
        ({520: [(1, OUT), (2, OUT)], 522: [(1, OUT), (2, OUT), (5, "/w/video.mp4")]}, False),
        # a shell that is not running this command (a foreground call has an .output of its own)
        ({530: [(1, "/x/fg.output")], 531: [(1, LOG)]}, False),
    ],
)
def test_tracker_finds_the_log_through_the_processes(claude_home, monkeypatch, files, found):
    h = claude_home
    _, out_file = bg_session(h, pid=500)
    out_file.parent.mkdir(parents=True)
    out_file.write_bytes(b"")
    log = h.root / "work" / "render.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"Fra: 7 | Rendering 1 / 64 samples\n")
    t = table()
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: t))
    names = {OUT: os.path.realpath(out_file), LOG: str(log)}
    asked = []

    def fake_open_files(pids):
        asked.append(set(pids))
        return {pid: [(fd, names.get(f, f)) for fd, f in fs] for pid, fs in files.items()}

    monkeypatch.setattr(model, "open_files", fake_open_files)
    tr = model.Tracker()
    tr.scan()  # --once draws right after the first scan: the line must be there already
    w = tr.watches["s-bg"]
    assert asked == [{520, 522, 530, 531}]
    assert w.redirects == ({os.path.realpath(out_file): str(log)} if found else {})
    assert w.bg_tasks[0].line == ("Fra: 7 | Rendering 1 / 64 samples" if found else "")


def test_no_lsof_without_running_background_commands(claude_home, monkeypatch):
    h = claude_home
    h.session("s-busy", status="busy", since_ms=h.ms_ago(seconds=5), pid=500)
    h.transcript("s-busy", h.assistant(h.ago(seconds=5), "m1", [h.tool("Bash", "t1", command="pytest")]))
    t = table()
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: t))
    monkeypatch.setattr(model, "open_files", lambda pids: pytest.fail("lsof for a session without background commands"))
    tr = model.Tracker()
    tr.scan()
    tr.poll()
    assert tr.watches["s-busy"].redirects == {}
