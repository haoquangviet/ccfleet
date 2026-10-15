"""ccfleet — a session manager for Claude Code (Windows, stdlib + Pillow).

Runs several Claude Code sessions, each in its own (minimized/hidden) console with Remote Control
enabled, restarts them when they die or after a reboot, supports several Claude accounts (one
CLAUDE_CONFIG_DIR per account) and serves a web UI for phone/desktop.

Data dir (config/state/logs): $CCFLEET_HOME (or --home), default ~/.ccfleet
  config.json  — accounts + sessions (no secrets; credentials live in each account's config dir)
  state.json   — runtime: pid / resolved session id / restart history per session
  fleet.log    — supervisor log

Usage:  python ccfleet.py <command> [...]   (python ccfleet.py -h)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import fleet_accounts
import fleet_console
import fleet_limits
from fleet_i18n import t


def _find_home() -> Path:
    """Data dir: `--home <dir>` on the command line (the boot task passes it), else $CCFLEET_HOME,
    else ~/.ccfleet."""
    argv = sys.argv
    if "--home" in argv[:-1]:
        return Path(argv[argv.index("--home") + 1])
    if os.environ.get("CCFLEET_HOME"):
        return Path(os.environ["CCFLEET_HOME"])
    return Path.home() / ".ccfleet"


HOME = _find_home()
CONFIG = HOME / "config.json"
STATE = HOME / "state.json"
LOG = HOME / "fleet.log"
LOCK = HOME / "supervisor.pid"
TASK_NAME = fleet_console.TASK_NAME
PORT_RANGE = (20000, 60000)   # web port: picked at random on first use, then saved

POLL_SEC = 10
BACKOFF_WINDOW_SEC = 600      # >= BACKOFF_MAX_RESTARTS restarts inside this window → pause
BACKOFF_MAX_RESTARTS = 5
BACKOFF_PAUSE_SEC = 600
ACTIVE_WINDOW_SEC = 600       # transcript touched this recently by another process → session is in use
CONSOLE_COLS, CONSOLE_ROWS = 120, 50   # tall enough that a 9-option /model picker renders in full

DEFAULT_CONFIG = {
    "host": {
        # Below this: no new launches and no scheduled prompts. A Claude Code session costs roughly
        # 200 MB, so 1.5 GB still leaves room for several more.
        "min_free_ram_gb": 1.5,
        "max_busy_sessions": 3,         # scheduled prompts wait while this many sessions are working
    },
    "claude_exe": "",  # empty → auto-detect (npm global claude.exe, ~/.local/bin, then PATH)
    "lang": "",        # "en" | "vi" for the CLI menu; empty → LANG env var, else English
    "accounts": {
        # "main" = default ~/.claude (config_dir null). Add more with: ccfleet account add <name>
        "main": {"config_dir": None},
    },
    "defaults": {
        "permission_mode": "bypassPermissions",
        "remote_control": True,
        "window": "minimized",          # minimized | hidden | normal
        "restart": "always",            # always | on-failure | never
        "effort": None,                 # low|medium|high|xhigh|max — lower = fewer tokens
        "model": None,
        # Token savers: per-machine sections move out of the system prompt so sessions share
        # prompt cache; a short working style is appended to every session.
        "exclude_dynamic_sections": True,
        "append_system_prompt": "Save context: read files in ranges (offset/limit), always tail logs, "
                                "hand multi-file reading to subagents, answer briefly.",
        "boot_prompt": None,            # sent once when (re)started after a reboot / supervisor start
        "extra_args": [],
        # Resource caps per session (Job Object: claude + every child build/deploy process).
        "mem_gb": 8,                    # job memory cap; allocations beyond it fail instead of freezing the host
        "cpu_pct": 50,                  # hard cap, % of the whole machine
    },
    "sessions": [
        # {"name": "my-app", "account": "main", "cwd": "C:\\Projects\\my-app",
        #  "session_id": "<uuid or empty → new session, id remembered>", "enabled": true}
    ],
}


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError):  # pythonw: no stdout
        pass
    with LOG.open("a", encoding="utf-8", errors="replace") as f:
        f.write(line + "\n")
    # keep the log small (context-friendly when someone tails it)
    if LOG.stat().st_size > 2_000_000:
        LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-2000:]) + "\n", encoding="utf-8")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        # e.g. a 0-byte state.json after a power cut. Everything but config.json is rebuildable:
        # set the bad file aside and go on.
        if path.name == "config.json":
            sys.exit(f"{path}: invalid JSON ({e})")
        bad = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            path.replace(bad)
        except OSError:
            pass
        print(f"{path}: invalid JSON ({e}) - moved to {bad.name}, starting fresh", file=sys.stderr)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # errors="replace": screen text scraped from a console can carry lone surrogates; a strict
    # utf-8 write would raise mid-save and leave a 0-byte file behind
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8", errors="replace")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())  # data on disk before the rename, or a crash can leave a 0-byte file
    tmp.replace(path)


def load_config() -> dict:
    cfg = load_json(CONFIG, None)
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_json(CONFIG, cfg)
    for k, v in DEFAULT_CONFIG["defaults"].items():
        cfg.setdefault("defaults", {}).setdefault(k, v)
    cfg.setdefault("accounts", {"main": {"config_dir": None}})
    cfg.setdefault("sessions", [])
    return cfg


def claude_exe(cfg: dict) -> str:
    if cfg.get("claude_exe"):
        return cfg["claude_exe"]
    npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    for c in (npm, Path.home() / ".local/bin/claude.exe"):
        if c.exists():
            return str(c)
    found = shutil.which("claude.exe") or shutil.which("claude")
    if not found:
        sys.exit("claude executable not found — set claude_exe in config.json")
    return found


pid_alive = fleet_console.pid_alive
kill_tree = fleet_console.kill_tree


def account_env(cfg: dict, account: str) -> dict:
    acc = cfg["accounts"].get(account)
    if acc is None:
        raise KeyError(f"unknown account '{account}' (ccfleet account list)")
    env = os.environ.copy()
    if acc.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = acc["config_dir"]
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def running_elsewhere(cfg: dict, account: str, session_id: str, owned: bool = False) -> int | None:
    """PID if this session id is already open (e.g. in VS Code) — never run it twice."""
    # 1) Claude Code's own live-session registry: <config_dir>/sessions/<pid>.json {pid, sessionId}.
    reg = fleet_accounts.config_dir_of(cfg["accounts"][account]) / "sessions"
    for f in reg.glob("*.json") if reg.is_dir() else []:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if e.get("sessionId") == session_id and pid_alive(e.get("pid")):
            return e["pid"]
    if owned:
        # our own (re)start: the registry above is enough; `claude agents --json` can block for
        # tens of seconds
        return None
    try:
        out = subprocess.run([claude_exe(cfg), "agents", "--json"], env=account_env(cfg, account),
                             capture_output=True, text=True, timeout=30).stdout
        for a in json.loads(out or "[]"):
            if a.get("sessionId") == session_id and pid_alive(a.get("pid")):
                return a["pid"]
    except Exception as e:  # listing is best-effort
        log(f"warn: claude agents --json failed for {account}: {e}")
    # Second guard: a transcript written in the last ACTIVE_WINDOW_SEC means someone is still
    # driving it → treat as open elsewhere (-1).
    root = fleet_accounts.config_dir_of(cfg["accounts"][account]) / "projects"
    for f in (root.glob(f"*/{session_id}.jsonl") if root.is_dir() else []):
        if time.time() - f.stat().st_mtime < ACTIVE_WINDOW_SEC:
            return -1
    return None


def session_opt(cfg: dict, s: dict, key: str):
    return s[key] if key in s else cfg["defaults"].get(key)


def build_args(cfg: dict, s: dict, sid: str, resume: bool, prompt: str | None) -> list[str]:
    args = [claude_exe(cfg)]
    if session_opt(cfg, s, "remote_control"):
        args += ["--remote-control", s["name"]]
    args += ["--resume", sid] if resume else ["--session-id", sid]
    if pm := session_opt(cfg, s, "permission_mode"):
        args += ["--permission-mode", pm]
    if m := session_opt(cfg, s, "model"):
        args += ["--model", m]
    if e := session_opt(cfg, s, "effort"):
        args += ["--effort", e]
    if session_opt(cfg, s, "exclude_dynamic_sections"):
        args.append("--exclude-dynamic-system-prompt-sections")
    if asp := session_opt(cfg, s, "append_system_prompt"):
        args += ["--append-system-prompt", asp]
    args += list(session_opt(cfg, s, "extra_args") or [])
    if prompt:
        args.append(prompt)
    return args


def eff_account(s: dict, st: dict) -> str:
    """Account the session currently runs on (a manual switch overrides the configured one)."""
    return st.get("account") or s.get("account", "main")


def launch(cfg: dict, s: dict, st: dict, prompt: str | None) -> int:
    sid = st.get("session_id") or s.get("session_id") or ""
    acc_name = eff_account(s, st)
    # resume only a conversation that exists on this account: a session started but never prompted
    # has no transcript, and `--resume <id>` then dies with "No conversation found"
    resume = bool(sid) and fleet_accounts.has_transcript(cfg["accounts"][acc_name], sid)
    if not sid:
        sid = str(uuid.uuid4())
    env = account_env(cfg, acc_name)
    env["CCFLEET_SESSION"] = s["name"]
    src = cfg["accounts"].get("main")
    dst = cfg["accounts"][acc_name]
    if src is not None and dst is not src:  # other account: skip Claude Code's first-run dialogs
        try:
            if added := fleet_accounts.seed_config(fleet_accounts.global_json(src), fleet_accounts.global_json(dst)):
                log(f"{s['name']}: carried {len(added)} settings from main to {acc_name}")
        except OSError as e:
            log(f"{s['name']}: settings seed failed: {e}")
    try:
        if not fleet_accounts.signed_in(dst):
            log(f"{s['name']}: account {acc_name} is NOT signed in on this machine — Claude will ask to log in")
        elif fleet_accounts.ensure_onboarded(dst):
            log(f"{s['name']}: marked {acc_name} onboarded (skip Claude Code's first-run login screen)")
        # the owner configured this session for bypassPermissions → don't park it on Claude Code's
        # one-time "Yes, I accept" screen on a fresh machine
        if session_opt(cfg, s, "permission_mode") == "bypassPermissions" and fleet_accounts.ack_bypass(dst):
            log(f"{s['name']}: bypass-permissions prompt acknowledged for {acc_name} (config: permission_mode)")
    except OSError as e:
        log(f"{s['name']}: onboarding flag failed: {e}")
    pid = fleet_console.launch_console(build_args(cfg, s, sid, resume, prompt), cwd=s.get("cwd") or None,
                                       env=env, window=session_opt(cfg, s, "window") or "minimized")
    st.update(pid=pid, session_id=sid, started_at=time.time())
    try:
        st["caps"] = fleet_limits.apply(pid, s["name"], session_opt(cfg, s, "mem_gb"), session_opt(cfg, s, "cpu_pct"))
    except OSError as e:
        log(f"{s['name']}: resource caps not applied: {e}")
    st.setdefault("restarts", []).append(time.time())
    st["restarts"] = [x for x in st["restarts"] if time.time() - x < BACKOFF_WINDOW_SEC]
    log(f"started {s['name']} pid={pid} account={acc_name} {'resume' if resume else 'new'} session={sid}")
    return pid


TRUST_MARKERS = ("Yes, I trust this folder", "Do you trust the files")
LIMIT_MARKERS = ("usage limit", "limit reached", "hit your limit", "limit will reset", "resets at")

# Whether a session is actively working. Detected from the live token counter / hook line /
# throbber, NOT just "esc to interrupt": while Claude animates its spinner the TUI redraws many
# times a second and `peek` often catches a frame where the status line has been overwritten and
# only a spinner row survived. Deliberately NOT firing on a bare "…" — replies often end in one.
_BUSY_MARKERS = ("esc to interrupt", "· ↓", "tokens)", "running ")
_SPINNER_RE = re.compile(r"[✻✽✢✶✳✦✧·*]\s+[A-Za-zÀ-Ỹ][A-Za-zÀ-ỹ]+…")


def is_busy(screen: str | None) -> bool:
    if not screen:
        return False
    low = screen.lower()
    return any(m in low for m in _BUSY_MARKERS) or bool(_SPINNER_RE.search(screen))


def console(*args: str, timeout: int = 20) -> str:
    """console("peek", pid, "45") / ("type", pid, text) / ("keys", pid, *names)."""
    op, pid = args[0], int(args[1])
    if op == "peek":
        return fleet_console.peek(pid, int(args[2]) if len(args) > 2 else 40)
    if op == "type":
        fleet_console.type_text(pid, args[2] if len(args) > 2 else "")
        return ""
    if op == "keys":
        fleet_console.send_keys(pid, list(args[2:]))
        return ""
    raise ValueError(f"unknown console op {op!r}")


def _accept_trust(pid: int, screen: str) -> None:
    """Pick 'Yes, I trust this folder' on the workspace-trust prompt. The cursor may default to
    'No, exit' (a blind Enter there quits Claude): find the cursor row and the 'Yes' row, step the
    highlight onto 'Yes', then Enter. Falls back to one 'down' when the layout can't be parsed."""
    lines = screen.splitlines()
    cur = next((i for i, x in enumerate(lines) if x.lstrip().startswith(("❯", "›", ">", "▶"))), None)
    yes = next((i for i, x in enumerate(lines) if "I trust this folder" in x), None)
    if cur is not None and yes is not None and cur != yes:
        console("keys", str(pid), *(["down" if yes > cur else "up"] * abs(yes - cur)))
        time.sleep(0.2)
    elif cur is None or yes is None:
        console("keys", str(pid), "down")
        time.sleep(0.2)
    console("keys", str(pid), "enter")


