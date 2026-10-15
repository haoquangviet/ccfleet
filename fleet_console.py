"""Console/process operations for ccfleet sessions (Windows).

Each session runs in its own console window (CREATE_NEW_CONSOLE); peek/type/keys go through a
persistent `console_io.py --serve` worker that attaches to that console. ccfleet.py calls ONLY
through this module, so the supervisor loop itself stays platform-neutral.

Public API:
    pid_alive(pid) -> bool
    kill_tree(pid) -> None
    peek(pid, lines) -> str
    type_text(pid, text) -> None
    send_keys(pid, names) -> None
    resize(pid, cols, rows) -> str
    launch_console(args, cwd, env, window) -> int    # returns the session's pid
    install_boot(home, python, script) -> None
    uninstall_boot() -> None
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

IS_WIN = sys.platform == "win32"
TASK_NAME = "ccfleet-supervisor"
_CONSOLE_IO = Path(__file__).with_name("console_io.py")


def _need_windows():
    raise NotImplementedError("the ccfleet console backend is Windows-only")


if IS_WIN:
    import ctypes

    def pid_alive(pid) -> bool:
        if not pid:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            # ACCESS_DENIED means the process EXISTS but runs elevated / as another user (e.g. the
            # supervisor's scheduled task). Only "invalid parameter" means there is no such process.
            return ctypes.get_last_error() == 5
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259  # STILL_ACTIVE
else:
    def pid_alive(pid) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, ValueError, OverflowError):
            return False
        except PermissionError:
            return True


def kill_tree(pid) -> None:
    if not IS_WIN:
        _need_windows()
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


class _Worker:
    """A persistent console_io.py --serve process. AttachConsole is per-process and global, so all
    peeks/types go through ONE worker, serialized by a lock. Keeping it alive avoids ~200 ms of
    Python start-up on every single peek."""

    def __init__(self):
        self.p = None
        self.lock = threading.Lock()

    def _spawn(self):
        py = Path(sys.executable).with_name("python.exe")  # pythonw has no stdio → use python.exe
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.p = subprocess.Popen(
            [str(py if py.exists() else sys.executable), str(_CONSOLE_IO), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW

    def request(self, req, timeout=20):
        if not IS_WIN:
            _need_windows()
        with self.lock:
            for attempt in (1, 2):  # respawn once if the worker died
                if self.p is None or self.p.poll() is not None:
                    self._spawn()
                try:
                    self.p.stdin.write(json.dumps(req) + "\n")
                    self.p.stdin.flush()
                    line = self.p.stdout.readline()
                    if not line:  # worker closed its stdout → dead
                        self.p = None
                        continue
                    res = json.loads(line)
                    if not res.get("ok"):
                        raise OSError(f"console {req['op']} failed: {res.get('err')}")
                    return res.get("out", "")
                except (OSError, ValueError) as e:
                    self.p = None
                    if attempt == 2:
                        raise OSError(f"console worker error: {e}")
        raise OSError("console worker unavailable")


_worker = _Worker()


def peek(pid, lines=40) -> str:
    return _worker.request({"op": "peek", "pid": int(pid), "lines": int(lines)})


def type_text(pid, text) -> None:
    _worker.request({"op": "type", "pid": int(pid), "text": text})


def send_keys(pid, names) -> None:
    _worker.request({"op": "keys", "pid": int(pid), "keys": list(names)})


def resize(pid, cols=120, rows=50):
    """Grow the session's console so tall menus (/model…) render in full."""
    return _worker.request({"op": "resize", "pid": int(pid), "cols": int(cols), "rows": int(rows)})


def launch_console(args, cwd, env, window="minimized") -> int:
    if not IS_WIN:
        _need_windows()
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = {"hidden": 0, "normal": 1}.get(window, 7)  # 7 = SW_SHOWMINNOACTIVE
    p = subprocess.Popen(args, cwd=cwd or None, env=env,
                         creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
    return p.pid


def install_boot(home, python, script, start=True) -> None:
    """Scheduled task: at boot (S4U, no logon needed) + a 5-minute keep-alive trigger."""
    if not IS_WIN:
        _need_windows()
    pyw = Path(python).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else python)
    user = os.environ.get("USERNAME", "Administrator")
    ps = f"""
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{script}" --home "{home}" run' -WorkingDirectory '{home}'
$t1 = New-ScheduledTaskTrigger -AtStartup; $t1.Delay = 'PT2M'
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType S4U -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t1,$t2 -Principal $p -Settings $s -Force | Out-Null
{"Start-ScheduledTask -TaskName '" + TASK_NAME + "'" if start else ""}
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stderr)


def uninstall_boot() -> None:
    if not IS_WIN:
        _need_windows()
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
