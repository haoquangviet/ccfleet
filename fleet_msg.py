"""Session-to-session messages through ccfleet (`ccfleet tell <session> "..."`), on this machine.

ccfleet owns every session it runs, so it can deliver reliably: a message is queued as one file in
<CCFLEET_HOME>/inbox/ (one file each, so the CLI and the supervisor never rewrite the same file),
the supervisor types it into the target once that session is idle, and every hop is logged.
The sender is the session the command runs in (CCFLEET_SESSION, set at launch) or "user".
"""
from __future__ import annotations

import secrets
import time

import ccfleet

INBOX = ccfleet.HOME / "inbox"
IDLE_WAIT_SEC = 180   # target busy longer than this → type anyway (Claude Code queues typed input)
EXPIRE_SEC = 3600     # undeliverable after an hour → dropped with a log line
MAX_TEXT = 4000


def _load() -> list:
    if not INBOX.is_dir():
        return []
    out = []
    for f in sorted(INBOX.glob("*.json")):
        m = ccfleet.load_json(f, None)
        if isinstance(m, dict) and m.get("id"):
            out.append(m)
    return out


def _drop(mid: str) -> None:
    for f in INBOX.glob(f"*-{mid}.json"):
        f.unlink(missing_ok=True)


def post(to: str, text: str, sender: str) -> dict:
    """Queue a message for a session on this machine."""
    text = " ".join((text or "").split())  # one line: a newline typed into the TUI submits early
    if not text or not (to or "").strip():
        raise ValueError("recipient or text missing")
    if len(text) > MAX_TEXT:
        raise ValueError(f"message too long (> {MAX_TEXT} characters) — write it to a file and send the path")
    msg = {"id": secrets.token_hex(4), "to": to.strip(), "from": sender or "user", "text": text, "at": time.time()}
    INBOX.mkdir(parents=True, exist_ok=True)
    ccfleet.save_json(INBOX / f"{int(msg['at'] * 1000)}-{msg['id']}.json", msg)
    return msg


def _format(msg: dict) -> str:
    return (f"[ccfleet · message from session {msg['from']}] {msg['text']} "
            f"(reply with: ccfleet tell {msg['from']} \"...\")")


def deliver(cfg: dict, state: dict) -> None:
    """Supervisor tick: type due messages into their sessions once those are idle."""
    q = _load()
    if not q:
        return
    done = set()
    names = {s["name"].lower(): s["name"] for s in cfg["sessions"]}
    for m in q:
        age = time.time() - m["at"]
        try:
            real = names.get(m["to"].lower())
            if not real:
                raise LookupError(f"no session '{m['to']}'")
            st = state.get(real, {})
            if not ccfleet.pid_alive(st.get("pid")):
                raise TimeoutError
            if st.get("busy") and age < IDLE_WAIT_SEC:
                continue
            ccfleet.console("type", str(st["pid"]), _format(m))
            ccfleet.log(f"msg {m['id']} {m['from']} -> {m['to']} delivered ({len(m['text'])} chars)")
            done.add(m["id"])
        except LookupError as e:
            ccfleet.log(f"msg {m['id']} {m['from']} -> {m['to']} dropped: {e}")
            done.add(m["id"])
        except (TimeoutError, OSError, RuntimeError) as e:
            if age > EXPIRE_SEC:
                ccfleet.log(f"msg {m['id']} {m['from']} -> {m['to']} expired undelivered ({e or 'session not running'})")
                done.add(m["id"])
    for mid in done:
        _drop(mid)


def pending() -> list:
    return _load()
