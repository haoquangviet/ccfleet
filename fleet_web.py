"""ccfleet web — remote dashboard for your Claude Code sessions (phone / desktop).

Binds 127.0.0.1 by default; put a TLS reverse proxy or tunnel in front for access from outside.
Auth: password (PBKDF2-SHA256) + TOTP (RFC 6238), HttpOnly/SameSite=Strict cookie (Secure when the
proxy says X-Forwarded-Proto: https), every POST needs header X-Fleet: 1 (CSRF), login lockout per
IP, audit log. X-Real-IP / X-Forwarded-For are trusted only from a loopback peer (the proxy).
First-time setup: `ccfleet web setup` prints a one-time link (30 min) where the owner sets the
password and enrols the authenticator app; `ccfleet web open` prints a 2-minute one-time login link.
Sessions run with bypassed permissions, so this page is effectively shell access.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ccfleet
import fleet_accounts
import fleet_files
import fleet_login
import fleet_skills
import fleet_transcript

AUTH = ccfleet.HOME / "web-auth.json"
AUDIT = ccfleet.HOME / "web-audit.log"
CLI_KEY = ccfleet.HOME / "cli.key"  # local shell → web API (peek/send fallback); created by the CLI
UI = Path(__file__).with_name("fleet_web_ui.html")
COOKIE = "fleet"
SESSION_TTL = 12 * 3600
LOCK_FAILS, LOCK_WINDOW = 5, 900
WEB_SESSIONS = ccfleet.HOME / "web-sessions.json"  # sha256(cookie) -> expiry; survives restarts
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
DEMO = False  # set by fleet_demo: no login, loopback only, fake data
_sessions: dict[str, float] = {k: v for k, v in ccfleet.load_json(WEB_SESSIONS, {}).items() if v > time.time()}
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ crypto helpers
def _pbkdf2(pw: str, salt: bytes, it: int = 310_000) -> str:
    return f"{salt.hex()}${it}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, it).hex()}"


def _pw_ok(pw: str, stored: str) -> bool:
    salt, it, h = stored.split("$")
    return hmac.compare_digest(_pbkdf2(pw, bytes.fromhex(salt), int(it)).split("$")[2], h)


def _totp(secret_b32: str, t: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", int((t or time.time()) // step))
    d = hmac.new(key, msg, hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return f"{(struct.unpack('>I', d[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _totp_ok(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    return any(hmac.compare_digest(_totp(secret, time.time() + k * 30), code) for k in (-1, 0, 1))


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def load_auth() -> dict:
    return ccfleet.load_json(AUTH, {})


def save_auth(a: dict) -> None:
    ccfleet.save_json(AUTH, a)


def new_setup_link(base_url: str) -> str:
    tok = secrets.token_urlsafe(24)
    a = load_auth()
    a.update(setup_hash=_h(tok), setup_exp=time.time() + 1800)
    save_auth(a)
    return f"{base_url.rstrip('/')}/#setup={tok}"


def new_login_link(base_url: str) -> str:
    tok = secrets.token_urlsafe(32)
    a = load_auth()
    a.update(login_hash=_h(tok), login_exp=time.time() + 120)
    save_auth(a)
    return f"{base_url.rstrip('/')}/#login={tok}"


def audit(ip: str, action: str) -> None:
    ccfleet.HOME.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {action}\n")


# ------------------------------------------------------------------ data for the UI
def state_payload() -> dict:
    cfg, state = ccfleet.load_config(), ccfleet.load_json(ccfleet.STATE, {})
    sessions = []
    for s in cfg["sessions"]:
        st = state.get(s["name"], {})
        alive = ccfleet.pid_alive(st.get("pid"))
        sessions.append({
            "name": s["name"], "cwd": s.get("cwd"), "account": ccfleet.eff_account(s, st),
            "enabled": s.get("enabled", True), "stopped": bool(st.get("stopped")),
            "status": "running" if alive else ("elsewhere" if st.get("external_pid") else "down"),
            "busy": bool(alive and st.get("busy")),
            "limit": bool(st.get("limit_hit")), "remote_url": st.get("remote_url"),
            "session_id": st.get("session_id") or s.get("session_id"),
            "up_min": int((time.time() - st.get("started_at", time.time())) / 60) if alive else None,
        })
    sup = int(ccfleet.LOCK.read_text()) if ccfleet.LOCK.exists() else None
    return {"supervisor": ccfleet.pid_alive(sup), "sessions": sessions,
            "accounts": {n: fleet_accounts.usage(a) for n, a in cfg["accounts"].items()},
            "account_info": {n: {"email": a.get("email"), "plan": a.get("plan"), "signed_in": fleet_accounts.signed_in(a)}
                             for n, a in cfg["accounts"].items()},
            "schedules": [{"session": j["session"], "minutes": j.get("minutes"), "every_min": j.get("every_min")}
                          for j in cfg.get("schedules", [])],
            "projects_root": str(fleet_skills.PROJECTS_ROOT)}


_UI_NOISE = ("bypass permissions", "shift+tab", "for agents", "? for shortcuts", "esc to interrupt", "/rc")

# Busy detection lives in ccfleet (one source of truth, shared with the schedule dispatcher).
is_busy = ccfleet.is_busy

# Fragments that only appear when `peek` catches the TUI mid-redraw: leftover VT escape bytes and
# torn throbber rows. A line carrying any of these is noise, never real reply text.
_RESIDUE_RE = re.compile(r"\[\?\d|\[\d+;|\[\d+[A-Za-z]|\?25[hl]|\b\d+;\d+[Hf]\b")
_SPINNER_ROW_RE = re.compile(r"^\s*[✻✽✢✶✳✦✧✛·*]\s+\S{1,3}(?:\s+\S{1,3})*\s*$")
# The live status line Claude draws while working: "Whirring…  9m 7s · ↓ 50.7k tokens)". The token
# count is often torn off a frame, so it is optional; the verb is >= 4 letters so a torn fragment
# ("ng…") never surfaces as the status.
_STATUS_RE = re.compile(
    r"([A-Za-zÀ-Ỹ][A-Za-zà-ỹ]{3,}…)\D{0,40}?(\d+m\s*\d+s|\d+s)(?:.{0,20}?([\d.]+k)\s*tokens)?", re.UNICODE)


def _is_noise_line(t: str) -> bool:
    return (not t or set(t) <= set("─━═│╭╮╰╯ >") or any(k in t for k in _UI_NOISE)
            or bool(_RESIDUE_RE.search(t)) or bool(_SPINNER_ROW_RE.match(t)))


def live_status(screen: str) -> str | None:
    """One clean status line while the session is working ("Whirring… 9m 7s · 50.7k tokens"), or
    None. The live screen is a redraw mess, but the spinner's own status line is stable."""
    if not screen:
        return None
    for line in reversed(screen.splitlines()):  # newest status wins
        m = _STATUS_RE.search(line)
        if m:
            s = f"{m.group(1)} {m.group(2)}"
            return f"{s} · {m.group(3)} tokens" if m.group(3) else s
    return None