def watch_screen(s: dict, st: dict) -> None:
    """Look at a running session's screen: accept the trust prompt, remember the Remote Control
    URL, track busy, flag usage-limit banners (shown in status / the web)."""
    try:
        screen = console("peek", str(st["pid"]), "40")
    except Exception as e:
        log(f"{s['name']}: peek failed: {e}")
        return
    if st.get("sized_pid") != st["pid"]:  # once per console: make it tall enough for long menus
        try:
            st["console_size"] = fleet_console.resize(st["pid"], CONSOLE_COLS, CONSOLE_ROWS)
        except Exception as e:
            log(f"{s['name']}: console resize failed: {e}")
        st["sized_pid"] = st["pid"]
    tail = "\n".join(screen.splitlines()[-8:])  # old dialogs linger in scrollback — act on the bottom only
    if (st.get("trust_pid") != st["pid"] and "Enter to confirm" in tail
            and any(m in screen for m in TRUST_MARKERS) and "claude.ai/code/session_" not in tail):
        _accept_trust(st["pid"], screen)
        st["trust_pid"] = st["pid"]
        log(f"{s['name']}: accepted workspace trust prompt")
    if (i := screen.find("https://claude.ai/code/session_")) >= 0:
        st["remote_url"] = screen[i:].split()[0]
    st["last_screen_tail"] = tail
    st["busy"] = is_busy(screen)
    # bottom only: a conversation that merely talks about limits must not raise the flag
    st["limit_hit"] = any(m in "\n".join(screen.splitlines()[-12:]).lower() for m in LIMIT_MARKERS)


