import os
import subprocess
import sys
import threading

import pytest

from wfwatch.keys import Keys, decode_keys


@pytest.mark.parametrize(
    ("data", "keys"),
    [
        ("\x1b[A", ["up"]),
        ("\x1b[B", ["down"]),
        ("\x1b[C", ["right"]),
        ("\x1b[D", ["left"]),
        ("\x1bOA", ["up"]),
        ("\x1bOD", ["left"]),  # application cursor mode
        ("\x1b[H", ["home"]),
        ("\x1b[F", ["end"]),
        ("\x1b[1~", ["home"]),
        ("\x1b[4~", ["end"]),
        ("\x1bOH", ["home"]),
        ("\x1b[5~", ["pgup"]),
        ("\x1b[6~", ["pgdn"]),
        ("\x1b[Z", ["shift-tab"]),
        ("\x1b[1;5A", ["up"]),
        ("\x1b[1;2B", ["down"]),  # with Ctrl/Shift
        # unknown sequences are dropped whole: no stray Esc, no digits that would open items
        ("\x1b[3~", []),
        ("\x1b[2~", []),
        ("\x1b[15~", []),
        ("\x1bOP", []),
        ("\x1b[<0;10;5M", []),
        ("\x1bx", []),
        ("\x1b", ["esc"]),
        ("\x1b\x1b", ["esc", "esc"]),
        ("\t", ["tab"]),
        ("\r", ["enter"]),
        ("\n", ["enter"]),
        ("\x7f", []),
        ("\x01", []),
        ("q1й²", ["q", "1", "й", "²"]),
        ("1\x1b[5~2\x1b[A", ["1", "pgup", "2", "up"]),
    ],
)
def test_decode_keys(data, keys):
    assert decode_keys(data) == (keys, "")


@pytest.mark.parametrize("data", ["\x1b", "\x1b[", "\x1b[1;5", "\x1bO"])
def test_decode_keys_keeps_an_incomplete_sequence(data):
    assert decode_keys("a" + data, final=False) == (["a"], data)


pty = pytest.importorskip("pty")


@pytest.fixture
def keys():
    master, slave = pty.openpty()
    k = Keys()
    k.fd = slave
    with k:
        yield k, master
    os.close(master)
    os.close(slave)


def test_sequence_split_across_reads(keys):
    k, master = keys
    k.ESC_WAIT = 0.5
    os.write(master, b"\x1b")
    threading.Timer(0.05, os.write, (master, b"[A")).start()  # ssh/tmux may deliver it in two pieces
    assert k.poll(1.0) == ["up"]


def test_lone_esc_and_multibyte_letters(keys):
    k, master = keys
    k.ESC_WAIT = 0.02
    os.write(master, b"\x1b")
    assert k.poll(1.0) == ["esc"]
    os.write(master, "й".encode()[:1])
    threading.Timer(0.05, os.write, (master, "й".encode()[1:])).start()
    got = k.poll(1.0)
    got += k.poll(1.0) if not got else []
    assert got == ["й"]


def test_no_termios_needed_outside_full_screen(tmp_path):
    """--list, --once and --help work without termios/tty (Windows)."""
    code = (
        "import sys; sys.modules['termios'] = None; sys.modules['tty'] = None\n"
        "from wfwatch.cli import main\n"
        "assert main(['--list']) == 0 and main(['--once', '--width', '100']) == 0\n"
        "import wfwatch.keys\n"
    )
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)}
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, encoding="utf-8", env=env, timeout=60, check=False
    )
    assert r.returncode == 0, r.stderr