def live_text(screen: str, n: int = 14) -> str:
    """The part of the TUI still being written, without separators / input box / status line."""
    keep = [line.rstrip() for line in screen.splitlines() if not _is_noise_line(line.strip())]
    return "\n".join(keep[-n:])


_OPT_RE = re.compile(r"^\s*(?:[❯›>▶]\s*)?(\d{1,2})[.)]\s+(.+?)\s*$")
_MENU_HINTS = ("enter to select", "enter to confirm", "esc to cancel", "to navigate", "type something",
               "do you want to", "would you like")


def parse_choices(screen: str) -> dict | None:
    """Detect an interactive menu at the bottom of the TUI (AskUserQuestion, permission prompt,
    /model, /resume pickers…). Returns {options:[{n,label,free}], current, question, scroll} or None."""
    lines = [x for x in screen.splitlines() if x.strip()]
    tail = lines[-22:]
    low = "\n".join(tail).lower()
    if not any(h in low for h in _MENU_HINTS):
        return None
    opts, current, first_idx = [], None, None
    for i, line in enumerate(tail):
        m = _OPT_RE.match(line)
        if not m:
            continue
        n, label = int(m.group(1)), m.group(2).strip(" │")
        if opts and n != opts[-1]["n"] + 1:  # a new numbered block starts → keep the last one
            opts, current = [], None
        if not opts:
            first_idx = i
        if re.match(r"^\s*[❯›>▶]", line):
            current = n
        opts.append({"n": n, "label": label[:160],
                     "free": bool(re.search(r"type something|other|something else", label, re.I))})
    if len(opts) < 2:
        return None
    question = " ".join(x.strip(" │") for x in tail[max(0, first_idx - 3):first_idx]).strip()[:300]
    # Claude Code paints long pickers in a small scrolling viewport (~3 rows with ↑/↓ markers) no
    # matter how tall the console is, so the UI offers arrow buttons to scroll the menu itself.
    block = "\n".join(tail[first_idx:first_idx + len(opts) + 1])
    return {"options": opts, "current": current or opts[0]["n"], "question": question,
            "scroll": "↓" in block or "↑" in block}