def run_schedules(cfg: dict, state: dict) -> None:
    """Durable cron for sessions: config "schedules": [{"session", "minutes": [17,47] | "every_min": 30,
    "prompt", "only_idle": true}]. Survives session restarts/reboots (unlike in-session CronCreate).
    A due prompt is held until the session is idle."""
    now = datetime.now()
    sched_state = state.setdefault("_schedules", {})
    for i, job in enumerate(cfg.get("schedules", [])):
        st = state.get(job["session"], {})
        if not pid_alive(st.get("pid")):
            continue
        key = f"{i}:{job['session']}"
        if key not in sched_state:  # first sight: start counting from now (no burst on boot)
            sched_state[key] = time.time()
            continue
        last = sched_state[key]
        if job.get("minutes"):
            slot = now.replace(second=0, microsecond=0)
            due_slots = [slot.replace(minute=m) for m in job["minutes"] if m <= now.minute]
            due = bool(due_slots) and max(due_slots).timestamp() > last
        else:
            due = time.time() - last >= job.get("every_min", 30) * 60
        if not due or (job.get("only_idle", True) and st.get("busy")):
            continue
        console("type", str(st["pid"]), job["prompt"])
        sched_state[key] = time.time()
        log(f"{job['session']}: schedule #{i} sent")


def claude_version(cfg: dict) -> str:
    try:
        return subprocess.run([claude_exe(cfg), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return "?"


def auto_update(cfg: dict, state: dict) -> None:
    """Nightly `claude update` (config "auto_update": {"enabled", "window": [2, 5]}), only when no
    session is working. A new version → each session is restarted (--resume, same conversation) at
    its next idle moment so it runs the new binary."""
    au = {"enabled": True, "window": [2, 5], **(cfg.get("auto_update") or {})}
    upd = state.setdefault("_update", {})
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if au["enabled"] and au["window"][0] <= now.hour < au["window"][1] and upd.get("last_day") != today:
        if any(state.get(s["name"], {}).get("busy") for s in cfg["sessions"]):
            return  # someone is working — try again next tick
        before = claude_version(cfg)
        r = subprocess.run([claude_exe(cfg), "update"], capture_output=True, text=True, timeout=900)
        after = claude_version(cfg)
        upd.update(last_day=today, version=after)
        log(f"claude update: {before} -> {after} (exit {r.returncode}) {(r.stdout or r.stderr).strip()[-200:]}")
        if after != before:
            for s in cfg["sessions"]:
                if pid_alive(state.get(s["name"], {}).get("pid")):
                    state[s["name"]]["needs_restart"] = True
    for s in cfg["sessions"]:  # roll idle sessions onto the new binary, one per tick
        st = state.get(s["name"], {})
        if st.get("needs_restart") and pid_alive(st.get("pid")) and not st.get("busy"):
            kill_tree(st["pid"])
            st["needs_restart"] = False
            st["restarts"] = []
            log(f"{s['name']}: restarted onto new Claude Code version")
            break


def find_session(cfg: dict, name: str) -> dict:
    for s in cfg["sessions"]:
        if s["name"].lower() == name.lower():
            return s
    sys.exit(f"no session '{name}' (ccfleet list)")


# ---------------------------------------------------------------- supervisor
def acquire_lock() -> None:
    if LOCK.exists():
        try:
            old = int(LOCK.read_text().strip())
            if old != os.getpid() and pid_alive(old):
                sys.exit(f"supervisor already running (pid {old})")
        except ValueError:
            pass
    HOME.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))


