"""`ccfleet tell`: queue, delivery when the target is idle, drop / expiry."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import ccfleet
import fleet_msg

CFG = {"sessions": [{"name": "api"}, {"name": "web"}]}


class Tell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch.object(fleet_msg, "INBOX", Path(self.tmp.name) / "inbox").start()
        self.typed = []
        mock.patch.object(ccfleet, "console", side_effect=lambda op, pid, text: self.typed.append((pid, text))).start()
        self.alive = mock.patch.object(ccfleet, "pid_alive", return_value=True).start()
        mock.patch.object(ccfleet, "log").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_one_line_and_limits(self):
        self.assertEqual(fleet_msg.post("api", "a\nb", "web")["text"], "a b")
        with self.assertRaises(ValueError):
            fleet_msg.post("api", "  ", "web")
        with self.assertRaises(ValueError):
            fleet_msg.post("api", "x" * (fleet_msg.MAX_TEXT + 1), "web")

    def test_delivery_waits_for_idle(self):
        fleet_msg.post("API", "hello", "web")  # names are case-insensitive
        state = {"api": {"pid": 11, "busy": True}}
        fleet_msg.deliver(CFG, state)
        self.assertEqual(self.typed, [])
        state["api"]["busy"] = False
        fleet_msg.deliver(CFG, state)
        self.assertEqual(len(self.typed), 1)
        self.assertIn("[ccfleet · message from session web] hello", self.typed[0][1])
        self.assertIn('ccfleet tell web "..."', self.typed[0][1])
        self.assertEqual(fleet_msg.pending(), [])

    def test_unknown_session_dropped(self):
        fleet_msg.post("nobody", "x", "web")
        fleet_msg.deliver(CFG, {})
        self.assertEqual(fleet_msg.pending(), [])

    def test_dead_target_kept_then_expires(self):
        self.alive.return_value = False
        m = fleet_msg.post("api", "x", "web")
        fleet_msg.deliver(CFG, {"api": {"pid": 1}})
        self.assertEqual(len(fleet_msg.pending()), 1)
        with mock.patch.object(fleet_msg.time, "time", return_value=m["at"] + fleet_msg.EXPIRE_SEC + 1):
            fleet_msg.deliver(CFG, {"api": {"pid": 1}})
        self.assertEqual(fleet_msg.pending(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
