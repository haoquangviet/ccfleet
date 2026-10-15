"""Web server over real HTTP on a loopback port: auth gate, CSRF header, forwarded-header trust,
Secure cookie behind an https proxy, TOTP, file guards."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
import ccfleet
import fleet_files
import fleet_web


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet_web.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def tearDown(self):
        fleet_web.DEMO = False
        fleet_web._fails.clear()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_index_and_me(self):
        r, data = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<html", data.lower())
        r, data = self.req("GET", "/api/me")
        me = json.loads(data)
        self.assertFalse(me["authed"])
        self.assertFalse(me["demo"])

    def test_api_requires_login(self):
        for p in ("/api/state", "/api/projects", "/api/files?project=x", "/api/session/x/screen"):
            self.assertEqual(self.req("GET", p)[0].status, 401, p)

    def test_post_requires_csrf_header(self):
        self.assertEqual(self.req("POST", "/api/session/x/send", {"text": "hi"})[0].status, 403)
        self.assertEqual(self.req("POST", "/api/session/x/send", {"text": "hi"}, {"X-Fleet": "1"})[0].status, 401)

    def test_demo_flag_skips_login_on_loopback(self):
        fleet_web.DEMO = True
        self.assertEqual(self.req("GET", "/api/state")[0].status, 200)

    def test_demo_refuses_public_bind(self):
        fleet_web.DEMO = True
        with self.assertRaises(SystemExit):
            fleet_web.serve(0, "0.0.0.0")

    def test_login_with_password_and_totp(self):
        secret = "JBSWY3DPEHPK3PXP"
        auth = {"pw": fleet_web._pbkdf2("correct horse battery", b"0123456789abcdef", 1000), "totp": secret}
        with mock.patch.object(fleet_web, "load_auth", return_value=auth):
            r, _ = self.req("POST", "/api/login", {"password": "wrong", "otp": fleet_web._totp(secret)}, {"X-Fleet": "1"})
            self.assertEqual(r.status, 401)
            r, _ = self.req("POST", "/api/login", {"password": "correct horse battery", "otp": fleet_web._totp(secret)},
                            {"X-Fleet": "1"})
            self.assertEqual(r.status, 200)
            cookie = r.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertNotIn("Secure", cookie)  # plain http on loopback
            tok = cookie.split(";")[0]
            self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": tok})[0].status, 200)
            # behind an https proxy the cookie is Secure
            r, _ = self.req("POST", "/api/login", {"password": "correct horse battery", "otp": fleet_web._totp(secret)},
                            {"X-Fleet": "1", "X-Forwarded-Proto": "https"})
            self.assertIn("Secure", r.getheader("Set-Cookie"))

    def test_lockout_keyed_on_forwarded_ip_from_loopback_proxy(self):
        with mock.patch.object(fleet_web, "load_auth", return_value={}):
            for _ in range(fleet_web.LOCK_FAILS):
                self.req("POST", "/api/login", {"password": "x", "otp": "1"}, {"X-Fleet": "1", "X-Forwarded-For": "203.0.113.7"})
            r, _ = self.req("POST", "/api/login", {"password": "x", "otp": "1"}, {"X-Fleet": "1", "X-Forwarded-For": "203.0.113.7"})
            self.assertEqual(r.status, 429)
            r, _ = self.req("POST", "/api/login", {"password": "x", "otp": "1"}, {"X-Fleet": "1", "X-Forwarded-For": "198.51.100.1"})
            self.assertEqual(r.status, 401)  # another client is not locked out

    def test_forwarded_headers_ignored_from_non_loopback_peer(self):
        h = fleet_web.Handler.__new__(fleet_web.Handler)
        h.client_address = ("192.0.2.10", 5555)
        h.headers = {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "1.2.3.4", "X-Forwarded-Proto": "https"}
        self.assertEqual(h.ip, "192.0.2.10")
        self.assertFalse(h._https)
        h.client_address = ("127.0.0.1", 5555)
        self.assertEqual(h.ip, "1.2.3.4")
        self.assertTrue(h._https)

    def test_totp_window(self):
        s = "JBSWY3DPEHPK3PXP"
        self.assertTrue(fleet_web._totp_ok(s, fleet_web._totp(s, time.time() - 30)))
        wrong = f"{(int(fleet_web._totp(s)) + 1) % 1_000_000:06d}"
        self.assertFalse(fleet_web._totp_ok(s, wrong) and wrong not in {fleet_web._totp(s, time.time() + k * 30) for k in (-1, 0, 1)})


class FileGuards(unittest.TestCase):
    def test_traversal_and_secrets(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, ".env").write_text("X=1")
            Path(t, "a.txt").write_text("a")
            with self.assertRaises(PermissionError):
                fleet_files.safe_path(t, "../outside.txt")
            with self.assertRaises(PermissionError):
                fleet_files.read(t, ".env")
            self.assertEqual(fleet_files.read(t, "a.txt")[1], 1)
            p1 = fleet_files.save_upload(t, "", "a.txt", b"new")
            self.assertEqual(p1.parent.name, fleet_files.INBOX)
            p2 = fleet_files.save_upload(t, "", "a.txt", b"newer")
            self.assertNotEqual(p1, p2)  # never overwrite
            self.assertEqual(p1.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