def tick(boot: bool) -> None:
    cfg = load_config()
    state = load_json(STATE, {})
    host = {**DEFAULT_CONFIG["host"], **(cfg.get("host") or {})}
    free = fleet_limits.free_ram_gb()
    low_ram = free < host["min_free_ram_gb"]
    if low_ram and not state.get("_low_ram"):
        log(f"host: free RAM {free:.1f} GB < {host['min_free_ram_gb']} GB - holding launches/schedules")
    state["_low_ram"] = low_ram
    for s in cfg["sessions"]:
        name = s["name"]
        st = state.setdefault(name, {})
        if not s.get("enabled", True) or st.get("stopped"):
            continue
        if pid_alive(st.get("pid")):
            watch_screen(s, st)
            continue
        if st.get("pid") and st.get("exit_logged") != st["pid"]:
            up = int((time.time() - st.get("started_at", time.time())) / 60)
            pk = fleet_limits.peak(st["pid"])  # "peak RAM 9.9/10 GB - HIT THE CAP": why it died
            log(f"{name}: process {st['pid']} gone after {up}m" + (f" ({pk})" if pk else "")
                + (f" - waiting for RAM ({free:.1f} GB free) before restarting" if low_ram and not boot else ""))
            st["exit_logged"] = st["pid"]
        sid = st.get("session_id") or s.get("session_id")
        kill_ext = st.pop("takeover_kill", False)
        if sid and (ext := running_elsewhere(cfg, eff_account(s, st), sid,
                                             owned=bool(st.get("pid")) or st.pop("takeover", False))):
            if kill_ext and ext > 0:
                # "Takeover" from the web: close the copy open outside ccfleet so ccfleet can run it
                kill_tree(ext)
                log(f"{name}: takeover - closed the copy open elsewhere (pid {ext}); started next tick")
                st.update(takeover=True, force_start=True)
                st.pop("external_pid", None)
                continue
            if not st.get("external_pid"):  # log only the transition
                log(f"{name}: already open elsewhere (pid {ext}) — not starting a second copy")
            st["external_pid"] = ext
            continue
        st.pop("external_pid", None)
        policy = session_opt(cfg, s, "restart")
        if st.get("pid") and not boot and not st.pop("force_start", False):  # manual start beats the policy
            if policy == "never":
                continue
            # on-failure: a session that ran > 60 s and closed is treated as a deliberate exit
            if policy == "on-failure" and time.time() - st.get("started_at", 0) > 60:
                continue
        if low_ram and not boot:
            continue  # host under memory pressure: wait (boot launches still go)
        if st.get("paused_until", 0) > time.time():
            continue
        recent = [x for x in st.get("restarts", []) if time.time() - x < BACKOFF_WINDOW_SEC]
        if len(recent) >= BACKOFF_MAX_RESTARTS:
            st["paused_until"] = time.time() + BACKOFF_PAUSE_SEC
            log(f"{name}: {len(recent)} restarts in {BACKOFF_WINDOW_SEC}s — pausing {BACKOFF_PAUSE_SEC}s")
            continue
        try:
            launch(cfg, s, st, st.pop("first_prompt", None) or (session_opt(cfg, s, "boot_prompt") if boot else None))
        except Exception as e:
            log(f"{name}: launch failed: {e}")
    busy = sum(1 for x in cfg["sessions"] if state.get(x["name"], {}).get("busy"))
    try:
        if not low_ram and busy < host["max_busy_sessions"]:
            run_schedules(cfg, state)
        auto_update(cfg, state)
    except Exception as e:
        log(f"schedules failed: {e}")
    try:
        import fleet_msg  # `ccfleet tell`: session-to-session messages, typed in when the target is idle
        fleet_msg.deliver(cfg, state)
    except Exception as e:
        log(f"messages failed: {e}")
    save_json(STATE, state)


def pick_port() -> int:
    """A random free port in PORT_RANGE that binds on 127.0.0.1 (obscurity is NOT the protection —
    the loopback bind, TLS in front and password + TOTP are)."""
    import random
    import socket
    for _ in range(100):
        port = random.randint(*PORT_RANGE)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port found")


def web_conf(cfg: dict | None = None) -> dict:
    """config "web" with defaults; on first use a random port is chosen, saved and announced once."""
    cfg = cfg or load_config()
    web = cfg.setdefault("web", {})
    if not web.get("port"):
        web["port"] = pick_port()
        save_json(CONFIG, cfg)
        log(f"web port {web['port']} chosen at random and saved to {CONFIG} (change: ccfleet web port <N>)")
    return {"enabled": False, "host": "127.0.0.1", **web}


def cmd_run(_a) -> None:
    acquire_lock()
    log(f"supervisor up (pid {os.getpid()}), home={HOME}")
    try:
        install_skill(load_config())  # every account gets the current ccfleet-sessions skill
    except OSError as e:
        log(f"skill install failed: {e}")
    web = web_conf()
    if web["enabled"]:
        import fleet_web  # lazy: fleet_web imports ccfleet
        fleet_web.start_thread(int(web["port"]), web["host"])
    boot = True
    try:
        while True:
            try:
                tick(boot)
                boot = False
            except Exception:
                # pythonw has no stderr: log the error and keep supervising
                import traceback
                log("tick error:\n" + traceback.format_exc())
            time.sleep(POLL_SEC)
    finally:
        LOCK.unlink(missing_ok=True)


