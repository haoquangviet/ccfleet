"""macOS / Linux backend: console_tmux command lines (subprocess mocked — runs on every OS), the
launchd / systemd boot files, the POSIX RAM watchdog helpers, and — only where tmux is installed — a
real round trip on a throwaway tmux server."""
import os
import plistlib
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

import _setup  # noqa: F401
import console_tmux
import fleet_console
import fleet_limits


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class FakeTmux:
    """Records tmux invocations (argv after `tmux -u -L <socket>`) and answers list-panes."""

    def __init__(self, panes="4242 %7 ccfleet_app\n", screen=""):
        self.calls, self.inputs = [], []
        self.panes, self.screen = panes, screen

    def __call__(self, argv, **kw):
        if argv[1:] == ["-V"]:
            return _cp("tmux 3.4\n")
        assert argv[:4] == ["/x/tmux", "-u", "-L", console_tmux.SOCKET], argv
        args = argv[4:]
        self.calls.append(args)
        if kw.get("input") is not None:
            self.inputs.append(kw["input"])
        if args[0] == "list-panes":
            return _cp(self.panes)
        if args[0] == "capture-pane":
            return _cp(self.screen)
        if args[0] == "new-session":
            return _cp("5151\n")
        if args[0] == "display-message":
            return _cp("120x50\n")
        if args[0] == "-V":
            return _cp("tmux 3.4\n")
        return _cp()


