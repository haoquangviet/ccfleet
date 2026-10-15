"""Read the tail of a Claude Code session transcript (<config_dir>/projects/*/<sid>.jsonl)
as compact chat items for the ccfleet web UI: user text, assistant text, tool calls (name + short arg).
Reads only the last `max_bytes` of the file — transcripts grow to hundreds of MB."""
from __future__ import annotations

import json
from pathlib import Path

import fleet_accounts


def find(acc: dict, session_id: str) -> Path | None:
    root = fleet_accounts.config_dir_of(acc) / "projects"
    hits = list(root.glob(f"*/{session_id}.jsonl")) if root.is_dir() else []
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def project_sessions(accounts: dict, cwd: str, limit: int = 30) -> list[dict]:
    """Past sessions of a project across accounts: id, account, last activity, size, first prompt
    (title). Matches transcripts by their recorded cwd (dir slugs are lossy)."""
    import os
    want = os.path.normcase(cwd)
    out: list[dict] = []
    for acc_name, acc in accounts.items():
        root = fleet_accounts.config_dir_of(acc) / "projects"
        for d in root.iterdir() if root.is_dir() else []:
            files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files or os.path.normcase(_cwd_of(files[0]) or "") != want:
                continue
            for f in files[:limit]:
                out.append({"session_id": f.stem, "account": acc_name, "last": f.stat().st_mtime,
                            "size_kb": f.stat().st_size // 1024, "title": _first_prompt(f)})
    return sorted(out, key=lambda x: x["last"], reverse=True)[:limit]


def _cwd_of(f: Path) -> str | None:
    with f.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(40), fh):
            if '"cwd"' in line:
                try:
                    if c := json.loads(line).get("cwd"):
                        return c
                except json.JSONDecodeError:
                    pass
    return None


def _first_prompt(f: Path) -> str:
    with f.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(200), fh):
            if '"type":"user"' not in line.replace(" ", ""):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            for it in _text_of((e.get("message") or {}).get("content")):
                t = it["text"].strip()
                if it["kind"] == "text" and t and not t.startswith("<"):
                    return t[:140]
    return ""


def _text_of(content) -> list[dict]:
    if isinstance(content, str):
        return [{"kind": "text", "text": content}]
    items = []
    for b in content or []:
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            items.append({"kind": "text", "text": b["text"]})
        elif t == "tool_use":
            inp = b.get("input") or {}
            arg = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            items.append({"kind": "tool", "text": f"{b.get('name')}: {str(arg)[:160]}"})
    return items


def tail(acc: dict, session_id: str, n: int = 40, max_bytes: int = 1_500_000) -> list[dict]:
    f = find(acc, session_id)
    if not f:
        return []
    with f.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - max_bytes))
        raw = fh.read().decode("utf-8", errors="replace").splitlines()[1:]  # first line may be cut
    out: list[dict] = []
    for line in raw:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = e.get("type")
        # a prompt typed while the session is busy is NOT a user entry: Claude Code logs
        # queue-operation enqueue → (remove + attachment queued_command when absorbed mid-turn).
        # Without these the web would never show what was sent during a turn.
        if role == "queue-operation" and isinstance(e.get("content"), str) and not e["content"].startswith("<"):
            if e.get("operation") == "enqueue":
                out.append({"role": "user", "kind": "text", "text": e["content"][:4000], "ts": e.get("timestamp"), "queued": True})
            elif e.get("operation") in ("remove", "dequeue"):
                for i in range(len(out) - 1, -1, -1):
                    if out[i].get("queued") and out[i]["text"] == e["content"][:4000]:
                        del out[i]
                        break
            continue
        if role == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str) and not a["prompt"].startswith("<"):
                out.append({"role": "user", "kind": "text", "text": a["prompt"][:4000], "ts": e.get("timestamp")})
            continue
        if role not in ("user", "assistant") or e.get("isMeta"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        # user entries that only carry tool results / system reminders are noise for the UI
        if role == "user" and isinstance(content, list) and all(b.get("type") == "tool_result" for b in content):
            continue
        for it in _text_of(content):
            txt = it["text"]
            if role == "user" and (txt.startswith("<system-reminder>") or txt.startswith("<task-notification>")):
                continue
            out.append({"role": role, "kind": it["kind"], "text": txt[:4000], "ts": e.get("timestamp")})
    return out[-n:]


MEDIA = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
         ".mp4": "video", ".webm": "video", ".mov": "video", ".mkv": "video",
         ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".pdf": "pdf"}
_PATH_RE = None
_scan_cache: dict = {}  # transcript path -> (mtime, cwd, paste_dir, {normcase path: item})


