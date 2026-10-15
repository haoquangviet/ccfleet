"""`ccfleet demo` — the web UI with fake data, for trying ccfleet or taking screenshots.

Creates a throwaway data dir with four fake projects, two Claude accounts, conversations and files,
then serves the web UI on 127.0.0.1 WITHOUT a login (demo mode refuses any other bind address).
No Claude Code process is started: the console, account usage, sign-in and the Haiku rewrite are
simulated. Prompts you send get a canned reply appended to the fake transcript.

    python fleet_demo.py [--port N] [--lang en|vi] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ccfleet demo")
    ap.add_argument("--port", type=int, default=0, help="default: a random free port")
    ap.add_argument("--lang", choices=["en", "vi"], default="en")
    ap.add_argument("--keep", action="store_true", help="keep the demo data dir on exit")
    ap.add_argument("--dir", help="demo data dir (default: a new temp dir)")
    return ap.parse_args()


ARGS = _parse() if __name__ == "__main__" else argparse.Namespace(port=0, lang="en", keep=False, dir=None)
DEMO_DIR = Path(ARGS.dir or tempfile.mkdtemp(prefix="ccfleet-demo-")).resolve()
os.environ["CCFLEET_HOME"] = str(DEMO_DIR / "home")
os.environ["CCFLEET_PROJECTS_ROOT"] = str(DEMO_DIR / "projects")
sys.argv = [sys.argv[0]]  # ccfleet looks for --home in argv

import ccfleet  # noqa: E402  (after CCFLEET_HOME is set)
import fleet_accounts  # noqa: E402
import fleet_login  # noqa: E402
import fleet_rewrite  # noqa: E402
import fleet_web  # noqa: E402

PROJ = DEMO_DIR / "projects"
ACCS = {"personal": DEMO_DIR / "accounts" / "personal", "work": DEMO_DIR / "accounts" / "work"}
USAGE = {"personal": {"five_hour": 37.0, "seven_day": 58.0, "max": 58.0, "resets": None, "plan": "max"},
         "work": {"five_hour": 12.0, "seven_day": 81.0, "max": 81.0, "resets": None, "plan": "max"}}

# name, account, pid (None = stopped), screen kind, conversation
SESSIONS = [
    ("web-shop", "personal", 91001, "busy", [
        ("user", "The checkout page shows the wrong total when a coupon and free shipping are combined. Fix it and add a test."),
        ("tool", "Read", {"file_path": "src/cart.ts"}),
        ("assistant", "Found it: `applyCoupon()` runs after the shipping discount, so the coupon percentage is applied to "
                      "a total that already excludes shipping. I'll reorder the steps and cover both cases with tests."),
        ("tool", "Write", {"file_path": "src/cart.ts"}),
        ("tool", "Write", {"file_path": "src/cart.test.ts"}),
        ("tool", "Bash", {"command": "npm test -- cart"}),
        ("assistant", "All 14 cart tests pass. Screenshot of the fixed checkout: docs/checkout-fixed.png"),
        ("user", "Great. Now refactor the price formatting into a shared helper."),
    ]),
    ("api-server", "work", 91002, "menu", [
        ("user", "Add rate limiting to the public endpoints (100 requests/minute per API key)."),
        ("tool", "Read", {"file_path": "src/server.py"}),
        ("assistant", "There are two reasonable ways to do this. Which do you prefer?"),
    ]),
    ("mobile-app", "personal", 91003, "idle", [
        ("user", "Write release notes for version 2.4 from the git log since v2.3."),
        ("tool", "Bash", {"command": "git log v2.3..HEAD --oneline"}),
        ("tool", "Write", {"file_path": "RELEASE_NOTES.md"}),
        ("assistant", "Done — RELEASE_NOTES.md has the 2.4 notes: offline mode, faster sync, dark-mode fixes and "
                      "11 bug fixes. Want me to open a PR?"),
    ]),
    ("docs-site", "work", None, "idle", [
        ("user", "Check every page for broken links."),
        ("tool", "Bash", {"command": "npx linkinator ./build --recurse"}),
        ("assistant", "No broken internal links. Two external links return 404: the old changelog URL and a "
                      "deleted blog post. I listed them in docs/link-report.md."),
        ("tool", "Write", {"file_path": "docs/link-report.md"}),
    ]),
]
FILES = {
    "web-shop": {"src/cart.ts": "export function total(cart) { /* … */ }\n", "src/cart.test.ts": "test('coupon + free shipping', () => {})\n",
                 "docs/checkout-fixed.png": None, "package.json": '{"name": "web-shop"}\n', ".env": "SECRET=demo\n"},
    "api-server": {"src/server.py": "app = None  # demo\n", "README.md": "# api-server\n"},
    "mobile-app": {"RELEASE_NOTES.md": "# 2.4\n- Offline mode\n- Faster sync\n", "assets/icon.png": None},
    "docs-site": {"docs/link-report.md": "# Link report\n- 2 external 404s\n", "build/index.html": "<h1>Docs</h1>\n"},
}
SCREENS = {
    "busy": ("● Reading src/format.ts\n\n"
             "✻ Refactoring… (1m 12s · ↓ 3.4k tokens · esc to interrupt)\n\n"
             " ╭──────────────────────────────────────────────╮\n │ >                                            │\n"
             " ╰──────────────────────────────────────────────╯\n   ⏵⏵ bypass permissions on (shift+tab to cycle)\n"),
    "menu": (" Rate limit\n\nWhere should the limiter live?\n\n"
             "❯ 1. Middleware (one place, all routes)\n"
             "  2. Per-route decorator (opt-in per endpoint)\n"
             "  3. Type something.\n\n"
             "Enter to select · Esc to cancel\n"),
    "idle": ("● Done.\n\n ╭──────────────────────────────────────────────╮\n │ >                                            │\n"
             " ╰──────────────────────────────────────────────╯\n   ? for shortcuts\n"),
}
RUNNING: set[int] = set()
PID_KIND: dict[int, str] = {}
PID_NAME: dict[int, str] = {}
_lock = threading.Lock()


def _slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _entry(cwd: str, role: str, minutes_ago: float, content) -> dict:
    return {"type": role, "cwd": cwd, "timestamp": _ts(minutes_ago), "message": {"role": role, "content": content}}


def _transcript(name: str) -> Path:
    cfg = ccfleet.load_config()
    s = next(x for x in cfg["sessions"] if x["name"] == name)
    st = ccfleet.load_json(ccfleet.STATE, {}).get(name, {})
    acc = Path(cfg["accounts"][ccfleet.eff_account(s, st)]["config_dir"])
    sid = st.get("session_id") or s["session_id"]
    hits = list((acc / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else acc / "projects" / _slug(s["cwd"]) / f"{sid}.jsonl"


def _append(name: str, entries: list[dict]) -> None:
    f = _transcript(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    with _lock, f.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def seed() -> None:
    from PIL import Image, ImageDraw
    home = Path(os.environ["CCFLEET_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(ccfleet.DEFAULT_CONFIG))
    cfg["accounts"] = {n: {"config_dir": str(d), "email": f"{n}@example.com", "plan": "max"} for n, d in ACCS.items()}
    cfg["lang"] = ARGS.lang
    cfg["web"] = {"enabled": True, "host": "127.0.0.1", "port": ARGS.port or None}
    state: dict = {}
    for i, (name, acc, pid, kind, convo) in enumerate(SESSIONS):
        cwd = str(PROJ / name)
        sid = f"0000demo-{i:04d}-4000-8000-{i:012d}"
        for rel, body in FILES[name].items():
            p = PROJ / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if body is None:  # a small generated image
                im = Image.new("RGB", (640, 360), (23, 71, 133) if i % 2 == 0 else (52, 141, 0))
                ImageDraw.Draw(im).text((24, 24), f"{name} — {rel}", fill=(255, 255, 255))
                im.save(p, "PNG")
            else:
                p.write_text(body, encoding="utf-8")
        (PROJ / name / ".git").mkdir(exist_ok=True)
        cfg["sessions"].append({"name": name, "account": acc, "cwd": cwd, "enabled": True, "session_id": sid})
        lines = [{"type": "summary", "cwd": cwd}]  # the reader skips the first line (it may be cut)
        ago = 30.0 + 40 * i
        for item in convo:
            ago -= 3
            if item[0] == "tool":
                inp = dict(item[2])
                if "file_path" in inp:
                    inp["file_path"] = str(PROJ / name / inp["file_path"])
                lines.append(_entry(cwd, "assistant", ago, [{"type": "tool_use", "name": item[1], "input": inp}]))
            elif item[0] == "user":
                lines.append(_entry(cwd, "user", ago, item[1]))
            else:
                lines.append(_entry(cwd, "assistant", ago, [{"type": "text", "text": item[1]}]))
        f = ACCS[acc] / "projects" / _slug(cwd) / f"{sid}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        st = {"session_id": sid, "restarts": []}
        if pid:
            st.update(pid=pid, started_at=time.time() - 60 * (25 + 30 * i), busy=kind == "busy")
            RUNNING.add(pid)
            PID_KIND[pid], PID_NAME[pid] = kind, name
        else:
            st["stopped"] = True
        state[name] = st
    for n, desc in (("code-review", "Review a diff for bugs before merging."),
                    ("release-notes", "Write release notes from the git log.")):
        d = home / "skills-lib" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {n}\ndescription: {desc}\n---\n\n# {n}\n", encoding="utf-8")
    ccfleet.save_json(ccfleet.CONFIG, cfg)
    ccfleet.save_json(ccfleet.STATE, state)
    ccfleet.LOCK.write_text(str(os.getpid()))  # "supervisor running" dot


# ---------------------------------------------------------------- simulated console & accounts
def fake_console(*args: str, timeout: int = 20) -> str:
    op, pid = args[0], int(args[1])
    if pid not in RUNNING:
        raise OSError("demo: session not running")
    kind = PID_KIND.get(pid, "idle")
    if op == "peek":
        return SCREENS[kind]
    name = PID_NAME[pid]
    cwd = str(PROJ / name)
    if op == "type":
        text = args[2] if len(args) > 2 else ""
        reply = (f"(demo) Got it. In a real session Claude Code would now work on: “{text[:120]}”"
                 if ARGS.lang == "en" else f"(demo) Đã nhận. Trong phiên thật, Claude Code sẽ làm: “{text[:120]}”")
        _append(name, [_entry(cwd, "user", 0, text)])
        threading.Timer(2.0, lambda: _append(name, [_entry(cwd, "assistant", 0, [{"type": "text", "text": reply}])])).start()
        PID_KIND[pid] = "idle"
    elif op == "keys" and kind == "menu" and ("enter" in args[2:] or "esc" in args[2:]):
        PID_KIND[pid] = "idle"
        _append(name, [_entry(cwd, "assistant", 0, [{"type": "text", "text": "(demo) Choice received — continuing."}])])
    return ""


def fake_kill(pid) -> None:
    RUNNING.discard(int(pid or 0))


_real_action = fleet_web.session_action


def demo_action(name: str, act: str, account: str | None = None) -> None:
    _real_action(name, act, account)
    if act in ("start", "restart", "takeover"):  # no supervisor here: "launch" a fake process at once
        state = ccfleet.load_json(ccfleet.STATE, {})
        st = state.setdefault(name, {})
        pid = max(PID_NAME, default=91000) + 1
        RUNNING.add(pid)
        PID_KIND[pid], PID_NAME[pid] = "idle", name
        st.update(pid=pid, started_at=time.time(), busy=False, stopped=False)
        for k in ("force_start", "takeover", "takeover_kill"):
            st.pop(k, None)
        ccfleet.save_json(ccfleet.STATE, state)


def _no_login(*_a, **_k):
    raise ValueError("signing in to a Claude account is disabled in demo mode")


def patch() -> None:
    ccfleet.pid_alive = lambda pid: bool(pid) and (int(pid) in RUNNING or int(pid) == os.getpid())
    ccfleet.kill_tree = fake_kill
    ccfleet.console = fake_console
    ccfleet.log = lambda msg: None
    fleet_accounts.usage = lambda acc: next((u for n, u in USAGE.items() if str(ACCS[n]) == acc.get("config_dir")), None)
    fleet_accounts.signed_in = lambda acc: True
    fleet_rewrite.rewrite = lambda text, cfg=None: (
        text if text.strip().startswith("/") else
        f"**Goal:** {text.strip()}\n\n**Tasks:**\n- Find the relevant code\n- Make the change\n- Add or update tests\n\n"
        "**Done when:** tests pass and you report what changed.")
    fleet_login.start = fleet_login.submit_code = _no_login
    fleet_login.poll = lambda name: {"state": "failed", "error": "disabled in demo mode"}
    fleet_login.cancel = lambda name: None
    fleet_web.session_action = demo_action
    fleet_web.DEMO = True


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # pipes default to the ANSI code page on Windows
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    seed()
    patch()
    port = int(ARGS.port or ccfleet.web_conf()["port"])
    url = f"http://127.0.0.1:{port}/"
    print(f"ccfleet demo — fake data in {DEMO_DIR}\nopen {url}  (no login; Ctrl+C to stop)", flush=True)
    try:
        fleet_web.serve(port, "127.0.0.1")
    except KeyboardInterrupt:
        pass
    finally:
        if not ARGS.keep and not ARGS.dir:
            shutil.rmtree(DEMO_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
