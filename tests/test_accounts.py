"""Account helpers: bypass prompt pre-accept, first-run settings carried to a new account, onboarding."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import fleet_accounts


class AckBypass(unittest.TestCase):
    def test_default_account_without_config_dir(self):
        """`main` has config_dir None (= ~/.claude) — Path(None) must not break the launch."""
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            with mock.patch.object(Path, "home", return_value=home):
                self.assertTrue(fleet_accounts.ack_bypass({"config_dir": None}))
                self.assertFalse(fleet_accounts.ack_bypass({"config_dir": None}))  # already set
            self.assertTrue(json.loads((home / ".claude" / "settings.json").read_text())["skipDangerousModePermissionPrompt"])


class Seed(unittest.TestCase):
    def test_copies_missing_prefs_not_identity(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "a.json"), Path(d, "b", ".claude.json")
            src.write_text(json.dumps({
                "theme": "dark", "remoteDialogSeen": True, "mcpServers": {"x": {}},
                "oauthAccount": {"emailAddress": "main@example.com"}, "userID": "u1", "cachedStatsigGates": {},
                "projects": {"D:/w": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"], "exampleFiles": ["a"]},
                             "D:/no": {"hasTrustDialogAccepted": False}}}), encoding="utf-8")
            dst.parent.mkdir()
            dst.write_text(json.dumps({"theme": "light", "oauthAccount": {"emailAddress": "work@example.com"}}), encoding="utf-8")
            added = fleet_accounts.seed_config(src, dst)
            b = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(b["theme"], "light")                       # never overwritten
            self.assertEqual(b["oauthAccount"]["emailAddress"], "work@example.com")
            self.assertTrue(b["remoteDialogSeen"])
            self.assertIn("mcpServers", b)
            self.assertNotIn("userID", b)
            self.assertNotIn("cachedStatsigGates", b)
            self.assertEqual(b["projects"]["D:/w"], {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]})
            self.assertNotIn("D:/no", b["projects"])
            self.assertTrue(added)
            self.assertEqual(fleet_accounts.seed_config(src, dst), [])  # idempotent


class Onboarded(unittest.TestCase):
    def test_signed_in_account_gets_flag_once(self):
        with tempfile.TemporaryDirectory() as t:
            acc = {"config_dir": t}
            self.assertFalse(fleet_accounts.ensure_onboarded(acc))  # not signed in → untouched
            (Path(t) / ".credentials.json").write_text('{"claudeAiOauth": {"refreshToken": "x"}}')
            (Path(t) / ".claude.json").write_text('{"oauthAccount": {"a": 1}}')
            self.assertTrue(fleet_accounts.ensure_onboarded(acc))
            d = json.loads((Path(t) / ".claude.json").read_text())
            self.assertTrue(d["hasCompletedOnboarding"])
            self.assertEqual(d["oauthAccount"], {"a": 1})
            self.assertFalse(fleet_accounts.ensure_onboarded(acc))

    def test_first_signed_in(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t, "b")).mkdir()
            (Path(t, "b", ".credentials.json")).write_text('{"claudeAiOauth": {"accessToken": "x"}}')
            accs = {"a": {"config_dir": str(Path(t, "a"))}, "b": {"config_dir": str(Path(t, "b"))}}
            self.assertEqual(fleet_accounts.first_signed_in(accs), "b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
