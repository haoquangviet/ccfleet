"""Drive Claude Code TUIs running inside tmux (macOS; Linux experimental).

Each ccfleet session runs in its own detached tmux session named `ccfleet_<name>` on ccfleet's OWN
tmux server (`tmux -L ccfleet`), so it never touches the user's tmux sessions or another tool's.
ccfleet tracks the pane's process id (the `claude` process itself — the command is exec'd, not run
through a shell), exactly like it tracks claude.exe's pid on Windows; peek/type/keys map that pid
back to its pane.

- peek(pid)        → the pane's visible text (`capture-pane -p`)
- type_text(pid,t) → the text (literal keys, or one bracketed paste when long / multi-line), then a
                     SEPARATE Enter: Claude Code treats a fast burst as a paste and swallows an
                     inline Enter (same rule as the Windows console_io)
- send_keys(pid,k) → named keys (enter, esc, arrows, shift+tab = BTab, ctrl+c…), one call per key
- resize(pid,c,r)  → `resize-window`, so tall menus render in full

Watch a session live:  tmux -L ccfleet attach -t ccfleet_<name>   (detach: Ctrl+B, then D)

CLI (debugging): python console_tmux.py ls | peek <pid> [lines] | type <pid> <text> | keys <pid> <k...>
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid

SOCKET = os.environ.get("CCFLEET_TMUX_SOCKET") or "ccfleet"  # always a private server, never the default one
PREFIX = "ccfleet_"
COLS, ROWS = 120, 50   # tall enough that a 9-option /model picker renders in full
_VT_RE = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[78=>cM]")

KEYS = {  # ccfleet key name -> tmux send-keys token (same names as console_io.KEYS)
    "enter": "Enter", "esc": "Escape", "tab": "Tab", "shift+tab": "BTab",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "backspace": "BSpace", "space": "Space", "ctrl+c": "C-c", "ctrl+o": "C-o", "ctrl+r": "C-r",
    "y": "y", "n": "n", "1": "1", "2": "2", "3": "3",
}

INSTALL_HINT = ("tmux is required to run sessions on this OS — install it: "
                "`brew install tmux` (macOS) or `sudo apt install tmux` / `sudo dnf install tmux` (Linux)")
_tmux_path: str | None = None


def tmux_bin() -> str:
    """Path of the tmux binary. launchd / systemd give a minimal PATH, so look in the usual places too."""
    global _tmux_path
    if _tmux_path:
        return _tmux_path
    for c in (shutil.which("tmux"), "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if c and os.path.exists(c):
            _tmux_path = c
            return c
    raise RuntimeError(INSTALL_HINT)


def check() -> str:
    """'tmux 3.4' when usable, else RuntimeError with an install hint (tmux >= 3.0 is needed)."""
    try:
        out = subprocess.run([tmux_bin(), "-V"], capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError as e:
        raise RuntimeError(f"{INSTALL_HINT} ({e})")
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and int(m.group(1)) < 3:
        raise RuntimeError(f"{out} is too old — ccfleet needs tmux 3.0 or newer ({INSTALL_HINT})")
    return out


def _tmux(*args: str) -> list[str]:
    # -u: UTF-8 even when launchd/systemd gave us no UTF-8 locale; -L: ccfleet's own server
    return [tmux_bin(), "-u", "-L", SOCKET, *args]


def _run(*args: str, timeout: int = 10, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(_tmux(*args), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, input=input)


def session_name(name: str) -> str:
    """tmux session name for a ccfleet session ('.' and ':' are tmux target syntax)."""
    return PREFIX + (re.sub(r"[^A-Za-z0-9_-]", "_", name or "") or "session")


def panes() -> list[dict]:
    """Every pane on ccfleet's tmux server: [{pid, pane, session}] ([] when no server runs)."""
    r = _run("list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{session_name}")
    out = []
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit():
            out.append({"pid": int(parts[0]), "pane": parts[1], "session": parts[2]})
    return out


def _pane_of(pid) -> str:
    """Pane id (%N) running `pid`; OSError when it isn't one of ccfleet's panes."""
    for p in panes():
        if p["pid"] == int(pid):
            return p["pane"]
    raise OSError(f"pid {pid} is not running in a ccfleet tmux session (tmux -L {SOCKET} ls)")


def owns(pid) -> bool:
    try:
        _pane_of(pid)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


_PASS_PREFIXES = ("CLAUDE_", "CCFLEET_", "ANTHROPIC_BASE", "LANG", "LC_")
_PASS_ALWAYS = ("PATH", "HOME", "USER", "SHELL")
_SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL", "COOKIE", "AUTH")