def menu_if_idle(screen: str) -> dict | None:
    """A menu is only meaningful on a settled (non-busy) screen."""
    return None if is_busy(screen) else parse_choices(screen)


def choose(pid: int, menu: dict, n: int, text: str | None = None) -> None:
    """Move the highlight from the current option to option n with arrows, then Enter — or, for a
    free-text option, type the answer and submit it."""
    delta = n - int(menu.get("current") or 1)
    keys = (["down"] * delta if delta > 0 else ["up"] * (-delta))[:20]
    if keys:
        ccfleet.console("keys", str(pid), *keys)
        time.sleep(0.3)
    if text:
        ccfleet.console("type", str(pid), text)
    else:
        ccfleet.console("keys", str(pid), "enter")


def watch(cfg: dict, s: dict, st: dict, q: dict) -> dict:
    """Long-poll (≤20 s): returns as soon as the transcript changes, or every ~1 s while the
    session is busy (with its live status), so the chat feels streamed."""
    acc = cfg["accounts"][ccfleet.eff_account(s, st)]
    sid = st.get("session_id") or s.get("session_id")
    f = fleet_transcript.find(acc, sid) if sid else None
    since = q.get("v", [""])[0]
    alive = ccfleet.pid_alive(st.get("pid"))
    start = time.time()
    deadline = start + 20
    while True:
        ver = f"{f.stat().st_mtime_ns}:{f.stat().st_size}" if f and f.exists() else "0"
        screen, busy = "", False
        if alive:
            try:
                screen = ccfleet.console("peek", str(st["pid"]), "45")
            except OSError:
                screen = ""
            busy = is_busy(screen)
        # require two agreeing reads before surfacing a menu, so one caught mid-redraw never shows
        menu = menu_if_idle(screen)
        if menu and alive:
            try:
                screen2 = ccfleet.console("peek", str(st["pid"]), "45")
            except OSError:
                screen2 = ""
            if is_busy(screen2) or parse_choices(screen2) != menu:
                menu, screen = None, screen2 or screen
                busy = busy or is_busy(screen2)
        changed = ver != since
        tick_due = busy and time.time() - start >= 0.8  # one "working" tick per poll, not a tight loop
        if changed or menu or q.get("screen") or time.time() > deadline or tick_due:
            return {"v": ver, "changed": changed, "busy": busy,
                    "messages": fleet_transcript.tail(acc, sid, int(q.get("n", ["50"])[0])) if (sid and changed) else None,
                    "live": (live_status(screen) or "") if busy else "",
                    "screen": screen if q.get("screen") else None, "menu": menu}
        time.sleep(0.25)


def known_root(project: str) -> str:
    """Only known project dirs (sessions, Claude history, the projects root) are browsable."""
    roots = {os.path.normcase(str(p)): str(p) for p in fleet_skills.project_dirs(ccfleet.load_config())}
    r = roots.get(os.path.normcase(project or ""))
    if not r:
        raise ValueError("unknown project")
    return r


def open_session(cwd: str, sid: str, account: str = "main") -> dict:
    """Resume a past session of a known project (or start a new one when sid is empty)."""
    cfg = ccfleet.load_config()
    known = {os.path.normcase(p["cwd"]): p["cwd"] for p in ccfleet.discover_projects(cfg).values()}
    known.update({os.path.normcase(s["cwd"]): s["cwd"] for s in cfg["sessions"] if s.get("cwd")})
    if os.path.normcase(cwd) not in known:
        raise ValueError("unknown project")
    if account not in cfg["accounts"]:
        raise ValueError(f"no account '{account}'")
    existing = next((s for s in cfg["sessions"] if sid and s.get("session_id") == sid), None)
    if existing:
        name = existing["name"]
    else:
        base = "".join(c if c.isalnum() or c in "-_" else "-" for c in Path(cwd).name)[:24]
        name = f"{base}-{(sid or secrets.token_hex(3))[:6]}"
        s = {"name": name, "account": account, "cwd": known[os.path.normcase(cwd)], "enabled": True}
        if sid:
            s["session_id"] = sid
        cfg["sessions"].append(s)
        ccfleet.save_json(ccfleet.CONFIG, cfg)
    state = ccfleet.load_json(ccfleet.STATE, {})
    st = state.setdefault(name, {})
    st.update(stopped=False, restarts=[])
    st.pop("paused_until", None)
    ccfleet.save_json(ccfleet.STATE, state)
    return {"name": name}