# ---------------------------------------------------------------- commands
def cmd_status(_a) -> None:
    cfg, state = load_config(), load_json(STATE, {})
    sup = int(LOCK.read_text()) if LOCK.exists() else None
    print(f"supervisor: {'running pid ' + str(sup) if pid_alive(sup) else 'NOT running'}  ({HOME})")
    for s in cfg["sessions"]:
        st = state.get(s["name"], {})
        if not s.get("enabled", True):
            stat = "disabled"
        elif st.get("stopped"):
            stat = "stopped"
        elif pid_alive(st.get("pid")):
            stat = f"running pid {st['pid']} up {int((time.time() - st.get('started_at', time.time())) / 60)}m"
        elif st.get("external_pid"):
            stat = f"open elsewhere pid {st['external_pid']}"
        else:
            stat = "down"
        n = len([x for x in st.get("restarts", []) if time.time() - x < BACKOFF_WINDOW_SEC])
        print(f"  {s['name']:<22} {eff_account(s, st):<10} {stat:<28} restarts/10m={n} "
              f"session={st.get('session_id') or s.get('session_id') or '-'}"
              f"{'  LIMIT!' if st.get('limit_hit') else ''}")
        if st.get("remote_url"):
            print(f"  {'':<22} {st['remote_url']}")


def cmd_list(_a) -> None:
    for s in load_config()["sessions"]:
        print(f"{s['name']:<22} account={s.get('account', 'main'):<10} enabled={s.get('enabled', True)} cwd={s.get('cwd')}")


def cmd_add(a) -> None:
    cfg = load_config()
    if any(s["name"].lower() == a.name.lower() for s in cfg["sessions"]):
        sys.exit(f"session '{a.name}' exists")
    if a.account not in cfg["accounts"]:
        sys.exit(f"unknown account '{a.account}'")
    s = {"name": a.name, "account": a.account, "cwd": a.cwd, "enabled": True}
    if a.session_id:
        s["session_id"] = a.session_id
    for k in ("model", "effort", "permission_mode", "window", "restart", "boot_prompt"):
        if getattr(a, k) is not None:
            s[k] = getattr(a, k)
    if a.no_remote_control:
        s["remote_control"] = False
    cfg["sessions"].append(s)
    save_json(CONFIG, cfg)
    print(t("cli.added", name=a.name, sec=POLL_SEC))


def set_stopped(name: str, stopped: bool) -> dict:
    state = load_json(STATE, {})
    st = state.setdefault(name, {})
    st["stopped"] = stopped
    st.pop("paused_until", None)
    if not stopped:
        st["restarts"] = []
    save_json(STATE, state)
    return st


def cmd_stop(a) -> None:
    s = find_session(load_config(), a.name)
    st = set_stopped(s["name"], True)
    if pid_alive(st.get("pid")):
        kill_tree(st["pid"])
    print(t("cli.stopped", name=s["name"]))


def cmd_start(a) -> None:
    s = find_session(load_config(), a.name)
    set_stopped(s["name"], False)
    print(t("cli.will_start", name=s["name"], sec=POLL_SEC))


def cmd_takeover(a) -> None:
    """Hand a session over from VS Code/terminal to ccfleet: close it there first, then run this —
    skips the "transcript recently written" guard once (the live-process check still applies)."""
    s = find_session(load_config(), a.name)
    state = load_json(STATE, {})
    state.setdefault(s["name"], {}).update(takeover=True, stopped=False)
    save_json(STATE, state)
    print(t("cli.takeover", name=s["name"], sec=POLL_SEC))


def cmd_restart(a) -> None:
    s = find_session(load_config(), a.name)
    st = set_stopped(s["name"], False)
    if pid_alive(st.get("pid")):
        kill_tree(st["pid"])
    print(t("cli.restarting", name=s["name"]))


def running_pid(name: str) -> int:
    st = load_json(STATE, {}).get(name, {})
    if not pid_alive(st.get("pid")):
        sys.exit(t("cli.not_running", name=name))
    return st["pid"]


def cmd_peek(a) -> None:
    s = find_session(load_config(), a.name)
    print(peek_screen(s["name"], running_pid(s["name"]), a.lines), end="")


def _web_or_none(path: str, body: dict | None = None):
    try:
        return _web_api(path, body)
    except (SystemExit, OSError):
        return None


def peek_screen(name: str, pid, lines: int = 40) -> str:
    """Screen text of a session; through this machine's web API when its console can't be attached
    from here (sessions of a background supervisor: AttachConsole fails with access denied)."""
    try:
        return console("peek", str(pid), str(lines))
    except OSError:
        r = _web_or_none(f"/api/session/{name}/screen")
        if r is None:
            raise
        return r.get("text", "")


def press_keys(name: str, pid, keys: list) -> None:
    try:
        console("keys", str(pid), *keys)
    except OSError:
        if _web_or_none(f"/api/session/{name}/keys", {"keys": keys}) is None:
            raise


def send_text(name: str, text: str) -> None:
    """Type a prompt into a running session (via the local web API when the console belongs to
    the background supervisor and can't be attached from this shell)."""
    try:
        console("type", str(running_pid(name)), text)
    except OSError:
        if _web_or_none(f"/api/session/{name}/send", {"text": text}) is None:
            raise
    log(f"{name}: sent prompt ({len(text)} chars)")


def cmd_send(a) -> None:
    """Type a prompt into a running session. --clip adds the clipboard (text, or a screenshot saved
    as PNG and passed by path); --rewrite tidies it with Haiku first."""
    s = find_session(load_config(), a.name)
    text = " ".join(a.text)
    if a.clip:
        import fleet_clip
        clip = fleet_clip.as_prompt(fleet_clip.grab())
        if not clip:
            sys.exit(t("cli.clip_empty"))
        text = f"{text}\n{clip}".strip()
    if not text:
        sys.exit(t("cli.send_usage"))
    if a.rewrite:
        import fleet_rewrite
        text = fleet_rewrite.rewrite(text)
        print(text + "\n")
    send_text(s["name"], text)
    print(t("cli.sent"))


