"""Per-project file manager for the ccfleet web: browse / view / upload / mkdir / delete inside a
project root, so big inputs (screenshots, specs, logs…) are saved to disk and only their PATH is
sent to Claude Code (cheaper than pasting content; images still pass the shrink hook on Read).

Safety: every path is resolved and must stay inside the project root (no traversal, no symlink
escape); .git internals are hidden; secret-looking files (.env*, *.key, *.pem, credentials) are
listed but never served or overwritten; uploads are size-capped.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

INBOX = ".ccfleet-inbox"
MAX_UPLOAD = 100 * 1024 * 1024
SECRET_RE = re.compile(r"(^\.env(\..*)?$)|(\.(key|pem|p8|pfx|p12)$)|(credentials?\.json$)|(^id_(rsa|ed25519))", re.I)
HIDE = {".git", "node_modules", "__pycache__", ".venv"}


def safe_path(root: str, rel: str) -> Path:
    base = Path(root).resolve()
    p = (base / (rel or "").lstrip("/\\")).resolve()
    if p != base and base not in p.parents:
        raise PermissionError("outside the project folder")
    return p


def listing(root: str, rel: str) -> dict:
    d = safe_path(root, rel)
    if not d.is_dir():
        raise FileNotFoundError(rel)
    items = []
    for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name in HIDE:
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        items.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size if e.is_file() else None,
                      "mtime": st.st_mtime, "secret": bool(SECRET_RE.search(e.name))})
    base = Path(root).resolve()
    return {"root": str(base), "rel": str(d.relative_to(base)).replace("\\", "/").strip("."),
            "abs": str(d), "items": items[:2000]}


def read(root: str, rel: str) -> tuple[Path, int]:
    p = safe_path(root, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    if SECRET_RE.search(p.name):
        raise PermissionError("secret-looking file — not served over the web")
    return p, p.stat().st_size


def save_upload(root: str, rel_dir: str, name: str, data: bytes) -> Path:
    name = os.path.basename(name or "").strip()
    if not name or name in (".", "..") or SECRET_RE.search(name):
        raise ValueError("invalid file name")
    if len(data) > MAX_UPLOAD:
        raise ValueError("file too large")
    d = safe_path(root, rel_dir if rel_dir else INBOX)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if p.exists():  # never overwrite: keep both
        p = d / f"{p.stem}-{time.strftime('%H%M%S')}{p.suffix}"
    p.write_bytes(data)
    return p


def mkdir(root: str, rel: str) -> Path:
    p = safe_path(root, rel)
    p.mkdir(parents=True, exist_ok=True)
    return p


def delete(root: str, rel: str) -> None:
    p = safe_path(root, rel)
    if p == Path(root).resolve() or not p.is_file():
        raise ValueError("only files can be deleted")
    if SECRET_RE.search(p.name):
        raise PermissionError("secret-looking file — not deleted over the web")
    p.unlink()