def session_action(name: str, act: str, account: str | None = None) -> None:
    """start / stop / restart / takeover a configured session.
    account: run it on that Claude account from now on — its transcript is copied there and the
    running copy (under ccfleet or elsewhere) is closed so it comes back with --resume on the new one."""
    if act not in ("start", "stop", "restart", "takeover"):
        raise ValueError("action")
    state = ccfleet.load_json(ccfleet.STATE, {})
    stt = state.setdefault(name, {})
    if account:
        cfg = ccfleet.load_config()
        s = next((x for x in cfg["sessions"] if x["name"] == name), None)
        if s is None or account not in cfg["accounts"]:
            raise ValueError("unknown session or account")
        cur = ccfleet.eff_account(s, stt)
        if account != cur:
            sid = stt.get("session_id") or s.get("session_id")
            # the transcript may sit under another account (opened in VS Code there): try them all
            srcs = [cur] + [a for a in cfg["accounts"] if a not in (cur, account)]
            if not fleet_accounts.signed_in(cfg["accounts"][account]):
                raise ValueError(f"account '{account}' is not signed in on this machine")
            # never prompted → no transcript anywhere: nothing to carry over, it simply starts there
            if (sid and any(fleet_accounts.has_transcript(cfg["accounts"][a], sid) for a in srcs)
                    and not any(fleet_accounts.move_transcript(cfg["accounts"][a], cfg["accounts"][account], sid) for a in srcs)):
                raise ValueError(f"could not copy conversation {sid[:8]} to {account}")
            if ccfleet.pid_alive(stt.get("pid")):
                ccfleet.kill_tree(stt["pid"])
            stt["account"] = account
            stt.pop("limit_hit", None)
            ccfleet.log(f"{name}: switched account {cur} -> {account} (web)")
    if act in ("stop", "restart") and ccfleet.pid_alive(stt.get("pid")):
        ccfleet.kill_tree(stt["pid"])
    stt["stopped"] = act == "stop"
    if act == "takeover":
        stt["takeover"] = True
        stt["takeover_kill"] = True  # web button: close the copy open elsewhere
    if act in ("start", "restart", "takeover"):
        stt["restarts"] = []
        stt["force_start"] = True
        stt.pop("paused_until", None)
    ccfleet.save_json(ccfleet.STATE, state)


def rename_session(old: str, new: str) -> dict:
    """Rename a session (config entry + state key); a running session keeps its pid."""
    new = (new or "").strip()
    if not new or not all(c.isalnum() or c in "-_." for c in new):
        raise ValueError("invalid name (letters, digits, - _ . only)")
    cfg = ccfleet.load_config()
    if not any(s["name"] == old for s in cfg["sessions"]):
        raise ValueError("no such session")
    if new != old and any(s["name"] == new for s in cfg["sessions"]):
        raise ValueError("name already taken")
    for s in cfg["sessions"]:
        if s["name"] == old:
            s["name"] = new
    ccfleet.save_json(ccfleet.CONFIG, cfg)
    state = ccfleet.load_json(ccfleet.STATE, {})
    if old in state:
        state[new] = state.pop(old)
        ccfleet.save_json(ccfleet.STATE, state)
    ccfleet.log(f"session renamed {old} -> {new}")
    return {"name": new}


def session_files(name: str) -> tuple[list[dict], str]:
    """Files of a session's project (right pane): what Claude wrote/used or the user sent, across
    every conversation of the project, minus anything secret-looking. Returns (items, root)."""
    cfg, s, st = session_cfg(name)
    sid = st.get("session_id") or s.get("session_id")
    try:
        root = known_root(s.get("cwd") or "")
    except ValueError:
        return [], s.get("cwd") or ""
    items = fleet_transcript.project_files(cfg["accounts"], root, sid, paste_dir=str(ccfleet.HOME / "paste"))
    return [i for i in items if not fleet_files.SECRET_RE.search(i["name"])], root


