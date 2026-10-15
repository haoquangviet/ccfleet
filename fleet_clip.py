"""Clipboard for the `ccfleet` CLI: paste text or an image (screenshot) into a prompt.

An image is saved as PNG under <CCFLEET_HOME>/paste/ and its path goes into the prompt — Claude Code
reads an image when a prompt names its file (the PreToolUse hook shrinks big ones). Copied files
(Explorer / Finder "Copy") come in as their paths. Images via Pillow's ImageGrab (Windows; macOS
through osascript; Linux needs wl-paste or xclip); text via PowerShell Get-Clipboard (Windows),
pbpaste (macOS), wl-paste / xclip / xsel (Linux). Must run in the user's desktop session (not the
supervisor's): over SSH, or without a clipboard tool, it degrades to "clipboard is empty".
Also used by the web composer: an image pasted there is stored by save_bytes().
"""
from __future__ import annotations

import subprocess
import sys
import time

import ccfleet

PASTE_DIR = ccfleet.HOME / "paste"
KEEP_DAYS = 14
MAX_IMAGE = 10 * 1024 * 1024


TEXT_CMDS = {
    "darwin": [["pbpaste"]],
    "linux": [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"]],
}


def text() -> str:
    if sys.platform == "win32":
        cmds = [["powershell", "-NoProfile", "-STA", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"]]
    else:
        cmds = TEXT_CMDS.get(sys.platform, TEXT_CMDS["linux"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue  # tool not installed / no display: try the next one
        if r.returncode == 0:
            return (r.stdout or "").replace("\r\n", "\n").strip()
    return ""


def _cleanup() -> None:
    for f in PASTE_DIR.glob("*.png") if PASTE_DIR.is_dir() else []:
        try:
            if time.time() - f.stat().st_mtime > KEEP_DAYS * 86400:
                f.unlink()
        except OSError:
            pass


def grab() -> dict:
    """What is on the clipboard now: {"images": [paths], "files": [paths], "text": str}."""
    out = {"images": [], "files": [], "text": ""}
    try:
        from PIL import ImageGrab
        g = ImageGrab.grabclipboard()
    except Exception:
        g = None
    if isinstance(g, list):  # files copied in Explorer
        out["files"] = [str(p) for p in g]
    elif g is not None:
        PASTE_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup()
        f = PASTE_DIR / f"paste-{time.strftime('%Y%m%d-%H%M%S')}.png"
        g.save(f, "PNG")
        out["images"] = [str(f)]
    if not out["images"] and not out["files"]:
        out["text"] = text()
    return out


def as_prompt(g: dict) -> str:
    """Clipboard content as prompt text (paths for images/files)."""
    parts = [f"[image: {p}]" for p in g["images"]] + [f"[file: {p}]" for p in g["files"]]
    if g["text"]:
        parts.append(g["text"])
    return " ".join(parts) if parts and not g["text"] else "\n".join(parts)


def save_bytes(data: bytes) -> str:
    """An image pasted in the web composer → PNG in PASTE_DIR. Decoded and re-encoded with Pillow,
    so whatever arrives must really be an image (and lands as a plain PNG)."""
    import io
    from PIL import Image
    if not data or len(data) > MAX_IMAGE:
        raise ValueError("image is empty or larger than 10 MB")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError("not an image")
    PASTE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup()
    f = PASTE_DIR / f"paste-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    im.save(f, "PNG")
    return str(f)