def _scan(f: Path, cwd: str, max_bytes: int, paste_dir: str | None) -> dict:
    """Paths one transcript wrote / pointed at / received, inside `cwd` (or images in paste_dir)."""
    import os
    import re
    global _PATH_RE
    if _PATH_RE is None:
        ext = "|".join(e.lstrip(".") for e in MEDIA) + "|md|txt|csv|json|html|docx|xlsx|pptx|zip|log|py|ts|tsx|js|ps1|sh|sql"
        _PATH_RE = re.compile(r"(?:[A-Za-z]:[\/]|\.{0,2}[\/]|[\w.-]+[\/])?[^\s\"'`<>|*?\[\](){}]*?\.(?:" + ext + r")\b", re.I)
    try:
        mt = f.stat().st_mtime
    except OSError:
        return {}
    hit = _scan_cache.get(str(f))
    if hit and hit[:3] == (mt, cwd, paste_dir):
        return hit[3]
    rbase = os.path.realpath(cwd)
    base = os.path.normcase(rbase)
    pbase = os.path.normcase(os.path.realpath(paste_dir)) if paste_dir else None
    with f.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - max_bytes))
        raw = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
    seen: dict[str, dict] = {}

    def add(p: str, via: str, ts):
        p = p.strip().strip(".,;:").replace("/", os.sep)
        if not p or len(p) > 400:
            return
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))
        nf = os.path.normcase(full)
        in_paste = bool(pbase) and os.path.dirname(nf) == pbase
        if not (nf.startswith(base + os.sep) or in_paste) or nf in seen:
            if nf in seen and via == "write":
                seen[nf]["via"] = "write"
            return
        if not os.path.isfile(full):
            return
        ext = os.path.splitext(full)[1].lower()
        seen[nf] = {"full": full, "rel": os.path.basename(full) if in_paste else os.path.relpath(full, rbase).replace("\\", "/"),
                    "name": os.path.basename(full), "src": "paste" if in_paste else "project",
                    "kind": MEDIA.get(ext, "file"), "via": via, "ts": ts}

    for line in raw:
        if '"assistant"' not in line and '"user"' not in line and '"queued_command"' not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = e.get("timestamp")
        content = (e.get("message") or {}).get("content")
        a = e.get("attachment") if e.get("type") == "attachment" else None
        if isinstance(a, dict) and a.get("type") == "queued_command" and isinstance(a.get("prompt"), str):
            for m in _PATH_RE.finditer(a["prompt"][:20000]):
                add(m.group(0), "user", ts)
            continue
        if e.get("type") == "user" and not e.get("isMeta"):
            texts = [content] if isinstance(content, str) else \
                [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            for t in texts:
                if not t.startswith("<"):  # system reminders / notifications are not the user's
                    for m in _PATH_RE.finditer(t[:20000]):
                        add(m.group(0), "user", ts)
            continue
        if e.get("type") != "assistant":
            continue
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if inp.get("file_path") and b.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    add(str(inp["file_path"]), "write", ts)
                elif isinstance(inp.get("command"), str):
                    for m in _PATH_RE.finditer(inp["command"][:4000]):
                        add(m.group(0), "bash", ts)
            elif b.get("type") == "text":
                for m in _PATH_RE.finditer(b.get("text", "")[:20000]):
                    add(m.group(0), "text", ts)
    _scan_cache[str(f)] = (mt, cwd, paste_dir, seen)  # a big transcript takes seconds to scan; the panel polls
    return seen


def _finish(merged: dict, limit: int) -> list[dict]:
    import os
    out = []
    for it in merged.values():
        try:
            st = os.stat(it["full"])  # fresh: a cached scan may be days old
        except OSError:
            continue
        out.append({**{k: v for k, v in it.items() if k != "full"}, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:limit]


def files(acc: dict, session_id: str, cwd: str, max_bytes: int = 6_000_000, limit: int = 150,
          paste_dir: str | None = None) -> list[dict]:
    """Files this session wrote or pointed at (Write/Edit/NotebookEdit inputs, paths in its text and
    Bash commands) plus what the user sent in (uploads, images pasted from the web) that still exist
    INSIDE its project — for the web's session Files panel. Outside the project only images in
    `paste_dir` count (served by name); everything else stays out — the web only serves known roots.
    Each item: rel, name, size, mtime, kind (image/video/audio/pdf/file), via (write/bash/text/user), src."""
    f = find(acc, session_id)
    return _finish(_scan(f, cwd, max_bytes, paste_dir), limit) if f else []


def project_files(accounts: dict, cwd: str, session_id: str | None = None, paste_dir: str | None = None,
                  limit: int = 400, max_transcripts: int = 30, max_bytes: int = 6_000_000) -> list[dict]:
    """files() over EVERY conversation of the project (all accounts, newest `max_transcripts`), not
    just the running one, so a new session still shows what earlier conversations produced. Items
    of the current session first-seen elsewhere keep their oldest `via`=write if any."""
    import re
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    tr: list[Path] = []
    for acc in accounts.values():
        d = fleet_accounts.config_dir_of(acc) / "projects" / slug
        if d.is_dir():
            tr += list(d.glob("*.jsonl"))
    if session_id:
        for acc in accounts.values():
            f = find(acc, session_id)
            if f and f not in tr:
                tr.append(f)
    tr = sorted(tr, key=lambda p: p.stat().st_mtime, reverse=True)[:max_transcripts]
    import time
    merged: dict[str, dict] = {}
    t0 = time.monotonic()
    for i, f in enumerate(tr):
        cached = (_scan_cache.get(str(f)) or (None,))[0] == f.stat().st_mtime
        if not cached and i and time.monotonic() - t0 > 6:
            continue  # first open of a big project: the rest fills in on the next polls
        mine = bool(session_id) and f.stem == session_id
        for k, it in _scan(f, cwd, max_bytes if mine or i == 0 else 2_000_000, paste_dir).items():
            if k not in merged:
                merged[k] = it
            elif it["via"] == "write":
                merged[k] = {**merged[k], "via": "write"}
    return _finish(merged, limit)
