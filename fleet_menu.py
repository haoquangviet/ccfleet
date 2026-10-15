"""ccfleet menu — what plain `ccfleet` does in a terminal.

The session list, then one keystroke per action: numbers pick a session, letters act on it.
No sub-command names to remember. Everything reuses ccfleet.py's own helpers, so the menu can
never drift from what `ccfleet status` and the web UI report. Strings come from fleet_i18n.
"""
from __future__ import annotations

import time

import ccfleet
import fleet_i18n
from fleet_i18n import t


def _rows() -> list[tuple[dict, dict, str]]:
    """(session, state, status) for every configured session, in config order."""
    cfg, state = ccfleet.load_config(), ccfleet.load_json(ccfleet.STATE, {})
    out = []
    for s in cfg["sessions"]:
        st = {**state.get(s["name"], {}), "_name": s["name"]}
        if not s.get("enabled", True):
            stat = t("st.disabled")
        elif st.get("stopped"):
            stat = t("st.stopped")
        elif ccfleet.pid_alive(st.get("pid")):
            stat = t("st.running", up=int((time.time() - st.get("started_at", time.time())) / 60))
        elif st.get("external_pid"):
            stat = t("st.elsewhere")
        else:
            stat = t("st.down")
        out.append((s, st, stat))
    return out


def _busy_mark(st: dict) -> str:
    """working / idle — only for sessions ccfleet actually owns a console for."""
    if not ccfleet.pid_alive(st.get("pid")):
        return ""
    try:
        return t("busy.yes") if ccfleet.is_busy(ccfleet.peek_screen(st.get("_name", ""), st["pid"], 20)) else t("busy.no")
    except Exception:
        return "?"


def _show_list() -> list[tuple[dict, dict, str]]:
    rows = _rows()
    sup = int(ccfleet.LOCK.read_text()) if ccfleet.LOCK.exists() else None
    alive = ccfleet.pid_alive(sup)
    print()
    print(t("menu.header", sup=t("menu.sup_on", pid=sup) if alive else t("menu.sup_off"), home=ccfleet.HOME))
    if not alive:
        print(t("menu.sup_hint"))
    print()
    for i, (s, st, stat) in enumerate(rows, 1):
        print(f"  {i:>2}. {s['name']:<24}{stat:<16}{_busy_mark(st):<13}{s.get('cwd', '')}")
    if not rows:
        print(t("menu.empty"))
    print()
    print(t("menu.prompt"))
    return rows


def _line(prompt: str) -> str | None:
    try:
        return input(prompt).lstrip("﻿")  # piping from PowerShell prefixes a BOM
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _ask(prompt: str) -> str:
    ln = _line(prompt)
    return "q" if ln is None else ln.strip()


def compose() -> str | None:
    """Multi-line prompt with clipboard paste (:v) and a Haiku rewrite (:r). None = cancelled."""
    import fleet_clip
    print(t("compose.help"))
    lines: list[str] = []
    while True:
        ln = _line("  … " if lines else "  > ")
        if ln is None or ln.strip() == ":x":
            return None
        cmd = ln.strip()
        if cmd == "":
            return "\n".join(lines).strip() or None
        if cmd == ":v":
            g = fleet_clip.grab()
            txt = fleet_clip.as_prompt(g)
            if not txt:
                print(t("compose.clip_empty"))
                continue
            lines += txt.splitlines()
            print(t("compose.clip_files", images=len(g["images"]), files=len(g["files"])) if not g["text"]
                  else t("compose.clip_text", n=len(txt), first=txt.splitlines()[0][:70]))
        elif cmd == ":r":
            draft = "\n".join(lines).strip()
            if not draft:
                print(t("compose.nothing"))
                continue
            print(t("compose.rewriting"))
            try:
                import fleet_rewrite
                new = fleet_rewrite.rewrite(draft)
            except Exception as e:
                print(t("err", e=e))
                continue
            print("\n" + "\n".join("    " + x for x in new.splitlines()) + "\n")
            if (_line(t("compose.pick")) or "").strip().lower() != "b":
                lines = new.splitlines()
            print(t("compose.after"))
        else:
            lines.append(ln.rstrip())


def _peek(s: dict, st: dict) -> None:
    if not ccfleet.pid_alive(st.get("pid")):
        print(t("sess.not_running"))
        return
    print()
    try:
        print(ccfleet.peek_screen(s["name"], st["pid"], 40), end="")
    except OSError as e:
        print(t("sess.screen_err", e=e))
    print()


def _session_menu(name: str) -> None:
    """Act on one session until the user backs out. State is re-read every loop so the status
    shown is current even while the supervisor restarts things underneath us."""
    while True:
        rows = {s["name"]: (s, st, stat) for s, st, stat in _rows()}
        if name not in rows:
            print(t("sess.gone", name=name))
            return
        s, st, stat = rows[name]
        busy = _busy_mark(st)
        print()
        print(f"  ── {name}  [{stat}{(' · ' + busy) if busy else ''}]  {s.get('cwd', '')}")
        print(t("menu.actions"))
        c = _ask("  > ").lower()
        if c in ("q", "0"):
            return
        if c == "":
            _peek(s, st)
        elif c == "g":
            text = compose()
            if text:
                if not ccfleet.pid_alive(st.get("pid")):
                    print(t("sess.not_running"))
                    continue
                try:
                    ccfleet.send_text(name, text)
                except OSError as e:
                    print(t("err", e=e))
                    continue
                print(t("sess.sent"))
        elif c == "k":
            keys = _ask(t("sess.keys_prompt")).split()
            if keys and keys != ["q"]:
                if not ccfleet.pid_alive(st.get("pid")):
                    print(t("sess.not_running"))
                    continue
                try:
                    ccfleet.press_keys(name, st["pid"], keys)
                except OSError as e:
                    print(t("err", e=e))
                    continue
                print(t("sess.keys_sent"))
        elif c == "c":
            ccfleet.set_stopped(name, False)
            print(t("sess.will_start", sec=ccfleet.POLL_SEC))
        elif c == "d":
            st2 = ccfleet.set_stopped(name, True)
            if ccfleet.pid_alive(st2.get("pid")):
                ccfleet.kill_tree(st2["pid"])
            print(t("sess.stopped"))
        elif c == "r":
            st2 = ccfleet.set_stopped(name, False)
            if ccfleet.pid_alive(st2.get("pid")):
                ccfleet.kill_tree(st2["pid"])
            print(t("sess.restarting"))
        else:
            print(t("menu.unknown_action"))


def run(_a=None) -> None:
    """Entry point for plain `ccfleet`."""
    while True:
        rows = _show_list()
        c = _ask("  > ").lower()
        if c == "q":
            return
        if c == "u":
            ccfleet.cmd_usage(None)
        elif c == "p":
            ccfleet.cmd_projects(type("A", (), {"limit": 20})())
        elif c == "w":
            ccfleet.cmd_web(type("A", (), {"action": "open"})())
        elif c == "l":
            print(t("lang.set", lang=fleet_i18n.set_lang("vi" if fleet_i18n.lang() == "en" else "en")))
        elif c.isdigit() and 1 <= int(c) <= len(rows):
            _session_menu(rows[int(c) - 1][0]["name"])
        elif c:
            print(t("menu.unknown"))
