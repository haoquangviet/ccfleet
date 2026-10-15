"""Claude Code PreToolUse hook (matcher: Read) — shrink big images before Claude reads them.

Image tokens ≈ width × height / 750, so a 2000×900 phone screenshot costs ~2.4k tokens.
When a Read targets an image over the pixel budget, write a downscaled copy to a cache
and point the Read at it (updatedInput). Text stays readable: default budget 0.75 MP
(~1000 tokens), long edge ≤ 1568 px. Never blocks: any error → exit 0 with no output.

Env: CCFLEET_IMG_MAX_MP (default 0.75), CCFLEET_IMG_MAX_EDGE (1568), CCFLEET_IMG_CACHE (dir).
settings.json:
  "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
     "command": "<venv python> <path>/image_shrink_hook.py"}]}]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_MP = float(os.environ.get("CCFLEET_IMG_MAX_MP", "0.75"))
MAX_EDGE = int(os.environ.get("CCFLEET_IMG_MAX_EDGE", "1568"))
CACHE = Path(os.environ.get("CCFLEET_IMG_CACHE", Path(os.environ.get("TEMP", "/tmp")) / "ccfleet-img-cache"))


def shrink(src: Path) -> Path | None:
    from PIL import Image  # lazy: keep hook start fast for non-images

    with Image.open(src) as im:
        w, h = im.size
        scale = min(1.0, (MAX_MP * 1_000_000 / (w * h)) ** 0.5, MAX_EDGE / max(w, h))
        if scale >= 0.97:
            return None  # already within budget
        key = hashlib.sha1(f"{src.resolve()}|{src.stat().st_mtime_ns}|{MAX_MP}|{MAX_EDGE}".encode()).hexdigest()[:16]
        CACHE.mkdir(parents=True, exist_ok=True)
        is_photo = src.suffix.lower() in {".jpg", ".jpeg"}
        out = CACHE / f"{src.stem[:40]}-{key}{'.jpg' if is_photo else '.png'}"
        if not out.exists():
            im2 = im.convert("RGB") if is_photo or im.mode in ("P", "CMYK") else im
            im2 = im2.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            im2.save(out, quality=82, optimize=True) if is_photo else im2.save(out, optimize=True)
        return out


def main() -> None:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    if data.get("tool_name") != "Read":
        return
    ti = data.get("tool_input") or {}
    path = Path(ti.get("file_path", ""))
    if path.suffix.lower() not in EXTS or not path.is_file():
        return
    out = shrink(path)
    if not out:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"ccfleet: image downscaled to save tokens ({out.name})",
            "updatedInput": {**ti, "file_path": str(out)},
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break a Read because of the optimizer
    sys.exit(0)