def cmd_rewrite(a) -> None:
    import fleet_rewrite
    text = " ".join(a.text)
    if a.clip or not text:
        import fleet_clip
        text = f"{text}\n{fleet_clip.text()}".strip()
    try:
        print(fleet_rewrite.rewrite(text))
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))


def discover_projects(cfg: dict) -> dict[str, dict]:
    """Projects = working dirs that have Claude Code transcripts, across all accounts.
    {cwd: {sessions, last (epoch), last_session, accounts}} — the real cwd is read from the
    newest transcript's first lines (dir names are lossy slugs)."""
    found: dict[str, dict] = {}
    for name, acc in cfg["accounts"].items():
        root = fleet_accounts.config_dir_of(acc) / "projects"
        for d in root.glob("*") if root.is_dir() else []:
            files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                continue
            cwd = None
            with files[0].open(encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(30), fh):
                    if '"cwd"' in line:
                        try:
                            cwd = json.loads(line).get("cwd")
                        except json.JSONDecodeError:
                            pass
                        if cwd:
                            break
            if ".claude-mem" in (cwd or d.name):  # plugin observer sessions, not a project
                continue
            key = os.path.normcase(cwd or d.name)
            p = found.setdefault(key, {"cwd": cwd or d.name, "sessions": 0, "last": 0, "accounts": set()})
            p["sessions"] += len(files)
            p["accounts"].add(name)
            if files[0].stat().st_mtime > p["last"]:
                p["last"], p["last_session"] = files[0].stat().st_mtime, files[0].stem
    return found


def cmd_projects(a) -> None:
    ps = sorted(discover_projects(load_config()).values(), key=lambda p: p["last"], reverse=True)
    for p in ps[: a.limit]:
        age = (time.time() - p["last"]) / 86400
        print(f"{datetime.fromtimestamp(p['last']):%Y-%m-%d} ({age:4.0f}d)  sessions={p['sessions']:<4} "
              f"{','.join(sorted(p['accounts'])):<10} {p['cwd']}  last={p['last_session']}")


