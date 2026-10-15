"""Console/process operations for ccfleet sessions, dispatched by platform.

Windows: each session runs in its own console window (CREATE_NEW_CONSOLE); peek/type/keys go through
a persistent `console_io.py --serve` worker that attaches to that console; boot = scheduled task.
macOS (Linux experimental): each session runs in a detached tmux session on ccfleet's own tmux
server (`tmux -L ccfleet`, see console_tmux.py); boot = a launchd LaunchAgent (macOS) or a systemd
user unit (Linux). ccfleet.py calls ONLY through this module, so the supervisor loop itself stays
platform-neutral.

Public API (identical on every platform):
    pid_alive(pid) -> bool
    kill_tree(pid) -> None
    peek(pid, lines) -> str
    type_text(pid, text) -> None
    send_keys(pid, names) -> None
    resize(pid, cols, rows) -> str
    launch_console(args, cwd, env, window) -> int    # returns the session's pid
    check() -> str                                   # backend ready? RuntimeError with a hint if not
    install_boot(home, python, script) -> str        # what was installed
    uninstall_boot() -> None
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
BACKEND = "windows-console" if IS_WIN else "tmux"
TASK_NAME = "ccfleet-supervisor"            # Windows scheduled task
LAUNCHD_LABEL = "com.ccfleet.supervisor"    # macOS LaunchAgent
SYSTEMD_UNIT = "ccfleet.service"            # Linux systemd --user unit
_CONSOLE_IO = Path(__file__).with_name("console_io.py")


# ---------------------------------------------------------------- boot files (pure, testable anywhere)
def service_path(python: str) -> str:
    """PATH for the macOS/Linux boot service: launchd/systemd start with a bare PATH that has neither
    Homebrew (tmux) nor ~/.local/bin (claude's native installer)."""
    dirs = [str(Path(python).parent), str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
            "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d.startswith("/"):
            dirs.append(d)
    return ":".join(dict.fromkeys(dirs))


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(home, python, script) -> bytes:
    """The LaunchAgent: starts the supervisor at login, restarts it whenever it exits."""
    import plistlib
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(python), str(script), "--home", str(home), "run"],
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"CCFLEET_HOME": str(home), "PATH": service_path(python),
                                 "LANG": os.environ.get("LANG") or "en_US.UTF-8",
                                 "PYTHONIOENCODING": "utf-8"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        # sessions live in ccfleet's tmux server: never let launchd reap them with the supervisor
        "AbandonProcessGroup": True,
        "StandardOutPath": f"{str(home).rstrip('/')}/launchd.out.log",
        "StandardErrorPath": f"{str(home).rstrip('/')}/launchd.err.log",
    })


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def systemd_unit(home, python, script) -> str:
    def q(x):
        return '"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return ("[Unit]\nDescription=ccfleet supervisor (Claude Code sessions)\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={q(python)} {q(script)} --home {q(home)} run\n"
            f"WorkingDirectory={home}\nEnvironment={q('CCFLEET_HOME=' + str(home))}\n"
            f"Environment={q('PATH=' + service_path(python))}\nEnvironment=PYTHONIOENCODING=utf-8\n"
            # sessions live in ccfleet's tmux server: stopping the supervisor must not kill them
            "Restart=always\nRestartSec=30\nKillMode=process\n\n[Install]\nWantedBy=default.target\n")


# ================================================================ Windows: console windows
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

    def kill_tree(pid) -> None:
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

    def check() -> str:
        return "windows console"

    def launch_console(args, cwd, env, window="minimized") -> int:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = {"hidden": 0, "normal": 1}.get(window, 7)  # 7 = SW_SHOWMINNOACTIVE
        p = subprocess.Popen(args, cwd=cwd or None, env=env,
                             creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
        return p.pid

    def install_boot(home, python, script, start=True) -> str:
        """Scheduled task: at boot (S4U, no logon needed) + a 5-minute keep-alive trigger."""
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
        return f"scheduled task '{TASK_NAME}'"

    def uninstall_boot() -> None:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)


# ================================================================ macOS / Linux: tmux
else:
    import console_tmux

    def pid_alive(pid) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, OverflowError):
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        try:  # a zombie still answers kill(0): reap it if it is our child
            done, _ = os.waitpid(int(pid), os.WNOHANG)
            return done == 0
        except ChildProcessError:  # not our child (tmux panes are the tmux server's children)
            return True

    def kill_tree(pid) -> None:
        """Close a session: SIGTERM the process and every descendant (dev servers, builds…), close
        its tmux session if it is one of ours, SIGKILL whatever is still there after 3 s."""
        import fleet_limits
        pid = int(pid)
        tree = [pid] + fleet_limits.descendants(pid)
        for p in tree:
            try:
                os.kill(p, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            console_tmux.kill(pid)
        except Exception:
            pass
        deadline = time.time() + 3
        while time.time() < deadline and any(pid_alive(p) for p in tree):
            time.sleep(0.1)
        for p in tree:
            if pid_alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def peek(pid, lines=40) -> str:
        return console_tmux.peek(pid, lines)

    def type_text(pid, text) -> None:
        console_tmux.type_text(pid, text)

    def send_keys(pid, names) -> None:
        console_tmux.send_keys(pid, list(names))

    def resize(pid, cols=120, rows=50):
        return console_tmux.resize(pid, cols, rows)

    def check() -> str:
        return console_tmux.check()

    def launch_console(args, cwd, env, window="minimized") -> int:
        """`window` has no meaning here: sessions are always detached tmux sessions
        (watch one live with `tmux -L ccfleet attach -t ccfleet_<name>`)."""
        return console_tmux.launch(args, cwd, env)

    def _launchctl(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True)

    def install_boot(home, python, script, start=True) -> str:
        Path(home).mkdir(parents=True, exist_ok=True)
        if IS_MAC:
            plist = launchd_plist_path()
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_bytes(launchd_plist(home, python, script))
            uid = os.getuid()
            for dom in (f"gui/{uid}", f"user/{uid}"):
                _launchctl("bootout", f"{dom}/{LAUNCHD_LABEL}")
            # gui/<uid> needs a logged-in desktop session; over plain SSH fall back to user/<uid>
            errs = []
            for dom in (f"gui/{uid}", f"user/{uid}"):
                r = _launchctl("bootstrap", dom, str(plist))
                if r.returncode == 0:
                    return f"LaunchAgent {plist} (launchctl domain {dom})"
                errs.append(f"{dom}: {(r.stderr or r.stdout).strip()}")
            raise SystemExit(f"wrote {plist} but launchctl bootstrap failed ({'; '.join(errs)}). It loads "
                             "at your next login; on a headless Mac turn on automatic login.")
        unit = systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(home, python, script), encoding="utf-8")
        for cmd in (["daemon-reload"], ["enable", *(["--now"] if start else []), SYSTEMD_UNIT]):
            r = subprocess.run(["systemctl", "--user", *cmd], capture_output=True, text=True)
            if r.returncode:
                raise SystemExit(f"systemctl --user {' '.join(cmd)} failed: {r.stderr.strip()}")
        return f"systemd user unit {unit} (keep it running while logged out: sudo loginctl enable-linger $USER)"

    def uninstall_boot() -> None:
        if IS_MAC:
            uid = os.getuid()
            for dom in (f"gui/{uid}", f"user/{uid}"):
                _launchctl("bootout", f"{dom}/{LAUNCHD_LABEL}")
            launchd_plist_path().unlink(missing_ok=True)
            return
        subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], capture_output=True)
        systemd_unit_path().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
