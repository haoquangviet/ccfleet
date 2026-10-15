"""Tiny i18n for the ccfleet CLI and terminal menu: English (default/fallback) and Vietnamese.

Language: config.json "lang" ("en" | "vi"), else the LANG environment variable (vi* → Vietnamese),
else English. Use t("key", name=...) — str.format placeholders; a missing key falls back to English.
"""
from __future__ import annotations

import os

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # terminal menu
        "menu.actions": ("  [enter] view screen      [g] send (+ paste)     [c] start / un-stop\n"
                         "  [d] stop                 [r] restart            [k] keys (esc, up, enter…)\n"
                         "  [q] back to the list"),
        "st.disabled": "disabled", "st.stopped": "stopped", "st.running": "running {up}m",
        "st.elsewhere": "open elsewhere", "st.down": "not running",
        "busy.yes": "working", "busy.no": "idle",
        "menu.header": "  ccfleet — supervisor {sup}   ({home})",
        "menu.sup_on": "running pid {pid}", "menu.sup_off": "NOT running",
        "menu.sup_hint": "  ! the supervisor is not running → sessions will not restart by themselves."
                         "  Start it:  ccfleet install  (or: ccfleet run)",
        "menu.empty": "  (no sessions yet — create one with:  ccfleet new <name> <path>)",
        "menu.prompt": "  Type a number to pick a session · [u] usage · [p] projects · [w] open web · [l] language · [q] quit",
        "menu.unknown": "  not understood — type a session number, or u / p / w / l / q",
        "menu.unknown_action": "  not understood — see the choices above",
        "compose.help": ("  Write the prompt — type and press Enter (several lines are fine), an EMPTY LINE = send.\n"
                         "  :v paste clipboard (text / image / copied files)   :r rewrite clearly (Haiku)   :x cancel"),
        "compose.clip_empty": "  (clipboard is empty)",
        "compose.clip_files": "  + {images} image(s), {files} file(s)",
        "compose.clip_text": "  + {n} characters: {first}",
        "compose.nothing": "  (nothing to rewrite yet)",
        "compose.rewriting": "  rewriting with Haiku…",
        "compose.pick": "  [enter] use this version · [b] keep the old one: ",
        "compose.after": "  (empty line to send, or keep typing)",
        "err": "  error: {e}",
        "sess.not_running": "  session is not running — press [c] to start it",
        "sess.screen_err": "  cannot read the screen: {e}",
        "sess.gone": "  session '{name}' is no longer configured",
        "sess.sent": "  sent", "sess.keys_prompt": "  keys (separated by spaces): ", "sess.keys_sent": "  keys sent",
        "sess.will_start": "  starts within {sec}s", "sess.stopped": "  stopped (press [c] to start again)",
        "sess.restarting": "  restarting",
        "lang.set": "  language: {lang}",
        # CLI
        "cli.added": "added {name}; the supervisor starts it within {sec}s (if running)",
        "cli.stopped": "stopped {name} (stays stopped until: ccfleet start {name})",
        "cli.will_start": "{name} will start within {sec}s",
        "cli.takeover": "{name}: ccfleet takes over within {sec}s (make sure the other window is closed)",
        "cli.restarting": "restarting {name}",
        "cli.removed": "removed {name}",
        "cli.sent": "sent",
        "cli.clip_empty": "clipboard is empty",
        "cli.send_usage": 'usage: ccfleet send <session> "text" [--clip] [--rewrite]',
        "cli.tell_usage": 'usage: ccfleet tell <session> "text"   (ccfleet tell --list: messages still waiting)',
        "cli.tell_queued": "queued {id} -> {to}: the supervisor types it in when that session is idle",
        "cli.project_created": "project {name} at {path} — its session starts within {sec}s",
        "cli.web_enabled": "web enabled — restart the supervisor to start it (ccfleet uninstall + install, or stop/start `ccfleet run`)",
        "cli.web_disabled": "web disabled (takes effect when the supervisor restarts)",
        "cli.web_open": "web UI of this machine (one-time link, 2 minutes):",
        "cli.web_setup": "one-time setup link (30 minutes) — set the password + TOTP there:",
        "cli.usage_unknown": "usage unknown (token expired or not signed in)",
        "cli.installed": "boot task registered and started (the supervisor runs at boot and is kept alive)",
        "cli.uninstalled": "boot task removed, supervisor stopped (sessions keep running)",
        "cli.not_running": "{name} is not running under ccfleet",
    },
    "vi": {
        "menu.actions": ("  [enter] xem màn hình     [g] gửi + dán ảnh      [c] chạy / bỏ dừng\n"
                         "  [d] dừng                 [r] khởi động lại     [k] phím (esc, up, enter…)\n"
                         "  [q] quay lại danh sách"),
        "st.disabled": "tắt", "st.stopped": "đã dừng", "st.running": "chạy {up}m",
        "st.elsewhere": "mở ở nơi khác", "st.down": "không chạy",
        "busy.yes": "đang trả lời", "busy.no": "rảnh",
        "menu.header": "  ccfleet — supervisor {sup}   ({home})",
        "menu.sup_on": "đang chạy pid {pid}", "menu.sup_off": "KHÔNG chạy",
        "menu.sup_hint": "  ! supervisor không chạy → phiên sẽ không tự khởi động lại."
                         "  Khởi động:  ccfleet install  (hoặc: ccfleet run)",
        "menu.empty": "  (chưa có phiên nào — tạo bằng:  ccfleet new <tên> <đường dẫn>)",
        "menu.prompt": "  Nhập số để chọn phiên · [u] hạn mức · [p] dự án · [w] mở web · [l] ngôn ngữ · [q] thoát",
        "menu.unknown": "  không hiểu — nhập số phiên, hoặc u / p / w / l / q",
        "menu.unknown_action": "  không hiểu — xem các lựa chọn ở trên",
        "compose.help": ("  Soạn lời nhắc — gõ rồi Enter (nhiều dòng được), DÒNG TRỐNG = gửi.\n"
                         "  :v dán clipboard (text / ảnh / file đã copy)   :r viết lại cho rõ (Haiku)   :x huỷ"),
        "compose.clip_empty": "  (clipboard trống)",
        "compose.clip_files": "  + {images} ảnh, {files} file",
        "compose.clip_text": "  + {n} ký tự: {first}",
        "compose.nothing": "  (chưa có gì để viết lại)",
        "compose.rewriting": "  đang viết lại bằng Haiku…",
        "compose.pick": "  [enter] dùng bản này · [b] giữ bản cũ: ",
        "compose.after": "  (Enter dòng trống để gửi, hoặc gõ thêm)",
        "err": "  lỗi: {e}",
        "sess.not_running": "  phiên không chạy — bấm [c] để chạy",
        "sess.screen_err": "  không đọc được màn hình: {e}",
        "sess.gone": "  phiên '{name}' không còn trong cấu hình",
        "sess.sent": "  đã gửi", "sess.keys_prompt": "  phím (cách nhau bằng dấu cách): ", "sess.keys_sent": "  đã gửi phím",
        "sess.will_start": "  sẽ chạy trong {sec}s", "sess.stopped": "  đã dừng (bấm [c] để chạy lại)",
        "sess.restarting": "  đang khởi động lại",
        "lang.set": "  ngôn ngữ: {lang}",
        "cli.added": "đã thêm {name}; supervisor sẽ chạy nó trong {sec}s (nếu đang chạy)",
        "cli.stopped": "đã dừng {name} (dừng hẳn tới khi: ccfleet start {name})",
        "cli.will_start": "{name} sẽ chạy trong {sec}s",
        "cli.takeover": "{name}: ccfleet tiếp quản trong {sec}s (nhớ đóng cửa sổ kia)",
        "cli.restarting": "đang khởi động lại {name}",
        "cli.removed": "đã xoá {name}",
        "cli.sent": "đã gửi",
        "cli.clip_empty": "clipboard trống",
        "cli.send_usage": 'cách dùng: ccfleet send <phiên> "nội dung" [--clip] [--rewrite]',
        "cli.tell_usage": 'cách dùng: ccfleet tell <phiên> "nội dung"   (ccfleet tell --list: tin đang chờ)',
        "cli.tell_queued": "đã xếp {id} -> {to}: supervisor gõ vào khi phiên đó rảnh",
        "cli.project_created": "dự án {name} tại {path} — phiên sẽ chạy trong {sec}s",
        "cli.web_enabled": "đã bật web — khởi động lại supervisor để chạy (ccfleet uninstall + install, hoặc dừng/chạy lại `ccfleet run`)",
        "cli.web_disabled": "đã tắt web (có hiệu lực khi supervisor khởi động lại)",
        "cli.web_open": "web của máy này (link dùng 1 lần, 2 phút):",
        "cli.web_setup": "link thiết lập dùng 1 lần (30 phút) — đặt mật khẩu + TOTP tại đó:",
        "cli.usage_unknown": "chưa rõ hạn mức (token hết hạn hoặc chưa đăng nhập)",
        "cli.installed": "đã đăng ký và chạy task khởi động (supervisor chạy lúc boot và được giữ sống)",
        "cli.uninstalled": "đã gỡ task khởi động, dừng supervisor (các phiên vẫn chạy)",
        "cli.not_running": "{name} không chạy dưới ccfleet",
    },
}

_lang: str | None = None


def detect(cfg: dict | None = None) -> str:
    v = str((cfg or {}).get("lang") or os.environ.get("CCFLEET_LANG") or os.environ.get("LANG") or "").lower()
    return "vi" if v.startswith("vi") else "en"


def set_lang(lang: str | None) -> str:
    global _lang
    _lang = lang if lang in STRINGS else "en"
    return _lang


def lang() -> str:
    if _lang is None:
        try:
            import ccfleet
            set_lang(detect(ccfleet.load_json(ccfleet.CONFIG, {}) or {}))
        except Exception:
            set_lang(detect())
    return _lang


def t(key: str, **kw) -> str:
    s = STRINGS.get(lang(), {}).get(key) or STRINGS["en"].get(key) or key
    return s.format(**kw) if kw else s
