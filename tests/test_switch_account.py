"""Web "switch account": the session moves to another account (transcript copied, process closed)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import ccfleet
import fleet_web


class SwitchAccount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = {"accounts": {"main": {"config_dir": str(t / "a")}, "work": {"config_dir": str(t / "b")}},
                    "sessions": [{"name": "x", "account": "main", "session_id": "sid1"}]}
        proj = t / "a" / "projects" / "C--p"
        proj.mkdir(parents=True)
        (proj / "sid1.jsonl").write_text("{}\n")
        (t / "b").mkdir()
        (t / "b" / ".credentials.json").write_text('{"claudeAiOauth": {"refreshToken": "x"}}')
        self.state = {"x": {"pid": 4242}}
        for k, v in dict(load_config=lambda: self.cfg, save_json=lambda p, d: None, log=lambda m: None,
                         pid_alive=lambda pid: pid == 4242).items():
            mock.patch.object(ccfleet, k, v).start()
        mock.patch.object(ccfleet, "load_json", return_value=self.state).start()
        self.killed = []
        mock.patch.object(ccfleet, "kill_tree", side_effect=self.killed.append).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_switch_moves_transcript_and_kills(self):
        fleet_web.session_action("x", "takeover", "work")
        self.assertEqual(self.state["x"]["account"], "work")
        self.assertEqual(self.killed, [4242])
        self.assertTrue((Path(self.tmp.name) / "b" / "projects" / "C--p" / "sid1.jsonl").exists())
        self.assertTrue(self.state["x"]["takeover_kill"])

    def test_never_prompted_session_just_switches(self):
        (Path(self.tmp.name) / "a" / "projects" / "C--p" / "sid1.jsonl").unlink()
        fleet_web.session_action("x", "takeover", "work")
        self.assertEqual(self.state["x"]["account"], "work")

    def test_not_signed_in_refused(self):
        (Path(self.tmp.name) / "b" / ".credentials.json").unlink()
        with self.assertRaises(ValueError):
            fleet_web.session_action("x", "takeover", "work")

    def test_unknown_account(self):
        with self.assertRaises(ValueError):
            fleet_web.session_action("x", "takeover", "nope")

    def test_same_account_is_plain_takeover(self):
        fleet_web.session_action("x", "takeover", "main")
        self.assertNotIn("account", self.state["x"])
        self.assertEqual(self.killed, [])


if __name__ == "__main__":
    unittest.main()
