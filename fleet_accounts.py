"""Claude account helpers for ccfleet (one CLAUDE_CONFIG_DIR per account).

- usage(acc): 5-hour / 7-day utilization (%) of a Claude subscription account, read with the
  account's own OAuth access token (from <config_dir>/.credentials.json) — the token is never logged.
- signed_in(acc): whether the account has a login on this machine.
- move_transcript(): copy a session transcript (+ its sidecar dir) from one account's config dir to
  another so `claude --resume <id>` continues the same conversation under the new account (used by
  the manual "switch account" action).
"""
from __future__ import annotations

import json
import sys
import shutil
import time
import urllib.request
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_SEC = 300
_cache: dict[str, tuple[float, dict | None]] = {}


def config_dir_of(acc: dict) -> Path:
    return Path(acc.get("config_dir") or Path.home() / ".claude")


def signed_in(acc: dict) -> bool:
    """This account has an OAuth login in its config dir (what `claude auth login` writes)."""
    f = config_dir_of(acc) / ".credentials.json"
    if not f.exists() and sys.platform == "darwin":
        return True  # macOS may keep it in the Keychain instead — can't tell, don't block
    try:
        cred = json.loads(f.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return False
    return bool(cred.get("refreshToken") or cred.get("accessToken"))


def has_transcript(acc: dict, session_id: str) -> bool:
    root = config_dir_of(acc) / "projects"
    return root.is_dir() and any(root.glob(f"*/{session_id}.jsonl"))


def usage(acc: dict) -> dict | None:
    """{'five_hour': %, 'seven_day': %, 'resets': iso, 'max': %} or None when unknown."""
    d = config_dir_of(acc)
    key = str(d)
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_SEC:
        return _cache[key][1]
    result = None
    try:
        cred = json.loads((d / ".credentials.json").read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        tok = cred.get("accessToken")
        # expired access token: the running CLI refreshes it; until then usage is unknown
        if tok and (cred.get("expiresAt") or 0) / 1000 > now:
            req = urllib.request.Request(USAGE_URL, headers={
                "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20", "User-Agent": "ccfleet/1"})
            body = json.loads(urllib.request.urlopen(req, timeout=15).read())
            fh = (body.get("five_hour") or {}).get("utilization") or 0.0
            sd = (body.get("seven_day") or {}).get("utilization") or 0.0
            result = {"five_hour": fh, "seven_day": sd, "max": max(fh, sd),
                      "resets": (body.get("five_hour") or {}).get("resets_at"),
                      "plan": cred.get("subscriptionType")}
    except Exception:
        result = None
    _cache[key] = (now, result)
    return result


def global_json(acc: dict) -> Path:
    """Claude Code's per-account settings/state file: ~/.claude.json for the default dir,
    <config_dir>/.claude.json otherwise."""
    return Path(acc["config_dir"]) / ".claude.json" if acc.get("config_dir") else Path.home() / ".claude.json"


# identity, credentials and per-account caches - never carried from one account to another
_NO_COPY = {"oauthAccount", "userID", "anonymousId", "primaryApiKey", "customApiKeyResponses",
            "s1mAccessCache", "hasAvailableSubscription", "subscriptionNoticeCount", "firstStartTime",
            "claudeCodeFirstTokenDate", "numStartups", "skillUsage", "feedbackSurveyState",
            "githubWebConnectionStatusCache", "promoStartupStatusCache", "penguinModeOrgEnabled",
            "fullscreenBootPending", "projects"}


def seed_config(src: Path, dst: Path) -> list[str]:
    """Give a fresh account the choices already made on the main one (theme, notices seen, MCP
    servers, trusted folders and their allowed tools…) so a session moved onto it does not stop at
    Claude Code's first-run dialogs. Only keys MISSING in dst are added; nothing is overwritten."""
    try:
        a = json.loads(src.read_text(encoding="utf-8"))
        b = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else {}
    except (OSError, ValueError):
        return []
    added = [k for k in a if k not in b and k not in _NO_COPY and not k.startswith("cached")]
    for k in added:
        b[k] = a[k]
    projects = b.setdefault("projects", {})
    for path, p in (a.get("projects") or {}).items():
        if p.get("hasTrustDialogAccepted") and not (projects.get(path) or {}).get("hasTrustDialogAccepted"):
            projects[path] = {**{k: v for k, v in p.items() if k in ("allowedTools", "hasTrustDialogAccepted",
                              "hasClaudeMdExternalIncludesApproved", "hasClaudeMdExternalIncludesWarningShown",
                              "enabledMcpjsonServers", "disabledMcpjsonServers", "mcpServers")},
                              **(projects.get(path) or {}), "hasTrustDialogAccepted": True}
            added.append(f"projects[{path}]")
    if added:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".ccfleet-tmp")
        tmp.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dst)
    return added


def ensure_onboarded(acc: dict) -> bool:
    """A signed-in account whose .claude.json lacks hasCompletedOnboarding makes Claude Code replay
    its first-run flow — ending on "Select login method" although the login is valid.
    Returns True when the flag was written."""
    if not signed_in(acc):
        return False
    f = global_json(acc)
    try:
        b = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return False
    if b.get("hasCompletedOnboarding"):
        return False
    b["hasCompletedOnboarding"] = True
    b.setdefault("theme", "dark")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".ccfleet-tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return True


def ack_bypass(acc: dict) -> bool:
    """settings.json skipDangerousModePermissionPrompt — what Claude Code itself writes after
    "Yes, I accept" on the Bypass Permissions warning. Returns True when written."""
    f = config_dir_of(acc).expanduser() / "settings.json"  # config_dir None = the default ~/.claude
    try:
        b = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(b, dict) or b.get("skipDangerousModePermissionPrompt"):
        return False
    b["skipDangerousModePermissionPrompt"] = True
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".ccfleet-tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return True


def first_signed_in(accounts: dict) -> str | None:
    """Name of the first account that is signed in on this machine (None when there is none)."""
    return next((n for n, a in accounts.items() if not a.get("disabled") and signed_in(a)), None)


def move_transcript(src_acc: dict, dst_acc: dict, session_id: str) -> bool:
    """Copy <src>/projects/<slug>/<sid>.jsonl (+ <sid>/ sidecar) into the same slug under dst."""
    src_root = config_dir_of(src_acc) / "projects"
    hits = list(src_root.glob(f"*/{session_id}.jsonl"))
    if not hits:
        return False
    f = max(hits, key=lambda p: p.stat().st_mtime)
    dst_dir = config_dir_of(dst_acc) / "projects" / f.parent.name
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f, dst_dir / f.name)
    side = f.parent / session_id
    if side.is_dir():
        shutil.copytree(side, dst_dir / session_id, dirs_exist_ok=True)
    return True