def session_cfg(name: str) -> tuple[dict, dict, dict]:
    cfg = ccfleet.load_config()
    s = next((x for x in cfg["sessions"] if x["name"] == name), None)
    if not s:
        raise KeyError(name)
    return cfg, s, ccfleet.load_json(ccfleet.STATE, {}).get(name, {})


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ccfleet"

    def log_message(self, *a):  # quiet; the audit log covers actions
        pass

    @property
    def _from_proxy(self) -> bool:
        return self.client_address[0] in LOOPBACK

    @property
    def ip(self) -> str:
        """Client IP. Forwarded headers are trusted only from a loopback peer (the local proxy /
        tunnel); anyone else could forge them to dodge the login lockout."""
        if self._from_proxy:
            fwd = self.headers.get("X-Real-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    @property
    def _https(self) -> bool:
        return self._from_proxy and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _send(self, code: int, body: bytes, ctype="application/json", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), headers=headers)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _cookie(self, name: str) -> str | None:
        c = self.headers.get("Cookie") or ""
        return next((p.split("=", 1)[1] for p in c.split("; ") if p.startswith(name + "=")), None)

    def _authed(self) -> bool:
        if DEMO and self.server.server_address[0] in LOOPBACK:
            return True
        # the CLI on this machine (peek/send fallback): a random key in CCFLEET_HOME that only
        # local admins can read. NOT a loopback check — a reverse proxy makes every request loopback.
        key = self.headers.get("X-Fleet-CLI")
        if key and CLI_KEY.exists():
            return hmac.compare_digest(key, CLI_KEY.read_text(encoding="utf-8").strip())
        tok = self._cookie(COOKIE)
        if not tok:
            return False
        with _lock:
            exp = _sessions.get(_h(tok))
            return bool(exp and exp > time.time())

    def _locked(self) -> bool:
        now = time.time()
        fl = [x for x in _fails.get(self.ip, []) if now - x < LOCK_WINDOW]
        _fails[self.ip] = fl
        return len(fl) >= LOCK_FAILS

    def _cookie_attrs(self, max_age: int) -> str:
        return f"Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self._https else "")

    # ---------------- routes
    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path in ("/", "/index.html"):
            return self._send(200, UI.read_bytes(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'"})
        if path.path == "/api/me":
            a = load_auth()
            return self._json(200, {"authed": self._authed(), "configured": bool(a.get("pw") and a.get("totp")),
                                    "demo": DEMO, "lang": ccfleet.load_config().get("lang") or None})
        if not self._authed():
            return self._json(401, {"error": "login"})
        q = urllib.parse.parse_qs(path.query)
        g = lambda k, d="": q.get(k, [d])[0]  # noqa: E731
        try:
            if path.path == "/api/state":
                return self._json(200, state_payload())
            if path.path == "/api/files":
                try:
                    return self._json(200, fleet_files.listing(known_root(g("project")), g("path")))
                except (ValueError, PermissionError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e) or "not found"})
            if path.path in ("/api/files/get", "/api/paste/get"):
                try:
                    if path.path == "/api/paste/get":  # an image pasted from the web (lives outside projects)
                        nm = g("name")
                        f = ccfleet.HOME / "paste" / nm
                        if not re.fullmatch(r"paste-[\w.-]+\.png", nm) or not f.is_file():
                            raise FileNotFoundError(nm)
                        size = f.stat().st_size
                    else:
                        f, size = fleet_files.read(known_root(g("project")), g("path"))
                except (ValueError, PermissionError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e) or "not found"})
                return self._file(f, size, bool(g("dl")))
            if path.path == "/api/projects":
                ps = sorted(ccfleet.discover_projects(ccfleet.load_config()).values(), key=lambda p: p["last"], reverse=True)
                return self._json(200, [{**p, "accounts": sorted(p["accounts"])} for p in ps[:60]])
            if path.path == "/api/accounts/login":
                return self._json(200, fleet_login.poll(g("name")))
            if path.path == "/api/project-sessions":
                cfg = ccfleet.load_config()
                cwd = g("cwd")
                if os.path.normcase(cwd) not in {os.path.normcase(p["cwd"]) for p in ccfleet.discover_projects(cfg).values()}:
                    return self._json(404, {"error": "project not in the Claude Code history"})
                managed = {s.get("session_id"): s["name"] for s in cfg["sessions"]}
                state = ccfleet.load_json(ccfleet.STATE, {})
                managed.update({st.get("session_id"): n for n, st in state.items() if isinstance(st, dict) and st.get("session_id")})
                items = fleet_transcript.project_sessions(cfg["accounts"], cwd)
                for it in items:
                    it["managed_as"] = managed.get(it["session_id"])
                return self._json(200, items)
            if path.path == "/api/skills":
                cfg = ccfleet.load_config()
                projects = [str(p) for p in fleet_skills.project_dirs(cfg)]
                sel = g("project")
                view = fleet_skills.project_view(cfg, Path(sel)) if sel in projects else None
                return self._json(200, {"library": fleet_skills.library(), "projects": projects, "project": view})
            parts = path.path.strip("/").split("/")  # api/session/<name>/<what>
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "session":
                cfg, s, st = session_cfg(urllib.parse.unquote(parts[2]))
                if parts[3] == "messages":
                    acc = cfg["accounts"][ccfleet.eff_account(s, st)]
                    sid = st.get("session_id") or s.get("session_id")
                    return self._json(200, fleet_transcript.tail(acc, sid, int(g("n", "40"))) if sid else [])
                if parts[3] == "watch":
                    return self._json(200, watch(cfg, s, st, q))
                if parts[3] == "files":
                    items, root = session_files(s["name"])
                    return self._json(200, {"cwd": root, "items": items})
                if parts[3] == "screen":
                    if not ccfleet.pid_alive(st.get("pid")):
                        return self._json(200, {"text": "(session is not running under ccfleet)"})
                    return self._json(200, {"text": ccfleet.console("peek", str(st["pid"]), "45")})
        except KeyError:
            return self._json(404, {"error": "not found"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("X-Fleet") != "1":
            return self._json(403, {"error": "csrf"})
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/api/paste":  # image pasted into the composer: raw body
            if not self._authed():
                return self._json(401, {"error": "login"})
            import fleet_clip
            n = int(self.headers.get("Content-Length") or 0)
            if n > fleet_clip.MAX_IMAGE:
                return self._json(413, {"error": "image larger than 10 MB"})
            try:
                p = fleet_clip.save_bytes(self.rfile.read(n))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"paste image {p}")
            return self._json(200, {"path": p})
        if path == "/api/files/upload":  # raw body (no multipart) — before _body(), which caps at 100 KB
            if not self._authed():
                return self._json(401, {"error": "login"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > fleet_files.MAX_UPLOAD:
                return self._json(413, {"error": "file too large (max 100 MB)"})
            try:
                root = known_root(q.get("project", [""])[0])
                p = fleet_files.save_upload(root, q.get("dir", [""])[0], q.get("name", [""])[0], self.rfile.read(n))
            except (ValueError, PermissionError) as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"upload {p}")
            return self._json(200, {"path": str(p)})
        body = self._body()
        if path == "/api/setup":
            return self._setup(body)
        if path == "/api/login":
            return self._login(body)
        if path == "/api/login-link":
            return self._login_link(body)
        if path == "/api/logout":
            with _lock:
                _sessions.pop(_h(self._cookie(COOKIE) or ""), None)
                ccfleet.save_json(WEB_SESSIONS, _sessions)
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; {self._cookie_attrs(0)}"})
        if not self._authed():
            return self._json(401, {"error": "login"})
        if path in ("/api/accounts/login", "/api/accounts/code", "/api/accounts/cancel"):
            name = (body.get("name") or "").strip().lower()
            try:
                if path.endswith("/login"):
                    r = fleet_login.start(name)
                elif path.endswith("/code"):
                    r = fleet_login.submit_code(name, body.get("code") or "")  # the code is never logged
                else:
                    fleet_login.cancel(name)
                    r = {"ok": True}
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"account {path.rsplit('/', 1)[1]} {name} -> {r.get('state', 'ok')}")
            return self._json(200, r)
        if path in ("/api/files/mkdir", "/api/files/delete"):
            try:
                root = known_root(body.get("project") or "")
                if path.endswith("mkdir"):
                    fleet_files.mkdir(root, body.get("path") or "")
                else:
                    fleet_files.delete(root, body.get("path") or "")
            except (ValueError, PermissionError, OSError) as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"{path.rsplit('/', 1)[1]} {body.get('project')} {body.get('path')}")
            return self._json(200, {"ok": True})
        if path == "/api/rewrite":  # ✨ in the composer: tidy a rough prompt with Haiku
            import fleet_rewrite
            try:
                return self._json(200, {"text": fleet_rewrite.rewrite(body.get("text") or "")})
            except (ValueError, RuntimeError, OSError) as e:
                return self._json(400, {"error": str(e)[:300]})
        if path == "/api/session-open":
            sid = (body.get("session_id") or "").strip()
            try:
                r = open_session(body.get("cwd") or "", sid, body.get("account") or "main")
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"open {r['name']} {sid or 'new'}")
            return self._json(200, {"ok": True, **r})
        if path == "/api/session-rename":
            try:
                r = rename_session(body.get("old") or "", body.get("new") or "")
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"rename {body.get('old')} -> {r['name']}")
            return self._json(200, {"ok": True, **r})
        if path == "/api/skills/import":
            n = len(fleet_skills.import_all(ccfleet.load_config()))
            audit(self.ip, f"skills import ({n})")
            return self._json(200, {"ok": True, "count": n})
        if path == "/api/skills/assign":
            cfg = ccfleet.load_config()
            proj = body.get("project") or ""
            if proj not in [str(p) for p in fleet_skills.project_dirs(cfg)]:
                return self._json(400, {"error": "unknown project"})
            view = fleet_skills.assign(cfg, Path(proj), list(body.get("skills") or []))
            audit(self.ip, f"skills assign {proj} ({len(view['assigned'])})")
            return self._json(200, view)
        if path == "/api/projects":
            name, folder = (body.get("name") or "").strip(), (body.get("path") or "").strip()
            if not folder or any(c in name for c in '\\/:*?"<>|'):
                return self._json(400, {"error": "a folder is required and the name may not contain special characters"})
            try:
                s = ccfleet.create_project(ccfleet.load_config(), name, folder, body.get("account") or "main",
                                           (body.get("clone_url") or "").strip() or None, True, True,
                                           (body.get("prompt") or "").strip() or None)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            audit(self.ip, f"project new {s['name']} {folder}")
            return self._json(200, {"ok": True, "name": s["name"]})
        parts = path.strip("/").split("/")
        try:
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "session":
                name = urllib.parse.unquote(parts[2])
                cfg, s, st = session_cfg(name)
                if parts[3] == "choose":
                    if not ccfleet.pid_alive(st.get("pid")):
                        return self._json(400, {"error": "session is not running under ccfleet"})
                    menu = parse_choices(ccfleet.console("peek", str(st["pid"]), "45"))  # fresh: highlight may have moved
                    n = int(body.get("n") or 0)
                    if not menu or not any(o["n"] == n for o in menu["options"]):
                        return self._json(409, {"error": "the menu changed — check the screen"})
                    choose(st["pid"], menu, n, (body.get("text") or "").strip() or None)
                    audit(self.ip, f"choose {name} #{n}{' +text' if body.get('text') else ''}")
                    return self._json(200, {"ok": True})
                if parts[3] == "keys":
                    keys = [k for k in (body.get("keys") or []) if isinstance(k, str)][:10]
                    allowed = {"enter", "esc", "tab", "shift+tab", "up", "down", "left", "right", "backspace",
                               "ctrl+c", "ctrl+o", "ctrl+r", "space", "y", "n", "1", "2", "3"}
                    if not keys or any(k.lower() not in allowed for k in keys) or not ccfleet.pid_alive(st.get("pid")):
                        return self._json(400, {"error": "invalid keys, or the session is not running under ccfleet"})
                    ccfleet.console("keys", str(st["pid"]), *keys)
                    audit(self.ip, f"keys {name} {'+'.join(keys)}")
                    return self._json(200, {"ok": True})
                if parts[3] == "send":
                    text = (body.get("text") or "").strip()
                    if not text or not ccfleet.pid_alive(st.get("pid")):
                        return self._json(400, {"error": "empty, or the session is not running under ccfleet"})
                    ccfleet.console("type", str(st["pid"]), text)
                    audit(self.ip, f"send {name} ({len(text)} chars)")
                    return self._json(200, {"ok": True})
                if parts[3] == "action":
                    act = body.get("action")
                    try:
                        session_action(name, act, (body.get("account") or "").strip() or None)
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    audit(self.ip, f"{act} {name}" + (f" account={body.get('account')}" if body.get("account") else ""))
                    return self._json(200, {"ok": True})
        except KeyError:
            return self._json(404, {"error": "not found"})
        return self._json(404, {"error": "not found"})

    def _file(self, f: Path, size: int, dl: bool):
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or f.suffix.lower() in (".md", ".py", ".ts", ".tsx", ".js", ".json", ".log",
                                                              ".yml", ".yaml", ".sql", ".ps1", ".sh", ".svg", ".html"):
            ctype = "text/plain; charset=utf-8"  # never render project HTML/JS/SVG as a page
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"{'attachment' if dl else 'inline'}; filename*=UTF-8''{urllib.parse.quote(f.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with f.open("rb") as fh:
            while chunk := fh.read(256 * 1024):
                self.wfile.write(chunk)

    def _setup(self, body: dict):
        a = load_auth()
        tok = body.get("token") or ""
        if not a.get("setup_hash") or a.get("setup_exp", 0) < time.time() or not hmac.compare_digest(_h(tok), a["setup_hash"]):
            audit(self.ip, "setup rejected")
            return self._json(403, {"error": "link expired or invalid"})
        if body.get("step") == "begin":
            pw = body.get("password") or ""
            if len(pw) < 12:
                return self._json(400, {"error": "password must be at least 12 characters"})
            secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            a.update(pending_pw=_pbkdf2(pw, secrets.token_bytes(16)), pending_totp=secret)
            save_auth(a)
            return self._json(200, {"secret": secret, "uri": f"otpauth://totp/ccfleet:owner?secret={secret}&issuer=ccfleet"})
        if body.get("step") == "confirm":
            if not a.get("pending_totp") or not _totp_ok(a["pending_totp"], body.get("otp")):
                return self._json(400, {"error": "wrong code"})
            save_auth({"pw": a["pending_pw"], "totp": a["pending_totp"]})  # setup link consumed
            audit(self.ip, "setup completed")
            return self._json(200, {"ok": True})
        return self._json(400, {"error": "step"})

    def _login(self, body: dict):
        if self._locked():
            return self._json(429, {"error": "locked for 15 minutes after too many failed attempts"})
        a = load_auth()
        ok = bool(a.get("pw") and a.get("totp") and _pw_ok(body.get("password") or "", a["pw"])
                  and _totp_ok(a["totp"], body.get("otp")))
        if not ok:
            _fails.setdefault(self.ip, []).append(time.time())
            audit(self.ip, "login FAILED")
            return self._json(401, {"error": "wrong password or code"})
        audit(self.ip, "login ok")
        return self._issue_cookie()

    def _issue_cookie(self):
        tok = secrets.token_urlsafe(32)
        with _lock:
            _sessions[_h(tok)] = time.time() + SESSION_TTL
            ccfleet.save_json(WEB_SESSIONS, {k: v for k, v in _sessions.items() if v > time.time()})
        return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}={tok}; {self._cookie_attrs(SESSION_TTL)}"})

    def _login_link(self, body: dict):
        """`ccfleet web open`: a one-time, 2-minute link printed to whoever runs ccfleet on this
        machine (they can already read CCFLEET_HOME — same trust as `ccfleet web setup`)."""
        if self._locked():
            return self._json(429, {"error": "locked for 15 minutes after too many failed attempts"})
        a = load_auth()
        ok = bool(a.get("login_hash") and a.get("login_exp", 0) > time.time()
                  and secrets.compare_digest(a["login_hash"], _h(str(body.get("token") or ""))))
        a.pop("login_hash", None)  # single use, also when wrong
        a.pop("login_exp", None)
        save_auth(a)
        if not ok:
            _fails.setdefault(self.ip, []).append(time.time())
            audit(self.ip, "login link FAILED")
            return self._json(401, {"error": "link expired or already used — run again: ccfleet web open"})
        audit(self.ip, "login via ccfleet web open")
        return self._issue_cookie()


def serve(port: int, host: str = "127.0.0.1") -> None:
    if DEMO and host not in LOOPBACK:
        raise SystemExit("demo mode only binds to 127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    ccfleet.log(f"web listening on {host}:{port}")
    srv.serve_forever()


def start_thread(port: int, host: str = "127.0.0.1") -> None:
    threading.Thread(target=serve, args=(port, host), daemon=True, name="ccfleet-web").start()
