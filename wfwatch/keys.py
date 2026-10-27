"""Key presses without Enter (cbreak mode); Ctrl+C still interrupts.

termios/tty exist only on POSIX, so they are imported when the keyboard is actually used:
--list, --once, --help and the tests work everywhere."""

from __future__ import annotations

import codecs
import contextlib
import os
import sys
import time

_PLAIN = {"\t": "tab", "\r": "enter", "\n": "enter"}
_CSI = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab"}
_CSI_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn"}
_SS3 = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}


def _csi(params: str, final: str) -> str | None:
    if final == "~":
        return _CSI_TILDE.get(params.split(";")[0])
    return _CSI.get(final)  # arrows with modifiers (Shift/Ctrl) count as plain arrows


def decode_keys(data: str, final: bool = True) -> tuple[list[str], str]:
    """Split terminal input into key names.

    Escape sequences are consumed whole: CSI (ESC [ ... up to a final byte 0x40–0x7E) and
    SS3 (ESC O x). Known ones become names (up, down, left, right, home, end, pgup, pgdn,
    shift-tab), unknown ones (Delete, F-keys, Alt+key, mouse) are dropped, so they never turn
    into stray Esc and digit presses. A sequence cut off at the end of ``data`` is returned as
    the remainder, unless ``final`` is set: then a lone ESC is the Esc key and an incomplete
    sequence is dropped."""
    out: list[str] = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c != "\x1b":
            if c in _PLAIN:
                out.append(_PLAIN[c])
            elif c >= " " and c != "\x7f":
                out.append(c)
            i += 1
            continue
        if i + 1 == n:  # ESC is the last thing read
            if not final:
                return out, data[i:]
            out.append("esc")
            break
        nxt = data[i + 1]
        if nxt == "[":
            j = i + 2
            while j < n and not "\x40" <= data[j] <= "\x7e":
                j += 1
            if j == n:
                if not final:
                    return out, data[i:]
                break
            name = _csi(data[i + 2 : j], data[j])
            i = j + 1
        elif nxt == "O":
            if i + 2 == n:
                if not final:
                    return out, data[i:]
                break
            name = _SS3.get(data[i + 2])
            i += 3
        elif nxt == "\x1b":  # Esc pressed twice
            name = "esc"
            i += 1
        else:  # Alt+key
            name = None
            i += 2
        if name:
            out.append(name)
    return out, ""


class Keys:
    ESC_WAIT = 0.03  # an escape sequence may arrive in pieces (ssh, tmux): wait this long for the rest

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self._old = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __enter__(self):
        if self.fd is not None:
            import termios
            import tty

            self._old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._old is not None:
            import termios

            with contextlib.suppress(termios.error, OSError):  # the terminal may be gone
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)

    def _wait(self, timeout: float) -> bool:
        import select

        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(r)

    def _read(self) -> str:
        try:
            b = os.read(self.fd, 1024)
        except OSError:
            b = b""
        if not b:  # end of input: stop listening instead of spinning on it
            self.fd = None
            return ""
        return self._decoder.decode(b)

    def poll(self, timeout: float) -> list[str]:
        """Wait up to ``timeout`` seconds; return the keys pressed."""
        if self.fd is None:
            time.sleep(timeout)
            return []
        if not self._wait(timeout):
            return []
        keys, rest = decode_keys(self._read(), final=False)
        if rest and self.fd is not None and self._wait(self.ESC_WAIT):
            more, rest = decode_keys(rest + self._read(), final=False)
            keys += more
        if rest:
            keys += decode_keys(rest, final=True)[0]
        return keys
