"""Read / type into another process's console window (Windows).

ccfleet uses this to drive Claude Code TUIs it launched in their own consoles:
- peek(pid)  → visible screen text (VT escape codes stripped), to detect the trust prompt,
               usage-limit banners, context %, idle prompt…
- type_text(pid, text, enter=True) → injects key events (a nudge / follow-up prompt).
Only works for console processes on this machine (not VS Code-hosted sessions).

CLI: python console_io.py peek <pid> [lines] | python console_io.py type <pid> <text>
"""
from __future__ import annotations

import ctypes
import re
import time
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
VT_RE = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[78=>cM]|\[\d+G")


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


class _Attached:
    """Context manager: attach to pid's console (detaching ours first)."""

    def __init__(self, pid: int):
        self.pid = pid

    def __enter__(self):
        k32.FreeConsole()
        if not k32.AttachConsole(self.pid):
            raise OSError(f"AttachConsole({self.pid}) failed: {ctypes.get_last_error()}")
        return self

    def __exit__(self, *exc):
        k32.FreeConsole()


def _clean(line: str) -> str:
    return VT_RE.sub(" ", line).replace("\x1b", "").rstrip()


def peek(pid: int, lines: int = 40) -> list[str]:
    """Last `lines` non-empty rows of the console's visible window, escape codes stripped."""
    with _Attached(pid):
        h = k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        info = CSBI()
        k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info))
        width = info.dwSize.X
        rows: list[str] = []
        for y in range(info.srWindow.Top, info.srWindow.Bottom + 1):
            buf = ctypes.create_unicode_buffer(width)
            n = wintypes.DWORD()
            k32.ReadConsoleOutputCharacterW(h, buf, width, COORD(0, y), ctypes.byref(n))
            rows.append(_clean(buf.value[: n.value]))
        k32.CloseHandle(h)
    return [r for r in rows if r.strip()][-lines:]


def resize(pid: int, cols: int = 120, rows: int = 50) -> tuple[int, int]:
    """Grow the session's console window so tall TUI menus fit on screen.

    Claude Code only draws as many menu rows as the window has; with the default ~30-row window a
    9-option /model picker renders just its last few options, and `peek` can only ever read what was
    drawn (otherwise a phone showed only the last options). The screen buffer must be at least as large as
    the window, so widen/heighten the buffer first, then the window. Returns the new (cols, rows).
    """
    with _Attached(pid):
        h = k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        info = CSBI()
        k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info))
        # Windows caps the window at what fits the (virtual) screen — asking for more fails with
        # ERROR_INVALID_PARAMETER and leaves the console untouched, so clamp to the allowed maximum.
        largest = k32.GetLargestConsoleWindowSize(h)
        cols = min(cols, largest & 0xFFFF) or cols
        rows = min(rows, (largest >> 16) & 0xFFFF) or rows
        cols = max(cols, info.srWindow.Right - info.srWindow.Left + 1)
        rows = max(rows, info.srWindow.Bottom - info.srWindow.Top + 1)
        # keep the scrollback; only ensure the buffer is wide/tall enough for the new window
        k32.SetConsoleScreenBufferSize(h, COORD(cols, max(info.dwSize.Y, rows)))
        rect = SMALL_RECT(0, 0, cols - 1, rows - 1)
        k32.SetConsoleWindowInfo(h, True, ctypes.byref(rect))
        k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info))
        k32.CloseHandle(h)
        return (info.srWindow.Right - info.srWindow.Left + 1,
                info.srWindow.Bottom - info.srWindow.Top + 1)


def _key(ch: str, vk: int = 0, down: bool = True) -> INPUT_RECORD:
    rec = INPUT_RECORD()
    rec.EventType = 1  # KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = down
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk
    rec.Event.KeyEvent.UnicodeChar = ch
    return rec


def type_text(pid: int, text: str, enter: bool = True) -> None:
    """Inject text (+ Enter) as keyboard input into pid's console."""
    recs: list[INPUT_RECORD] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":  # Claude Code TUI: Shift+Enter-free newline isn't needed; send as space
            ch = " "
        # WCHAR holds one UTF-16 unit: an emoji / astral char goes in as its surrogate pair
        b = ch.encode("utf-16-le", "surrogatepass")
        for u in (chr(int.from_bytes(b[i:i + 2], "little")) for i in range(0, len(b), 2)):
            recs += [_key(u, 0, True), _key(u, 0, False)]
    with _Attached(pid):
        h = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        written = wintypes.DWORD()
        ok = True
        if recs:
            arr = (INPUT_RECORD * len(recs))(*recs)
            ok = k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(written))
        if enter and ok:
            # Claude Code treats a fast burst of input as a PASTE and an Enter inside it becomes a
            # newline — submit with a separate Enter after the TUI has absorbed the text.
            if recs:
                time.sleep(0.25 + min(1.5, len(recs) / 4000))
            ent = (INPUT_RECORD * 2)(_key("\r", 0x0D, True), _key("\r", 0x0D, False))
            ok = k32.WriteConsoleInputW(h, ent, 2, ctypes.byref(written))
        k32.CloseHandle(h)
    if not ok:
        raise OSError(f"WriteConsoleInputW failed: {ctypes.get_last_error()}")


