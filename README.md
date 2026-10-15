# ccfleet

**A session manager for Claude Code — run and manage multiple Claude Code sessions on Windows, with a mobile web UI.**

Tiếng Việt: [README.vi.md](README.vi.md)

**ccfleet** is a small supervisor and session manager for
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) on Windows. It runs **multiple Claude Code
sessions** at once, each in its own console with Remote Control turned on. It restarts sessions when
they die (auto-restart with backoff) and resumes the same conversation after a reboot. You can drive
everything from a terminal menu or from a **web UI** that works on your **phone** and on the desktop.
It also handles **multiple Claude accounts**: you can sign them in from the web, pick an account for
each session, and see each account's usage.

It is plain Python (standard library plus Pillow) and needs no VS Code.

> Not affiliated with Anthropic. Claude and Claude Code are trademarks of Anthropic.

---

## Why ccfleet

| Pain point | With ccfleet |
|---|---|
| Remote control from the Claude app only continues a session that is already open; you cannot start a new one from your phone | Create, start, stop and restart any session from the web UI, on any project, from your phone |
| Claude Code in a terminal or in VS Code means one heavy window per session that you have to keep open and babysit | Sessions run in the background, each in its own console, with per-session RAM/CPU caps; no IDE needed |
| A crash, an update or a reboot kills the session and the conversation is gone until you reopen it by hand | Auto-restart with backoff and `--resume` of the same conversation, also after a reboot |
| Several Claude accounts mean logging in and out in each terminal | Sign accounts in from the web, see 5h / 7-day usage, choose the account per session |

## Features

**Supervisor (`ccfleet run`)**
- Runs each session in its own (minimized or hidden) console with `claude --remote-control <name>`, so you can also open it from claude.ai or the Claude mobile app.
- Restarts a session automatically when it exits. Restart policy is `always`, `on-failure` or `never`, with a backoff: 5 restarts in 10 minutes pauses the session for 10 minutes.
- Restarts use `--resume <session-id>`, so a session continues the same conversation after a crash or reboot.
- Never starts a second copy of a session that is already open elsewhere, for example in VS Code or a terminal. `takeover` hands that session over to the supervisor.
- Accepts the "Do you trust this folder?" prompt automatically. Records the Remote Control URL. Flags usage-limit banners.
- Durable schedules: sends a prompt at fixed minutes or every N minutes, only while the session is idle. Schedules survive restarts, unlike in-session cron.
- Runs `claude update` every night inside a configurable time window, only when no session is working. Idle sessions then move onto the new binary one at a time.
- Host RAM guard: when free RAM is below a threshold, no new launches start and scheduled prompts wait.
- Per-session RAM and CPU caps through **Windows Job Objects**. Child processes (builds, test runners) inherit the caps, so one runaway build can't freeze the machine.

**Terminal**
- Plain `ccfleet` opens an interactive menu: a numbered session list, then one key per action (peek, send, start, stop, restart, keys).
- `ccfleet send <session> "..." --clip --rewrite` adds what's on the clipboard (text, screenshots, copied files) and can tidy the prompt with Haiku first.
- `ccfleet tell <session> "..."` sends a message from one session to another. It is typed in once the target session is idle.

**Web UI (phone + desktop)**
- Live chat view built from the session transcripts. While Claude works you see its status line ("Refactoring… 1m 12s · 3.4k tokens").
- Claude Code's interactive menus (questions, permission prompts, `/model`…) appear as tap-able buttons.
- Quick keys (Esc, arrows, Enter, Shift+Tab, Ctrl+C) and common slash commands. A raw screen tab shows the session's console.
- Paste screenshots into the prompt box (saved as PNG; the file path goes into the prompt). A ✨ button rewrites the prompt with Haiku.
- Each session has a file pane listing the files it wrote, used or was sent. There is also a project file manager for browsing, uploading, creating folders and deleting files; secret-looking files are never served.
- Projects are discovered from your Claude Code history. From there you can reopen any past conversation or start a new one. You can also create a new project (empty folder with `git init`, or a `git clone`).
- **Multi-account:** sign in a new Claude account from the web, choose an account per session, or switch a running session to another account (its transcript is copied and the session resumes there). The UI shows 5-hour and 7-day usage for each account.
- Shared skills library: collects skills from every account and project, then assigns them to projects as directory junctions. Those links are kept out of git through `.git/info/exclude`.

