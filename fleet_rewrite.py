"""Rewrite a rough prompt into a clear, structured one with a small model (Haiku) before it is sent
to a session. Runs `claude -p` headless on one of your signed-in accounts — no API key, no tools,
no project settings, nothing saved. Used by the web composer (✨) and `ccfleet rewrite "..."`."""
from __future__ import annotations

import subprocess
import tempfile

import ccfleet
import fleet_accounts

MAX_TEXT = 8000

SYSTEM = """You rewrite a prompt the user is about to send to a Claude Code session (a coding agent working inside a project).
Goal: a clear, complete prompt that is easy to get right the first time.

Rules:
- Keep the intent, the scope and every concrete detail (file names, commands, URLs, numbers, names). Do NOT invent requirements, technologies or facts.
- Keep the user's language. Fix spelling, unclear abbreviations and clumsy sentences.
- When the prompt is long enough, structure it (drop any section that has no information; never write "unknown"):
  **Goal:** one sentence — what must be achieved.
  **Context:** current state / the problem / why it is needed.
  **Tasks:** bullet points, one concrete step per line, in order.
  **Constraints:** what must not be done / must be kept.
  **Done when:** how to verify, what to report back.
- Where the user is vague about something the agent must decide, add a task "if X is unclear, ask before doing it" — do not guess.
- A short, already clear prompt (a single simple instruction) only gets its wording fixed, no forced structure. A command starting with "/" is returned verbatim.
- Be concise: no greetings, no explanations.

Return ONLY the rewritten prompt, nothing before or after it."""


def _account(cfg: dict) -> str:
    if cfg.get("rewrite_account") in cfg["accounts"]:
        return cfg["rewrite_account"]
    return fleet_accounts.first_signed_in(cfg["accounts"]) or next(iter(cfg["accounts"]))


def rewrite(text: str, cfg: dict | None = None) -> str:
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f"prompt is empty or longer than {MAX_TEXT} characters")
    if text.startswith("/"):
        return text
    cfg = cfg or ccfleet.load_config()
    cmd = [ccfleet.claude_exe(cfg), "-p", "--model", cfg.get("rewrite_model") or "haiku",
           "--system-prompt", SYSTEM, "--output-format", "text", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--tools", ""]
    with tempfile.TemporaryDirectory(prefix="ccfleet-rewrite-") as cwd:  # no project CLAUDE.md
        r = subprocess.run(cmd, input=text, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=120, cwd=cwd, env=ccfleet.account_env(cfg, _account(cfg)),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"Haiku did not answer: {(r.stderr or r.stdout or '').strip()[:300]}")
    return out