def create_project(cfg: dict, name: str, path: str, account: str = "main", clone_url: str | None = None,
                   git_init: bool = True, start: bool = True, prompt: str | None = None) -> dict:
    """New project = folder (created, or `git clone`d) + a session for it.
    Returns the session dict. Raises ValueError with a user-facing message."""
    name = (name or "").strip() or Path(os.path.expanduser(path or "")).name  # empty name → folder name
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ValueError("invalid session name (letters, digits, - _ . only)")
    if any(s["name"].lower() == name.lower() for s in cfg["sessions"]):
        raise ValueError(f"a session named '{name}' already exists")
    if account not in cfg["accounts"]:
        raise ValueError(f"no account '{account}'")
    if not fleet_accounts.signed_in(cfg["accounts"][account]):
        ok = [x for x, acc in cfg["accounts"].items() if fleet_accounts.signed_in(acc)]
        raise ValueError(f"account '{account}' is not signed in on this machine"
                         + (f" — pick one of: {', '.join(ok)}" if ok else " — add a Claude account first"))
    p = Path(os.path.expanduser(path or ""))
    if not p.is_absolute():
        raise ValueError("the project folder must be a full path (e.g. D:\\projects\\my-app or ~/my-app)")
    if clone_url:
        if p.exists() and any(p.iterdir()):
            raise ValueError("the target folder is not empty")
        if not shutil.which("git"):
            raise ValueError("git is not installed on this machine — install Git for Windows, or leave the clone URL empty")
        r = subprocess.run(["git", "clone", clone_url, str(p)], capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise ValueError(f"git clone failed: {r.stderr.strip()[-300:]}")
    else:
        p.mkdir(parents=True, exist_ok=True)
        if git_init and not (p / ".git").exists() and shutil.which("git"):  # no git → plain folder
            subprocess.run(["git", "init", "-q", str(p)], capture_output=True)
    s = {"name": name, "account": account, "cwd": str(p), "enabled": True}
    if prompt:
        s["boot_prompt"] = prompt
    cfg["sessions"].append(s)
    save_json(CONFIG, cfg)
    if not start:
        set_stopped(name, True)
    elif prompt:
        # first launch gets the brief as its opening prompt (tick only sends boot_prompt at supervisor start)
        state = load_json(STATE, {})
        state.setdefault(name, {})["first_prompt"] = prompt
        save_json(STATE, state)
    log(f"project created: {name} -> {p}{' (cloned)' if clone_url else ''}")
    return s


def cmd_new(a) -> None:
    try:
        create_project(load_config(), a.name, a.path, a.account, a.clone, not a.no_git, not a.no_start, a.prompt)
    except ValueError as e:
        sys.exit(str(e))
    print(t("cli.project_created", name=a.name, path=a.path, sec=POLL_SEC))


def cmd_web(a) -> None:
    cfg = load_config()
    port = int(web_conf(cfg)["port"])
    web = cfg["web"]
    if a.action == "port":
        if a.value:
            if not a.value.isdigit() or not 1024 <= int(a.value) <= 65535:
                sys.exit("port must be a number between 1024 and 65535")
            web["port"] = int(a.value)
            save_json(CONFIG, cfg)
            print(f"web port set to {a.value} (restart the supervisor to apply)")
        else:
            print(port)
    elif a.action == "enable":
        web["enabled"] = True
        save_json(CONFIG, cfg)
        print(t("cli.web_enabled"))
    elif a.action == "disable":
        web["enabled"] = False
        save_json(CONFIG, cfg)
        print(t("cli.web_disabled"))
    elif a.action == "open":
        import webbrowser
        import fleet_web
        url = fleet_web.new_login_link(f"http://127.0.0.1:{port}")
        print(t("cli.web_open"), url)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    elif a.action == "setup":
        import fleet_web
        save_json(CONFIG, cfg)
        print(t("cli.web_setup"), fleet_web.new_setup_link(web.get("base_url") or f"http://127.0.0.1:{port}"))


def cmd_usage(_a) -> None:
    for name, acc in load_config()["accounts"].items():
        u = fleet_accounts.usage(acc)
        print(f"{name:<10} " + (f"5h={u['five_hour']:.0f}%  7d={u['seven_day']:.0f}%  plan={u['plan']}  5h-reset={u['resets']}"
                                if u else t("cli.usage_unknown")))


def _web_api(path: str, body: dict | None = None):
    """Call this machine's own web API as the local admin (random key file in CCFLEET_HOME)."""
    import secrets
    import urllib.error
    import urllib.request
    key_file = HOME / "cli.key"
    if not key_file.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{int(web_conf()['port'])}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Fleet-CLI": key_file.read_text(encoding="utf-8").strip(),
                                          "X-Fleet": "1", "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=130).read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def cmd_tell(a) -> None:
    import fleet_msg
    if a.list:
        for m in fleet_msg.pending():
            print(f"{m['id']}  {m['from']} -> {m['to']}  {int(time.time() - m['at'])}s  {m['text'][:80]}")
        return
    if not a.to or not a.text:
        sys.exit(t("cli.tell_usage"))
    # the sender is the session this runs in (launch() sets CCFLEET_SESSION), else a human at a shell
    try:
        m = fleet_msg.post(a.to, " ".join(a.text), os.environ.get("CCFLEET_SESSION") or "user")
    except ValueError as e:
        sys.exit(str(e))
    print(t("cli.tell_queued", id=m["id"], to=m["to"]))


def cmd_remove(a) -> None:
    cfg = load_config()
    s = find_session(cfg, a.name)
    cmd_stop(a)
    cfg["sessions"] = [x for x in cfg["sessions"] if x is not s]
    save_json(CONFIG, cfg)
    print(t("cli.removed", name=s["name"]))


def cmd_account(a) -> None:
    cfg = load_config()
    if a.action == "list":
        for name, acc in cfg["accounts"].items():
            r = subprocess.run([claude_exe(cfg), "auth", "status"], env=account_env(cfg, name),
                               capture_output=True, text=True, timeout=60)
            first = (r.stdout or r.stderr).strip().splitlines()[:3]
            print(f"{name:<10} dir={acc.get('config_dir') or '~/.claude'}  {' | '.join(first)}")
        return
    if not a.name:
        sys.exit("account name required")
    if a.action == "add":
        if a.name in cfg["accounts"]:
            sys.exit(f"account '{a.name}' exists")
        d = Path(a.dir or Path.home() / f".claude-{a.name}")
        d.mkdir(parents=True, exist_ok=True)
        # reuse the main account's user settings (hooks incl. image shrinker, permissions)
        main_settings = Path.home() / ".claude" / "settings.json"
        if main_settings.exists() and not (d / "settings.json").exists():
            shutil.copy2(main_settings, d / "settings.json")
        cfg["accounts"][a.name] = {"config_dir": str(d)}
        save_json(CONFIG, cfg)
        print(f"account '{a.name}' → {d}. Opening a console to sign in…")
        a.action = "login"
    if a.action == "login":
        subprocess.Popen([claude_exe(cfg), "auth", "login"], env=account_env(cfg, a.name),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        print("finish the sign-in in the new console window (browser link)")
    elif a.action == "remove":
        if any(s.get("account") == a.name for s in cfg["sessions"]):
            sys.exit("account still used by sessions")
        cfg["accounts"].pop(a.name, None)
        save_json(CONFIG, cfg)
        print(f"removed account '{a.name}' (config dir left on disk)")


def cmd_shortcut(_a) -> None:
    """Create a short `ccfleet` command: ccfleet.cmd (+ a Git Bash twin) in %USERPROFILE%\\bin, added
    to the user PATH. It hard-codes this machine's CCFLEET_HOME + python + ccfleet.py path."""
    py = sys.executable
    script = str(Path(__file__).resolve())
    bindir = Path(os.environ.get("USERPROFILE") or Path.home()) / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    tmpl = (Path(__file__).parent / "deploy" / "ccfleet.cmd.tmpl").read_text(encoding="utf-8")
    # python.exe (not pythonw) so CLI output shows in the console
    py = str(Path(py).with_name("python.exe")) if Path(py).with_name("python.exe").exists() else py
    (bindir / "ccfleet.cmd").write_text(tmpl.replace("__HOME__", str(HOME)).replace("__PYTHON__", py)
                                        .replace("__SCRIPT__", script), encoding="utf-8")
    fwd = lambda p: str(p).replace("\\", "/")  # noqa: E731
    (bindir / "ccfleet").write_bytes(
        f"#!/bin/sh\nexport CCFLEET_HOME='{fwd(HOME)}' PYTHONIOENCODING=utf-8\nexec '{fwd(py)}' '{fwd(script)}' \"$@\"\n".encode())
    cur = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetEnvironmentVariable('Path','User')"],
                         capture_output=True, text=True).stdout.strip()
    if str(bindir).lower() not in (cur or "").lower():
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"[Environment]::SetEnvironmentVariable('Path',([Environment]::GetEnvironmentVariable('Path','User')+';{bindir}'),'User')"],
                       capture_output=True)
        print(f"added {bindir} to your PATH — open a NEW terminal, then run:  ccfleet status")
    else:
        print(f"ready: {bindir}\\ccfleet.cmd — run:  ccfleet status")


def install_skill(cfg: dict) -> list:
    """Copy the bundled ccfleet-sessions skill into EVERY account's config dir (user skills load
    from CLAUDE_CONFIG_DIR, so a session on a second account would otherwise lack it)."""
    src = Path(__file__).parent / "deploy" / "skill" / "SKILL.md"
    if not src.is_file():
        return []
    out = []
    for acc in cfg["accounts"].values():
        dst = Path(acc.get("config_dir") or Path.home() / ".claude") / "skills" / "ccfleet-sessions"
        dst.mkdir(parents=True, exist_ok=True)
        if not (dst / "SKILL.md").exists() or (dst / "SKILL.md").read_bytes() != src.read_bytes():
            shutil.copy2(src, dst / "SKILL.md")
        out.append(dst)
    return out