**Languages**
- The web UI and the terminal menu are available in **English and Vietnamese**. See [Language](#language).

**Extras**
- Image shrink hook: a `PreToolUse(Read)` hook downscales big screenshots before Claude reads them, which saves image tokens.

## Requirements

- Windows 10/11 or Windows Server
- Python 3.11+ and [Pillow](https://pypi.org/project/pillow/)
- Claude Code CLI installed (`claude` on PATH or the npm global install) and signed in
- A Claude subscription (Pro / Max) for each account you add

## Quick start

```powershell
git clone https://github.com/<you>/ccfleet.git
cd ccfleet
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# register a session (a project folder Claude Code should work in)
.venv\Scripts\python ccfleet.py add my-app --cwd C:\Projects\my-app

# run the supervisor in the foreground...
.venv\Scripts\python ccfleet.py run
# ...or install it as a scheduled task (ccfleet-supervisor): starts at boot, kept alive
.venv\Scripts\python ccfleet.py install

# optional: a short `ccfleet` command on your PATH
.venv\Scripts\python ccfleet.py shortcut
```

Turn on the web UI:

```powershell
ccfleet web enable      # sets "web": {"enabled": true} in config.json
# restart the supervisor (ccfleet uninstall && ccfleet install, or stop and start `ccfleet run`)
ccfleet web open        # one-time 2-minute login link, for use on this machine
ccfleet web setup       # one-time link to set a password + TOTP (needed for remote access)
```

There is no fixed port. The first time the web UI is used, ccfleet picks a random free port between 20000 and 60000 on 127.0.0.1, saves it as `web.port` in `config.json` and prints it once. `ccfleet web port` prints the current port, and `ccfleet web port N` sets a different one (you can also edit `config.json`). The UI is then at `http://127.0.0.1:<port>`.

> The random port is **not** a security measure. The protection comes from the loopback bind, TLS in front, and password + TOTP.

To use it from your phone or from outside, see [Remote access](#remote-access-from-your-phone--outside).

## Demo

```powershell
ccfleet demo            # prints http://127.0.0.1:<random-port>   (--port N to choose, --lang vi for Vietnamese)
```

Demo mode serves the web UI with **fake data**: several sessions (one busy, one waiting on a menu, one stopped), chat history, files and two accounts with usage bars. It needs no Claude install, does no authentication and touches nothing in your real data dir. It is meant for screenshots and for trying the UI. It only binds to loopback and refuses any other host.

## CLI reference

| Command | What it does |
|---|---|
| `ccfleet` / `ccfleet menu` | Interactive session manager (for humans; waits on stdin) |
| `ccfleet run` | Supervisor loop (what the scheduled task runs) |
| `ccfleet status` | Supervisor + every session: running/busy/stopped, pid, session id, Remote Control URL |
| `ccfleet list` | Configured sessions |
| `ccfleet add <name> --cwd <dir> [--account a] [--session-id id] [--effort …] [--model …] [--boot-prompt …]` | Add a session (or adopt an existing conversation by id) |
| `ccfleet start / stop / restart / remove <name>` | Start (un-stop), stop and keep stopped, kill + start, stop + delete |
| `ccfleet takeover <name>` | Hand over a session you just closed in VS Code / a terminal |
| `ccfleet peek <name> [--lines N]` | Print a session's screen text |
| `ccfleet send <name> "..." [--clip] [--rewrite]` | Type a prompt into a running session |
| `ccfleet rewrite "..." [--clip]` | Rewrite a rough prompt clearly with Haiku |
| `ccfleet tell <session> "..."` / `ccfleet tell --list` | Message another session (delivered when it is idle) / pending messages |
| `ccfleet usage` | 5h / 7d usage per account |
| `ccfleet new <name> <path> [--clone URL] [--prompt "..."]` | Create a project folder (or clone) + its session |
| `ccfleet projects` | Projects found in Claude Code history (all accounts) |
| `ccfleet account list \| add <name> \| login <name> \| remove <name>` | Manage Claude accounts (one `CLAUDE_CONFIG_DIR` each) |
| `ccfleet web open \| setup \| enable \| disable` | Web UI: local login link, password + TOTP setup, on/off |
| `ccfleet web port [N]` | Print the web port, or set it to N |
| `ccfleet install` / `ccfleet uninstall` | Register / remove the `ccfleet-supervisor` scheduled task |
| `ccfleet shortcut` | Create a `ccfleet` command (`ccfleet.cmd` + a Git Bash twin) on your PATH |
| `ccfleet skill-install` | Install the bundled `ccfleet-sessions` skill into every account |
| `ccfleet demo [--port N] [--lang en\|vi]` | Demo web UI with fake data (loopback only) |

## Configuration

Everything lives in the data dir: `%CCFLEET_HOME%`, default `~/.ccfleet`.

| File | Contents |
|---|---|
| `config.json` | accounts, sessions, defaults, schedules, web settings (no secrets) |
| `state.json` | runtime state: pid, resolved session id, restart history |
| `fleet.log` | supervisor log (trimmed automatically) |
| `web-auth.json`, `web-sessions.json`, `web-audit.log` | web login (hashed), cookie sessions (hashed), audit trail |

Example `config.json`:

```json
{
  "host": { "min_free_ram_gb": 1.5, "max_busy_sessions": 3 },
  "claude_exe": "",
  "defaults": {
    "permission_mode": "bypassPermissions",
    "remote_control": true,
    "window": "minimized",
    "restart": "always",
    "effort": null,
    "model": null,
    "mem_gb": 8,
    "cpu_pct": 50,
    "boot_prompt": null,
    "append_system_prompt": "Read files in chunks, tail logs, keep replies short."
  },
  "accounts": {
    "main": { "config_dir": null },
    "work": { "config_dir": "C:\\Users\\me\\.claude-work" }
  },
  "sessions": [
    { "name": "my-app", "account": "main", "cwd": "C:\\Projects\\my-app", "enabled": true },
    { "name": "docs", "account": "work", "cwd": "C:\\Projects\\docs", "effort": "low", "restart": "on-failure" }
  ],
  "schedules": [
    { "session": "my-app", "minutes": [17, 47], "prompt": "Continue with the next item in plans/", "only_idle": true },
    { "session": "docs", "every_min": 30, "prompt": "Check the build and fix any broken links." }
  ],
  "auto_update": { "enabled": true, "window": [2, 5] },
  "web": { "enabled": true, "port": 34567, "host": "127.0.0.1", "base_url": "https://fleet.example.com" }
}
```

- `host.min_free_ram_gb`: below this, nothing new launches and scheduled prompts wait.
- `host.max_busy_sessions`: scheduled prompts wait while this many sessions are working.
- Any `defaults` key can be overridden per session.
- `config_dir: null` means the default `~/.claude`. Each other account has its own `CLAUDE_CONFIG_DIR`.
- `auto_update.window` is the hour range (local time) in which `claude update` may run.

## Language

ccfleet ships in English and Vietnamese.

- **Web UI:** the language follows your browser (`vi*` → Vietnamese, anything else → English). Use the **EN / VI** switcher to change it; your choice is remembered in that browser.
- **CLI / terminal menu:** set `"lang": "vi"` or `"lang": "en"` in `config.json`. Without that setting, the `LANG` environment variable decides (`vi*` → Vietnamese). The default is English.
- **Demo:** `ccfleet demo --lang vi` or `--lang en` picks the language of the demo data.

## Image shrink hook

Image tokens grow with pixel count, roughly width × height / 750. A phone screenshot can cost thousands of tokens. `image_shrink_hook.py` writes a downscaled copy of any image over the budget and points the `Read` at it. Register it in `~/.claude/settings.json` (and in each account's `settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Read",
        "hooks": [{ "type": "command",
                    "command": "C:\\path\\to\\ccfleet\\.venv\\Scripts\\python.exe C:\\path\\to\\ccfleet\\image_shrink_hook.py" }] }
    ]
  }
}
```

Environment knobs: `CCFLEET_IMG_MAX_MP` (default `0.75` megapixels), `CCFLEET_IMG_MAX_EDGE` (default `1568` px), `CCFLEET_IMG_CACHE` (cache dir).

## Security

> [!IMPORTANT]
> Sessions run with `bypassPermissions` by default. Anyone who can use the web UI can make Claude run any command on this machine, so **the web UI is equivalent to a shell.**

- The web server binds to **127.0.0.1** by default. **Never expose or port-forward the raw port.** The random port is not a security measure.
- For remote access, use a tunnel or a **TLS reverse proxy** and keep the loopback bind (see [Remote access](#remote-access-from-your-phone--outside)).
- Login requires a **password (PBKDF2-SHA256) and a TOTP code**. This can't be turned off; only the loopback-only demo has no login. Repeated failures lock the client IP out for 15 minutes.
- Cookie sessions are random tokens, stored only as hashes (HttpOnly, SameSite=Strict, 12 h). The cookie gets the Secure flag when the request came over HTTPS through the proxy. To sign out every device, delete `web-sessions.json` and restart.
- Every state-changing request must carry the CSRF header `X-Fleet: 1`.
- Actions are written to `web-audit.log`. OAuth codes and prompt text are never logged.
- The file manager only serves known project roots, blocks path traversal, and never serves or overwrites secret-looking files (`.env*`, keys, credentials).
- Demo mode (`ccfleet demo`) has no authentication, so it refuses to bind anything other than loopback.

## Remote access (from your phone / outside)

ccfleet always listens on loopback only. To reach it from your phone or another network, put something in front of it that adds TLS:

- **Cloudflare Tunnel** (recommended): no open ports on your machine, and you can add Cloudflare Access as an extra login layer.
- **Tailscale** (`tailscale serve`): private to the devices on your tailnet.
- **Reverse proxy with TLS**: Caddy (automatic HTTPS) or nginx + certbot (see [`deploy/nginx.example.conf`](deploy/nginx.example.conf)).

Step-by-step guide, including proxy headers, `web.base_url`, running as a Windows service and a security checklist: **[docs/remote-access.md](docs/remote-access.md)** (Tiếng Việt: [docs/remote-access.vi.md](docs/remote-access.vi.md)).

## How it works

- **Console attach:** each session is a real `claude` process in its own Windows console. The supervisor attaches to that console to read the screen (`peek`) and to inject key events (`type`/`keys`).
- **Transcripts:** the chat view and file pane read Claude Code's own transcripts in `<config dir>/projects/*/<session-id>.jsonl`. Only the tail of each file is read, so large histories stay cheap.
- **Enter is sent separately.** Claude Code treats fast input as a paste and swallows an Enter that arrives inside it. Prompt text is typed first, then Enter follows after a short pause.
- **Busy detection** uses the spinner and status line on screen. Schedules and messages wait for an idle session, so they never interrupt a running turn.

## Limitations

- The console backend is **Windows-only** (Win32 console APIs and Job Objects).
- When the supervisor runs as a scheduled task (S4U), it runs in **session 0**. A shell in your RDP or desktop session can't attach to those consoles, so `peek` and `send` fall back to the local web API.
- Claude Code draws long pickers (e.g. `/model`) in a small scrolling viewport. The web UI shows only what is drawn and offers ↑/↓ to scroll.
- Usage numbers come from the account's OAuth token and are unavailable while that token is expired.

## ccfleet Pro / Premium

Need more? A commercial edition adds:

- **Multi-machine hub + nodes:** drive sessions on many PCs and Macs from one web UI.
- **Read-only share links:** protected by a PIN, with viewer comments.
- **MCP server:** control your sessions from claude.ai or other MCP clients.
- **Per-project task queue:** tasks are dispatched automatically.
- **Session recovery:** recover sessions from an old or dead machine's disk.

Contact: hqv@haoquangviet.com

## License

[MIT](LICENSE)

Not affiliated with Anthropic. Claude and Claude Code are trademarks of Anthropic.
