import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import wfwatch
from wfwatch import cli, model


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([flag])
    assert e.value.code == 0
    assert capsys.readouterr().out == f"wfwatch {wfwatch.__version__}\n"


def test_installed_version_is_the_package_version():
    from importlib import metadata

    try:
        installed = metadata.version("wfwatch")
    except metadata.PackageNotFoundError:
        pytest.skip("wfwatch is not installed (pip install -e .)")
    assert installed == wfwatch.__version__  # pyproject takes the version from wfwatch.__version__


def test_tests_never_see_the_real_claude_dir():
    real = Path.home() / ".claude"
    assert real not in (model.CLAUDE_DIR, Path(os.environ["CLAUDE_CONFIG_DIR"]))
    assert model.discover_runs() == [] and os.environ["COLUMNS"] == "80"


def test_python_dash_m(tmp_path):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path), "WFWATCH_LANG": "en"}
    run = [sys.executable, "-m", "wfwatch"]
    got = subprocess.run([*run, "--version"], capture_output=True, encoding="utf-8", env=env, timeout=60)
    assert (got.returncode, got.stdout) == (0, f"wfwatch {wfwatch.__version__}\n")
    got = subprocess.run([*run, "--once", "--width", "100"], capture_output=True, encoding="utf-8", env=env, timeout=60)
    assert got.returncode == 0, got.stderr
    assert "Traceback" not in got.stderr


def test_once_list_and_unknown_run(demo_run, capsys):
    assert cli.main(["--once", "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert "DEMO-FLOW" in out  # one workflow going (its own session does not count): straight to the diagram
    assert cli.main(["--once", "--overview", "--width", "120"]) == 0
    assert "overview" in capsys.readouterr().out
    assert cli.main(["--list", "--width", "100"]) == 0
    assert "wf_abc-123" in capsys.readouterr().out
    assert cli.main(["no-such-run"]) == 1
    assert "Run “no-such-run” not found" in capsys.readouterr().out
    assert cli.main(["[bold]x"]) == 1  # the query is printed as it is, not as Rich markup
    assert "“[bold]x”" in capsys.readouterr().out


@pytest.mark.parametrize("env", [{}, {"FORCE_COLOR": "1"}, {"TTY_COMPATIBLE": "1", "TTY_INTERACTIVE": "1"}])
def test_full_screen_needs_a_terminal(claude_home, capsys, monkeypatch, env):
    for k, v in env.items():  # Rich takes these as "a terminal", but a pipe is still a pipe
        monkeypatch.setenv(k, v)
    assert cli.main([]) == 2  # stdout is captured: not a terminal
    assert "--once" in capsys.readouterr().err


def test_full_screen_on_windows_points_to_wsl(claude_home, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.main([]) == 2
    assert "WSL" in capsys.readouterr().err
    assert cli.main(["--once"]) == 0  # the rest works


def test_ctrl_c_before_the_full_screen_has_no_traceback(claude_home, monkeypatch):
    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(model.Tracker, "scan", interrupted)
    assert cli.main(["--once"]) == 130
    monkeypatch.setattr(model, "load_sessions", interrupted)
    assert cli.main(["--list"]) == 130


pty = pytest.importorskip("pty")


def run_in_pty(home, action, timeout=20.0):
    """Start the full-screen mode in a pseudo-terminal, do `action(proc, master)`, wait for the exit.
    Returns (exit code, everything written to the terminal, terminal attributes afterwards)."""
    import fcntl
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    before = termios.tcgetattr(slave)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home), "TERM": "xterm-256color"}
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    p = subprocess.Popen(
        [sys.executable, "-m", "wfwatch.cli"], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    out = b""

    def read(until):
        nonlocal out
        end = time.monotonic() + timeout
        while time.monotonic() < end and not until():
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break

    try:
        read(lambda: b"\x1b[?1049h" in out and out.count(b"\x1b[H") >= 2)  # alternate screen, a couple of frames
        assert b"\x1b[?1049h" in out, out[-500:]
        action(p, master)
        read(lambda: p.poll() is not None)
        read(lambda: not select.select([master], [], [], 0.2)[0])  # drain the rest
        p.wait(timeout)
        return p.returncode, out, before, termios.tcgetattr(slave)
    finally:
        if p.poll() is None:
            p.kill()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ("how", "code"),
    [
        (lambda p, m: p.send_signal(signal.SIGTERM), 128 + signal.SIGTERM),
        (lambda p, m: p.send_signal(signal.SIGHUP), 128 + signal.SIGHUP),
        (lambda p, m: os.write(m, b"q"), 0),
        (lambda p, m: p.send_signal(signal.SIGINT), 0),
    ],
    ids=["SIGTERM", "SIGHUP", "q", "SIGINT"],
)
def test_terminal_is_restored(tmp_path, how, code):
    import termios

    rc, out, before, after = run_in_pty(tmp_path, how)
    assert rc == code, out[-800:]
    assert b"\x1b[?1049l" in out  # left the alternate screen
    assert b"\x1b[?25h" in out  # cursor shown again
    flags = termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
    assert after[3] & flags == before[3] & flags == flags  # echo and line mode are back
    assert b"Traceback" not in out