class TmuxCommands(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTmux()
        patches = [mock.patch.object(console_tmux, "_tmux_path", "/x/tmux"),
                   mock.patch.object(console_tmux.subprocess, "run", self.fake),
                   mock.patch.object(console_tmux.time, "sleep")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_always_own_socket(self):
        self.assertEqual(console_tmux._tmux("ls")[1:], ["-u", "-L", "ccfleet", "ls"])
        self.assertNotEqual(console_tmux.SOCKET, "default")

    def test_key_mapping(self):
        console_tmux.send_keys(4242, ["esc", "shift+tab", "ctrl+c", "up", "down", "enter", "tab", "space"])
        sent = [c[-1] for c in self.fake.calls if c[0] == "send-keys"]
        self.assertEqual(sent, ["Escape", "BTab", "C-c", "Up", "Down", "Enter", "Tab", "Space"])
        # one send-keys per key, all aimed at the session's pane id
        self.assertTrue(all(c[:3] == ["send-keys", "-t", "%7"] for c in self.fake.calls if c[0] == "send-keys"))

    def test_same_key_names_as_windows(self):
        import importlib.util
        if sys.platform == "win32" and importlib.util.find_spec("console_io"):
            import console_io
            self.assertEqual(set(console_io.KEYS), set(console_tmux.KEYS))

    def test_unknown_key_rejected_before_sending(self):
        with self.assertRaises(KeyError):
            console_tmux.send_keys(4242, ["enter", "rm -rf"])
        self.assertFalse([c for c in self.fake.calls if c[0] == "send-keys"])

    def test_type_short_text_then_separate_enter(self):
        console_tmux.type_text(4242, "-reply with the word pong")
        sk = [c for c in self.fake.calls if c[0] == "send-keys"]
        self.assertEqual(sk[0], ["send-keys", "-t", "%7", "-l", "--", "-reply with the word pong"])
        self.assertEqual(sk[1], ["send-keys", "-t", "%7", "Enter"])

    def test_type_multiline_is_one_bracketed_paste(self):
        console_tmux.type_text(4242, "line one\r\nline two")
        ops = [c[0] for c in self.fake.calls]
        self.assertIn("load-buffer", ops)
        paste = next(c for c in self.fake.calls if c[0] == "paste-buffer")
        self.assertIn("-p", paste)
        self.assertEqual(paste[-2:], ["-t", "%7"])
        self.assertEqual(self.fake.inputs, ["line one\nline two"])
        self.assertEqual(self.fake.calls[-1], ["send-keys", "-t", "%7", "Enter"])
        self.assertFalse([c for c in self.fake.calls if c[0] == "send-keys" and "-l" in c])

    def test_peek(self):
        self.fake.screen = "\n".join(["", "╭───╮", "│ > hi │", "", "pong", "", ""])
        self.assertEqual(console_tmux.peek(4242, 2), "│ > hi │\npong")
        self.assertIn(["capture-pane", "-p", "-t", "%7"], self.fake.calls)

    def test_unknown_pid_is_oserror(self):
        # OSError → ccfleet falls back to the local web API, same as a failed AttachConsole
        with self.assertRaises(OSError):
            console_tmux.peek(999, 10)
        self.assertFalse(console_tmux.owns(999))
        self.assertTrue(console_tmux.owns(4242))

    def test_resize_and_kill(self):
        self.assertEqual(console_tmux.resize(4242, 120, 50), "120x50")
        self.assertIn(["resize-window", "-t", "%7", "-x", "120", "-y", "50"], self.fake.calls)
        self.assertTrue(console_tmux.kill(4242))
        self.assertIn(["kill-session", "-t", "%7"], self.fake.calls)
        self.assertFalse(console_tmux.kill(999))

    def test_launch(self):
        env = {"PATH": "/usr/bin", "HOME": "/Users/x", "CCFLEET_SESSION": "my.app",
               "CLAUDE_CONFIG_DIR": "/Users/x/.claude-work", "ANTHROPIC_API_KEY": "sk-secret",
               "GH_TOKEN": "ghp_secret"}
        with mock.patch.dict(os.environ, {}, clear=False):
            pid = console_tmux.launch(["/x/claude", "--session-id", "abc"], "/tmp/proj", env)
        self.assertEqual(pid, 5151)
        new = next(c for c in self.fake.calls if c[0] == "new-session")
        self.assertEqual(new[:10], ["new-session", "-d", "-s", "ccfleet_my_app", "-x", "120", "-y", "50", "-P", "-F"])
        self.assertIn("/tmp/proj", new)
        i = new.index("/usr/bin/env")
        self.assertIn("CLAUDE_CONFIG_DIR=/Users/x/.claude-work", new[i:])
        self.assertIn("CCFLEET_SESSION=my.app", new[i:])
        self.assertEqual(new[-3:], ["/x/claude", "--session-id", "abc"])
        self.assertFalse([a for a in new if "secret" in a], "secrets must never go on a command line")
        # a stale session with the same name is cleared first; the status bar is turned off
        self.assertIn(["kill-session", "-t", "=ccfleet_my_app"], self.fake.calls)
        self.assertIn(["set-option", "-t", "=ccfleet_my_app", "status", "off"], self.fake.calls)

    def test_launch_default_account_unsets_config_dir(self):
        console_tmux.launch(["/x/claude"], None, {"PATH": "/usr/bin", "CCFLEET_SESSION": "a"})
        new = next(c for c in self.fake.calls if c[0] == "new-session")
        i = new.index("/usr/bin/env")
        self.assertEqual(new[i + 1:i + 3], ["-u", "CLAUDE_CONFIG_DIR"])

    def test_session_name(self):
        self.assertEqual(console_tmux.session_name("hello world:1.2"), "ccfleet_hello_world_1_2")
        self.assertEqual(console_tmux.session_name("login-work"), "ccfleet_login-work")

    def test_check(self):
        self.assertEqual(console_tmux.check(), "tmux 3.4")
        with mock.patch.object(console_tmux.subprocess, "run", return_value=_cp("tmux 2.6\n")):
            with self.assertRaises(RuntimeError):
                console_tmux.check()


class TmuxMissing(unittest.TestCase):
    def test_helpful_error(self):
        with mock.patch.object(console_tmux, "_tmux_path", None), \
             mock.patch.object(console_tmux.shutil, "which", return_value=None), \
             mock.patch.object(console_tmux.os.path, "exists", return_value=False):
            with self.assertRaises(RuntimeError) as cm:
                console_tmux.check()
            self.assertIn("brew install tmux", str(cm.exception))


class BootFiles(unittest.TestCase):
    def test_launchd_plist(self):
        d = plistlib.loads(fleet_console.launchd_plist("/Users/x/.ccfleet", "/opt/homebrew/bin/python3",
                                                       "/Users/x/ccfleet/ccfleet.py"))
        self.assertEqual(d["Label"], "com.ccfleet.supervisor")
        self.assertTrue(d["Label"].startswith("com.ccfleet."))
        self.assertEqual(d["ProgramArguments"], ["/opt/homebrew/bin/python3", "/Users/x/ccfleet/ccfleet.py",
                                                 "--home", "/Users/x/.ccfleet", "run"])
        self.assertTrue(d["KeepAlive"] and d["RunAtLoad"] and d["AbandonProcessGroup"])
        self.assertEqual(d["EnvironmentVariables"]["CCFLEET_HOME"], "/Users/x/.ccfleet")
        self.assertIn("/opt/homebrew/bin", d["EnvironmentVariables"]["PATH"].split(":"))
        self.assertTrue(d["StandardOutPath"].startswith("/Users/x/.ccfleet"))
        self.assertTrue(d["StandardErrorPath"].startswith("/Users/x/.ccfleet"))
        self.assertTrue(str(fleet_console.launchd_plist_path()).endswith(
            os.path.join("Library", "LaunchAgents", "com.ccfleet.supervisor.plist")))

    def test_systemd_unit(self):
        u = fleet_console.systemd_unit("/home/x/.ccfleet", "/usr/bin/python3", "/home/x/ccfleet/ccfleet.py")
        self.assertIn('ExecStart="/usr/bin/python3" "/home/x/ccfleet/ccfleet.py" --home "/home/x/.ccfleet" run', u)
        self.assertIn("KillMode=process", u)  # sessions survive a supervisor restart
        self.assertIn("Restart=always", u)

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_install_calls_launchctl_bootstrap(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as t, \
             mock.patch.object(fleet_console, "launchd_plist_path", return_value=Path(t) / "x.plist"), \
             mock.patch.object(fleet_console.subprocess, "run", return_value=_cp()) as run:
            fleet_console.install_boot(t, sys.executable, "/x/ccfleet.py")
            boot = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["launchctl", "bootstrap"]]
            self.assertEqual(boot[0][2], f"gui/{os.getuid()}")
            fleet_console.uninstall_boot()
            self.assertFalse((Path(t) / "x.plist").exists())


class PosixLimits(unittest.TestCase):
    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                                    10000.\n"
               "Pages active:                                 277618.\n"
               "Pages inactive:                                20000.\n"
               "Pages speculative:                              3000.\n"
               "Pages purgeable:                                 536.\n")

    def test_vm_stat(self):
        self.assertAlmostEqual(fleet_limits.parse_vm_stat(self.VM_STAT), 33536 * 16384 / 1024 ** 3)

    def test_meminfo(self):
        self.assertAlmostEqual(fleet_limits.parse_meminfo("MemTotal: 1 kB\nMemAvailable:    2097152 kB\n"), 2.0)
        self.assertIsNone(fleet_limits.parse_meminfo("nothing"))

    def test_process_tree_rss(self):
        table = fleet_limits.parse_ps("  100     1  1048576\n  101   100  1048576\n  102   101   524288\n"
                                      "  103     1  9999999\n garbage\n")
        self.assertEqual(sorted(fleet_limits.tree_of(100, table)), [101, 102])
        self.assertAlmostEqual(fleet_limits.tree_rss_gb(100, table), 2.5)

    @unittest.skipIf(sys.platform == "win32", "POSIX watchdog")
    def test_watchdog(self):
        table = fleet_limits.parse_ps("100 1 4194304\n101 100 6291456\n")
        with mock.patch.object(fleet_limits, "proc_table", return_value=table):
            self.assertIsNone(fleet_limits.over_cap(100, 16))
            self.assertAlmostEqual(fleet_limits.over_cap(100, 8), 10.0)
        self.assertIn("HIT THE CAP", fleet_limits.peak(100))

    def test_windows_has_no_watchdog(self):
        if sys.platform == "win32":
            self.assertIsNone(fleet_limits.over_cap(os.getpid(), 0.001))


HAVE_TMUX = sys.platform != "win32" and bool(shutil.which("tmux") or os.path.exists("/opt/homebrew/bin/tmux"))


@unittest.skipUnless(HAVE_TMUX, "needs tmux (macOS / Linux)")
class RealTmux(unittest.TestCase):
    """End to end on a private, throwaway tmux server (never ccfleet's real one, never the default)."""

    def setUp(self):
        self.sock = f"ccfleet-unittest-{os.getpid()}"
        p = mock.patch.object(console_tmux, "SOCKET", self.sock)
        p.start()
        self.addCleanup(p.stop)
        sock_file = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", self.sock)
        self.addCleanup(lambda: os.path.exists(sock_file) and os.unlink(sock_file))  # runs last
        self.addCleanup(lambda: subprocess.run(console_tmux._tmux("kill-server"), capture_output=True))

    def test_round_trip(self):
        env = {**os.environ, "CCFLEET_SESSION": "unittest"}
        pid = fleet_console.launch_console(["/bin/cat"], "/tmp", env)
        self.assertTrue(fleet_console.pid_alive(pid))
        self.assertIn("ccfleet_unittest", [p["session"] for p in console_tmux.panes()])
        fleet_console.type_text(pid, "hello from ccfleet")
        time.sleep(0.5)
        self.assertIn("hello from ccfleet", fleet_console.peek(pid, 10))
        self.assertIn("x", fleet_console.resize(pid, 100, 40))
        fleet_console.send_keys(pid, ["ctrl+c"])  # cat exits → pane closes
        for _ in range(30):
            if not fleet_console.pid_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(fleet_console.pid_alive(pid))

    def test_kill_tree(self):
        pid = fleet_console.launch_console(["/bin/sh", "-c", "sleep 300 & sleep 300; wait"], "/tmp",
                                           {**os.environ, "CCFLEET_SESSION": "unittest-kill"})
        time.sleep(0.5)
        kids = fleet_limits.descendants(pid)
        self.assertTrue(kids)
        fleet_console.kill_tree(pid)
        self.assertFalse(fleet_console.pid_alive(pid))
        self.assertFalse([k for k in kids if fleet_console.pid_alive(k)])