def env_args(env: dict) -> list[str]:
    """`/usr/bin/env -u … K=V …` prefix for the session command.

    A running tmux server gives new panes ITS environment (from whenever it started), not ours — so a
    session would silently run on the wrong account (CLAUDE_CONFIG_DIR). Pass what matters explicitly
    and unset CLAUDE_CONFIG_DIR when the account uses the default dir. Never secrets: argv is
    readable by every local user through `ps`."""
    drop = [k for k in ("CLAUDE_CONFIG_DIR",) if k not in env]
    sets = []
    for k, v in env.items():
        if any(w in k.upper() for w in _SECRET_WORDS) or "\n" in v:
            continue
        if k in _PASS_ALWAYS or k.startswith(_PASS_PREFIXES) or os.environ.get(k) != v:
            sets.append(f"{k}={v}")
    return ["/usr/bin/env"] + [x for k in drop for x in ("-u", k)] + sets


def launch(args: list[str], cwd: str | None, env: dict, cols: int = COLS, rows: int = ROWS) -> int:
    """Start `args` in a detached tmux session named after env CCFLEET_SESSION; return the pane pid
    (= the program's pid: tmux execs a multi-word command directly, no shell)."""
    check()
    sess = session_name(env.get("CCFLEET_SESSION") or "session")
    _run("kill-session", "-t", f"={sess}")  # a leftover pane with this name (crashed supervisor)
    cmd = ["new-session", "-d", "-s", sess, "-x", str(cols), "-y", str(rows), "-P", "-F", "#{pane_pid}"]
    if cwd:
        cmd += ["-c", str(cwd)]
    cmd += env_args(env) + [str(a) for a in args]
    r = subprocess.run(_tmux(*cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=20, env=env, cwd=cwd or None)
    if r.returncode:
        raise OSError(f"tmux new-session failed: {(r.stderr or r.stdout).strip()}")
    for opt in (("status", "off"), ("remain-on-exit", "off")):
        _run("set-option", "-t", f"={sess}", *opt)
    _run("set-option", "-s", "focus-events", "on")  # ccfleet's own server only; Claude Code asks for it
    pid = (r.stdout or "").strip().splitlines()
    if not pid or not pid[0].strip().isdigit():
        raise OSError(f"tmux session {sess} reported no pane pid")
    return int(pid[0].strip())


def peek(pid, lines: int = 40) -> str:
    """Last `lines` non-empty rows of the pane's visible screen."""
    r = _run("capture-pane", "-p", "-t", _pane_of(pid))
    if r.returncode:
        raise OSError(f"tmux capture-pane failed: {r.stderr.strip()}")
    rows = [_VT_RE.sub(" ", x).replace("\x1b", "").rstrip() for x in (r.stdout or "").splitlines()]
    return "\n".join([x for x in rows if x.strip()][-int(lines):])


def type_text(pid, text: str, enter: bool = True) -> None:
    pane = _pane_of(pid)
    text = (text or "").replace("\r\n", "\n")
    if text:
        if len(text) <= 500 and "\n" not in text:
            _run("send-keys", "-t", pane, "-l", "--", text)  # -l: literal, no key-name lookup
        else:
            # one long send-keys burst can lose its head in Claude Code; a multi-line prompt typed
            # as keys would submit at the first newline. Hand it over as ONE bracketed paste.
            buf = f"ccfleet_in_{uuid.uuid4().hex[:8]}"
            _run("load-buffer", "-b", buf, "-", input=text)
            _run("paste-buffer", "-d", "-p", "-b", buf, "-t", pane)  # -p: bracketed, -d: drop buffer
        if enter:
            time.sleep(0.25 + min(1.5, len(text) / 4000))
    if enter:
        _run("send-keys", "-t", pane, "Enter")


def send_keys(pid, names: list[str]) -> None:
    """Named keys (see KEYS), one send-keys per key — a single burst can read as a paste."""
    toks = [KEYS[n.lower()] for n in names]  # KeyError on an unknown name, like console_io
    pane = _pane_of(pid)
    for tok in toks:
        _run("send-keys", "-t", pane, tok)


def resize(pid, cols: int = COLS, rows: int = ROWS) -> str:
    pane = _pane_of(pid)
    _run("resize-window", "-t", pane, "-x", str(int(cols)), "-y", str(int(rows)))
    r = _run("display-message", "-p", "-t", pane, "#{window_width}x#{window_height}")
    return (r.stdout or "").strip() or f"{cols}x{rows}"


def kill(pid) -> bool:
    """Close the tmux session running `pid` (its pane gets SIGHUP). False when it isn't ours."""
    try:
        pane = _pane_of(pid)
    except (OSError, RuntimeError):
        return False
    _run("kill-session", "-t", pane)
    return True


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["ls"]:
        for p in panes():
            print(p["pid"], p["pane"], p["session"])
    elif len(a) >= 2 and a[0] == "peek":
        print(peek(int(a[1]), int(a[2]) if len(a) > 2 else 40))
    elif len(a) >= 3 and a[0] == "type":
        type_text(int(a[1]), " ".join(a[2:]))
    elif len(a) >= 3 and a[0] == "keys":
        send_keys(int(a[1]), a[2:])
    else:
        sys.exit(__doc__)
