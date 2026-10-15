"""Core helpers: data dir, JSON persistence, RAM guard, console API, web port picker, i18n."""
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import ccfleet
import fleet_console
import fleet_i18n
import fleet_limits


class FindHome(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"CCFLEET_HOME": r"X:\somewhere"}), mock.patch.object(sys, "argv", ["x"]):
            self.assertEqual(ccfleet._find_home(), Path(r"X:\somewhere"))

    def test_home_flag_beats_env(self):
        with mock.patch.dict(os.environ, {"CCFLEET_HOME": r"X:\env"}), \
             mock.patch.object(sys, "argv", ["ccfleet.py", "--home", r"Y:\flag", "run"]):
            self.assertEqual(ccfleet._find_home(), Path(r"Y:\flag"))

    def test_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CCFLEET_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["x"]):
            self.assertEqual(ccfleet._find_home(), Path.home() / ".ccfleet")


class Json(unittest.TestCase):
    def test_lone_surrogate_does_not_raise(self):
        """Screen text scraped from a console can carry lone surrogates."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            ccfleet.save_json(p, {"screen": "ok \ud83d broken"})
            self.assertIn("broken", ccfleet.load_json(p, {})["screen"])
            self.assertFalse(p.with_suffix(".tmp").exists())

    def test_empty_state_is_set_aside(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "state.json"
            st.write_bytes(b"")
            self.assertEqual(ccfleet.load_json(st, {}), {})
            self.assertFalse(st.exists())
            self.assertEqual(len(list(Path(t).glob("state.json.corrupt-*"))), 1)

    def test_bad_config_still_stops(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "config.json"
            cfg.write_text("{", encoding="utf-8")
            with self.assertRaises(SystemExit):
                ccfleet.load_json(cfg, None)
            self.assertTrue(cfg.exists())


class RamGuard(unittest.TestCase):
    SESSION_GB = 0.18  # measured cost of one idle Claude Code session

    def test_threshold_is_proportionate(self):
        t = ccfleet.DEFAULT_CONFIG["host"]["min_free_ram_gb"]
        self.assertGreaterEqual(t / self.SESSION_GB, 5)
        self.assertLessEqual(t / self.SESSION_GB, 12)
        self.assertGreaterEqual(t, 1.0)

    def test_free_ram_reads_a_plausible_number(self):
        free = fleet_limits.free_ram_gb()
        self.assertGreater(free, 0)
        self.assertLess(free, 4096)


class Console(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(fleet_console.pid_alive(os.getpid()))
        for pid in (None, 0, 2_000_000_000):
            self.assertFalse(fleet_console.pid_alive(pid))

    def test_public_api_present(self):
        for fn in ("peek", "type_text", "send_keys", "resize", "launch_console", "install_boot", "uninstall_boot", "kill_tree"):
            self.assertTrue(callable(getattr(fleet_console, fn, None)), fn)
        self.assertEqual(fleet_console.TASK_NAME, "ccfleet-supervisor")


class WebPort(unittest.TestCase):
    def test_random_port_in_range_and_bindable(self):
        p = ccfleet.pick_port()
        self.assertTrue(ccfleet.PORT_RANGE[0] <= p <= ccfleet.PORT_RANGE[1])
        with socket.socket() as s:
            s.bind(("127.0.0.1", p))

    def test_first_use_saves_the_port(self):
        with tempfile.TemporaryDirectory() as t:
            cfg_file = Path(t) / "config.json"
            with mock.patch.object(ccfleet, "CONFIG", cfg_file), mock.patch.object(ccfleet, "log"):
                cfg = ccfleet.load_config()
                port = ccfleet.web_conf(cfg)["port"]
                self.assertEqual(ccfleet.load_config()["web"]["port"], port)
                self.assertEqual(ccfleet.web_conf()["port"], port)  # stable afterwards
                self.assertEqual(ccfleet.web_conf()["host"], "127.0.0.1")
                self.assertFalse(ccfleet.web_conf()["enabled"])


class I18n(unittest.TestCase):
    def tearDown(self):
        fleet_i18n.set_lang("en")

    def test_detect(self):
        self.assertEqual(fleet_i18n.detect({"lang": "vi"}), "vi")
        with mock.patch.dict(os.environ, {"LANG": "vi_VN.UTF-8", "CCFLEET_LANG": ""}):
            self.assertEqual(fleet_i18n.detect({}), "vi")
        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8", "CCFLEET_LANG": ""}):
            self.assertEqual(fleet_i18n.detect({}), "en")

    def test_same_keys_in_every_language(self):
        self.assertEqual(set(fleet_i18n.STRINGS["en"]), set(fleet_i18n.STRINGS["vi"]))

    def test_translate_and_fallback(self):
        fleet_i18n.set_lang("vi")
        self.assertEqual(fleet_i18n.t("cli.sent"), "đã gửi")
        fleet_i18n.set_lang("xx")  # unknown → English
        self.assertEqual(fleet_i18n.t("cli.removed", name="a"), "removed a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