def cmd_skill_install(_a) -> None:
    done = install_skill(load_config())
    if not done:
        sys.exit("skill source not found (deploy/skill/SKILL.md)")
    for d in done:
        print(f"installed skill 'ccfleet-sessions' -> {d} (available in all new sessions)")


def cmd_install(_a) -> None:
    """Register the boot task so the supervisor starts at boot and stays alive."""
    fleet_console.install_boot(str(HOME), sys.executable, str(Path(__file__).resolve()))
    print(t("cli.installed"))


def cmd_uninstall(_a) -> None:
    fleet_console.uninstall_boot()
    if LOCK.exists() and pid_alive(int(LOCK.read_text())):
        kill_tree(int(LOCK.read_text()))
    print(t("cli.uninstalled"))


def cmd_demo(a) -> None:
    """Web UI with fake sessions/accounts/transcripts in a throwaway data dir — no Claude needed."""
    args = [sys.executable, str(Path(__file__).with_name("fleet_demo.py")), "--port", str(a.port), "--lang", a.lang]
    if a.keep:
        args.append("--keep")
    if a.dir:
        args += ["--dir", a.dir]
    try:
        sys.exit(subprocess.call(args))
    except KeyboardInterrupt:
        pass


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # pipes default to the ANSI code page on Windows
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="ccfleet", description="Session manager for Claude Code")
    ap.add_argument("--home", help="data dir (default: $CCFLEET_HOME or ~/.ccfleet)")
    # not required: plain `ccfleet` opens the interactive menu instead of printing a usage error
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="supervisor loop (used by the boot task)").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="supervisor + sessions state").set_defaults(fn=cmd_status)
    sub.add_parser("list", help="configured sessions").set_defaults(fn=cmd_list)
    p = sub.add_parser("add", help="add a session")
    p.add_argument("name")
    p.add_argument("--cwd", required=True)
    p.add_argument("--account", default="main")
    p.add_argument("--session-id", help="resume this existing session id (default: new session)")
    p.add_argument("--model")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--permission-mode")
    p.add_argument("--window", choices=["minimized", "hidden", "normal"])
    p.add_argument("--restart", choices=["always", "on-failure", "never"])
    p.add_argument("--boot-prompt", help="prompt sent when the session is (re)started after reboot")
    p.add_argument("--no-remote-control", action="store_true")
    p.set_defaults(fn=cmd_add)
    for name, fn, h in (("start", cmd_start, "start / un-stop"), ("stop", cmd_stop, "stop and keep stopped"),
                        ("restart", cmd_restart, "kill + start again"),
                        ("takeover", cmd_takeover, "take over a session just closed in VS Code / a terminal"),
                        ("remove", cmd_remove, "stop + delete")):
        q = sub.add_parser(name, help=h)
        q.add_argument("name")
        q.set_defaults(fn=fn)
    p = sub.add_parser("peek", help="show a running session's screen text")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=30)
    p.set_defaults(fn=cmd_peek)
    p = sub.add_parser("send", help="type a prompt into a running session")
    p.add_argument("name")
    p.add_argument("text", nargs="*")
    p.add_argument("--clip", action="store_true", help="append the clipboard (text / image / copied files)")
    p.add_argument("--rewrite", action="store_true", help="rewrite the prompt clearly with Haiku before sending")
    p.set_defaults(fn=cmd_send)
    p = sub.add_parser("rewrite", help="rewrite a rough prompt clearly with Haiku (text, or the clipboard)")
    p.add_argument("text", nargs="*")
    p.add_argument("--clip", action="store_true")
    p.set_defaults(fn=cmd_rewrite)
    p = sub.add_parser("tell", help="message another session on this machine, delivered when it is idle")
    p.add_argument("to", nargs="?")
    p.add_argument("text", nargs="*")
    p.add_argument("--list", action="store_true", help="messages still waiting")
    p.set_defaults(fn=cmd_tell)
    sub.add_parser("usage", help="quota usage per account").set_defaults(fn=cmd_usage)
    p = sub.add_parser("new", help="create a project folder (or git clone) + its session")
    p.add_argument("name")
    p.add_argument("path")
    p.add_argument("--account", default="main")
    p.add_argument("--clone", help="git URL to clone into path")
    p.add_argument("--prompt", help="opening brief for the new session")
    p.add_argument("--no-git", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("web", help="web UI: open (one-time login link) / setup (password + TOTP) / enable / disable / port [N]")
    p.add_argument("action", choices=["open", "setup", "enable", "disable", "port"])
    p.add_argument("value", nargs="?", help="for 'port': the new port number")
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser("projects", help="projects discovered from Claude Code history (all accounts)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=cmd_projects)
    p = sub.add_parser("account", help="manage Claude accounts (one CLAUDE_CONFIG_DIR each)")
    p.add_argument("action", choices=["list", "add", "login", "remove"])
    p.add_argument("name", nargs="?")
    p.add_argument("--dir", help="config dir for 'add' (default ~/.claude-<name>)")
    p.set_defaults(fn=cmd_account)
    sub.add_parser("install", help="register the boot scheduled task").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the scheduled task").set_defaults(fn=cmd_uninstall)
    sub.add_parser("shortcut", help="create a short `ccfleet` command on PATH").set_defaults(fn=cmd_shortcut)
    sub.add_parser("skill-install", help="install the bundled skill into every account").set_defaults(fn=cmd_skill_install)
    sub.add_parser("menu", help="interactive session manager (same as plain `ccfleet`)").set_defaults(fn=None)
    p = sub.add_parser("demo", help="web UI with fake data on 127.0.0.1 (no Claude, no login) — for trying it / screenshots")
    p.add_argument("--port", type=int, default=0, help="default: a random free port")
    p.add_argument("--lang", choices=["en", "vi"], default="en")
    p.add_argument("--keep", action="store_true", help="keep the temporary demo data dir")
    p.add_argument("--dir", help="use this demo data dir (kept on exit) instead of a temp dir")
    p.set_defaults(fn=cmd_demo)
    a = ap.parse_args()
    if getattr(a, "fn", None) is None:
        import fleet_menu
        fleet_menu.run(a)
        return
    a.fn(a)


if __name__ == "__main__":
    main()
