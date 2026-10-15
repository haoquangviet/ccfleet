"""Add a Claude account from the web: run `claude auth login` in a hidden console with a fresh
CLAUDE_CONFIG_DIR, scrape the OAuth URL from its screen, type the pasted code back in, then confirm
with `claude auth status` and register the account in config.json.

The code and tokens never touch ccfleet's own files — the CLI stores credentials in the account's
config dir as usual. Login consoles are killed after success/cancel or 15 minutes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import ccfleet
import fleet_console

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
LOGIN_TTL = 900

# own file, not state.json: the supervisor tick rewrites state.json from a copy it loaded seconds
# earlier, which could erase a login started in between
LOGINS = ccfleet.HOME / "logins.json"


def auth_status(cfg: dict, config_dir: str) -> dict:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
    r = subprocess.run([ccfleet.claude_exe(cfg), "auth", "status", "--json"], env=env, capture_output=True,
                       text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def start(name: str) -> dict:
    """Begin a login. A NEW name gets ~/.claude-<name>; an EXISTING one signs in again into its own
    config dir (expired token, wrong account)."""
    cfg, logins = ccfleet.load_config(), ccfleet.load_json(LOGINS, {})
    if not NAME_RE.match(name):
        raise ValueError("account name: lowercase letters, digits and '-', at most 24 characters")
    old = logins.get(name)
    if old and ccfleet.pid_alive(old.get("pid")):
        ccfleet.kill_tree(old["pid"])
    acc = cfg["accounts"].get(name)
    d = Path(acc.get("config_dir") or Path.home() / ".claude") if acc is not None else Path.home() / f".claude-{name}"
    d.mkdir(parents=True, exist_ok=True)
    main_settings = Path.home() / ".claude" / "settings.json"
    if main_settings.exists() and not (d / "settings.json").exists():
        shutil.copy2(main_settings, d / "settings.json")  # same hooks (image shrink…) & permissions
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(d), "CCFLEET_SESSION": f"login-{name}"}
    pid = fleet_console.launch_console([ccfleet.claude_exe(cfg), "auth", "login"], cwd=str(Path.home()),
                                       env=env, window="hidden")
    logins[name] = {"pid": pid, "dir": str(d), "started": time.time()}
    ccfleet.save_json(LOGINS, logins)
    ccfleet.log(f"account login started: {name} -> {d}")
    return {"name": name, "dir": str(d)}


def poll(name: str) -> dict:
    """{state: waiting_url|need_code|done|failed|expired, url?, email?}"""
    cfg, logins = ccfleet.load_config(), ccfleet.load_json(LOGINS, {})
    lg = logins.get(name)
    if name in cfg["accounts"] and not lg:
        return {"state": "done"}
    if not lg:
        return {"state": "failed", "error": "not started"}
    if time.time() - lg["started"] > LOGIN_TTL:
        cancel(name)
        return {"state": "expired"}
    if not ccfleet.pid_alive(lg["pid"]):
        st = auth_status(cfg, lg["dir"])
        if st.get("loggedIn"):
            return finish(name, st)
        return {"state": "failed", "error": "the login process exited"}
    try:
        screen = ccfleet.console("peek", str(lg["pid"]), "60")
    except OSError:
        return {"state": "waiting_url"}
    flat = screen.replace("\r", "")
    m = re.search(r"(https://\S*oauth/authorize\S*)", flat.replace("\n", ""))
    if not m:
        return {"state": "waiting_url"}
    return {"state": "need_code", "url": m.group(1).split("Paste")[0]}


def submit_code(name: str, code: str) -> dict:
    cfg, logins = ccfleet.load_config(), ccfleet.load_json(LOGINS, {})
    lg = logins.get(name)
    code = (code or "").strip()
    if not lg:
        return {"state": "failed", "error": "no such login in progress (cancelled or expired) — start again"}
    if not ccfleet.pid_alive(lg["pid"]):
        ccfleet.log(f"account login {name}: login process {lg['pid']} exited before the code was pasted")
        return {"state": "failed", "error": "the login process exited before the code was pasted — start again"}
    if not code or len(code) > 512 or any(c.isspace() for c in code):
        return {"state": "failed", "error": "invalid code"}
    ccfleet.console("type", str(lg["pid"]), code)
    for _ in range(20):  # wait for the CLI to exchange the code
        time.sleep(1.5)
        st = auth_status(cfg, lg["dir"])
        if st.get("loggedIn"):
            return finish(name, st)
        if not ccfleet.pid_alive(lg["pid"]):
            break
    return {"state": "failed", "error": "not signed in yet — check the code (it works only once) and try again"}


def finish(name: str, st: dict) -> dict:
    cfg, logins = ccfleet.load_config(), ccfleet.load_json(LOGINS, {})
    lg = logins.pop(name, None) or {}
    if lg.get("pid") and ccfleet.pid_alive(lg["pid"]):
        ccfleet.kill_tree(lg["pid"])
    old = cfg["accounts"].get(name)
    # a re-login keeps the account's config_dir as it was (main stays None = default dir)
    cfg["accounts"][name] = {**(old or {}),
                             "config_dir": old.get("config_dir") if old is not None else (lg.get("dir") or str(Path.home() / f".claude-{name}")),
                             "email": st.get("email"), "plan": st.get("subscriptionType")}
    ccfleet.save_json(ccfleet.CONFIG, cfg)
    ccfleet.save_json(LOGINS, logins)
    ccfleet.log(f"account added: {name} ({st.get('subscriptionType')})")
    try:
        ccfleet.install_skill(cfg)  # sessions moved onto this account keep the ccfleet-sessions skill
    except OSError:
        pass
    return {"state": "done", "email": st.get("email"), "plan": st.get("subscriptionType")}


def cancel(name: str) -> None:
    logins = ccfleet.load_json(LOGINS, {})
    lg = logins.pop(name, None)
    if lg and ccfleet.pid_alive(lg.get("pid")):
        ccfleet.kill_tree(lg["pid"])
    ccfleet.save_json(LOGINS, logins)