def press_enter(pid: int) -> None:
    type_text(pid, "", enter=True)


SHIFT, CTRL = 0x0010, 0x0008  # dwControlKeyState: SHIFT_PRESSED, LEFT_CTRL_PRESSED
KEYS = {  # name -> (virtual key, char, modifier state)
    "enter": (0x0D, "\r", 0), "esc": (0x1B, "\x1b", 0), "tab": (0x09, "\t", 0),
    "shift+tab": (0x09, "\0", SHIFT), "up": (0x26, "\0", 0), "down": (0x28, "\0", 0),
    "left": (0x25, "\0", 0), "right": (0x27, "\0", 0), "backspace": (0x08, "\b", 0),
    "ctrl+c": (0x43, "\x03", CTRL), "ctrl+o": (0x4F, "\x0f", CTRL), "ctrl+r": (0x52, "\x12", CTRL),
    "space": (0x20, " ", 0), "y": (0x59, "y", 0), "n": (0x4E, "n", 0),
    "1": (0x31, "1", 0), "2": (0x32, "2", 0), "3": (0x33, "3", 0),
}


def send_keys(pid: int, names: list[str]) -> None:
    """Send special keys (Esc to interrupt, arrows/Enter in menus, Shift+Tab mode cycle, Ctrl+C…)."""
    recs: list[INPUT_RECORD] = []
    for n in names:
        vk, ch, mod = KEYS[n.lower()]
        for down in (True, False):
            r = _key(ch, vk, down)
            r.Event.KeyEvent.dwControlKeyState = mod
            recs.append(r)
    arr = (INPUT_RECORD * len(recs))(*recs)
    with _Attached(pid):
        h = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        written = wintypes.DWORD()
        ok = k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(written))
        k32.CloseHandle(h)
    if not ok:
        raise OSError(f"WriteConsoleInputW failed: {ctypes.get_last_error()}")


def _serve() -> None:
    """Persistent worker: read one JSON request per line on stdin, write one JSON line on stdout.
    Keeps the Python interpreter alive so ccfleet doesn't pay ~200 ms of process startup on every peek.
    Request: {"op":"peek","pid":123,"lines":45} | {"op":"type","pid":123,"text":"..."} |
             {"op":"keys","pid":123,"keys":["down","enter"]}
    Response: {"ok":true,"out":"<screen text>"} or {"ok":false,"err":"..."}"""
    import json
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req["op"]
            if op == "peek":
                res = {"ok": True, "out": "\n".join(peek(int(req["pid"]), int(req.get("lines", 40))))}
            elif op == "type":
                type_text(int(req["pid"]), req.get("text", ""))
                res = {"ok": True, "out": ""}
            elif op == "keys":
                send_keys(int(req["pid"]), list(req.get("keys", [])))
                res = {"ok": True, "out": ""}
            elif op == "resize":
                c, r = resize(int(req["pid"]), int(req.get("cols", 120)), int(req.get("rows", 50)))
                res = {"ok": True, "out": f"{c}x{r}"}
            elif op == "ping":
                res = {"ok": True, "out": "pong"}
            else:
                res = {"ok": False, "err": f"unknown op {op}"}
        except Exception as e:  # never crash the worker — report and keep serving
            res = {"ok": False, "err": f"{type(e).__name__}: {e}"}
        out.write(json.dumps(res) + "\n")
        out.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        _serve()
    elif len(sys.argv) >= 3 and sys.argv[1] == "peek":
        for line in peek(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 40):
            print(line)
    elif len(sys.argv) >= 4 and sys.argv[1] == "keys":
        send_keys(int(sys.argv[2]), sys.argv[3:])
    elif len(sys.argv) >= 4 and sys.argv[1] == "type":
        type_text(int(sys.argv[2]), " ".join(sys.argv[3:]))
    else:
        sys.exit(__doc__)
