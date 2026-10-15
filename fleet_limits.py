"""Per-session resource caps + host free-RAM check, dispatched by platform.

Windows: Job Objects (see fleet_limits_win) cap each session's memory/CPU, children included —
allocations beyond the cap fail.
macOS / Linux: there are no Job Objects. The memory cap is a best-effort WATCHDOG instead: every
supervisor tick sums the resident memory (ps RSS) of the session's whole process tree, and a
session over its `mem_gb` is killed and then restarted by its restart policy. There is no hard CPU
cap; a session with cpu_pct < 100 gets a lower scheduling priority (nice 10), inherited by its builds.

Public API is identical on every platform:
  free_ram_gb() -> float
  apply(pid, name, mem_gb, cpu_pct) -> str   # short description, '' when nothing was applied
  over_cap(pid, mem_gb) -> float | None      # tree RSS in GB when above the cap (POSIX watchdog)
  peak(pid) -> str                           # peak memory of a finished session, if known
  descendants(pid) -> list[int]              # child processes, recursively (POSIX; [] on Windows)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time


# ---------------------------------------------------------------- pure helpers (testable anywhere)
def parse_vm_stat(out: str) -> float:
    """macOS `vm_stat`: free + inactive + speculative + purgeable pages × page size, in GiB."""
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 4096
    pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(rf"^{label}:\s+(\d+)", out, re.M)
        if m:
            pages += int(m.group(1))
    return pages * page / 1024 ** 3


def parse_meminfo(text: str) -> float | None:
    """Linux /proc/meminfo MemAvailable in GiB."""
    m = re.search(r"^MemAvailable:\s+(\d+) kB", text, re.M)
    return int(m.group(1)) / 1024 ** 2 if m else None


def parse_ps(out: str) -> dict[int, tuple[int, int]]:
    """`ps -A -o pid=,ppid=,rss=` → {pid: (ppid, rss_kb)}."""
    table: dict[int, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]))
    return table


def tree_of(pid: int, table: dict[int, tuple[int, int]]) -> list[int]:
    """Descendants of pid (children, grandchildren…) in a ps table."""
    kids: dict[int, list[int]] = {}
    for p, (pp, _) in table.items():
        kids.setdefault(pp, []).append(p)
    out, todo, seen = [], [int(pid)], {int(pid)}
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                out.append(c)
                todo.append(c)
    return out


def tree_rss_gb(pid: int, table: dict[int, tuple[int, int]]) -> float:
    return sum(table.get(p, (0, 0))[1] for p in [int(pid)] + tree_of(pid, table)) / 1024 ** 2


# ---------------------------------------------------------------- platform dispatch
if sys.platform == "win32":
    from fleet_limits_win import apply, free_ram_gb, peak  # noqa: F401

    def over_cap(pid: int, mem_gb: float | None) -> float | None:
        return None  # the Job Object enforces the cap itself

    def descendants(pid: int) -> list[int]:
        return []  # taskkill /T walks the tree itself

else:
    def free_ram_gb() -> float:
        try:
            if sys.platform == "darwin":
                out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
                if "Pages free" in out:
                    return parse_vm_stat(out)
            elif os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
                    v = parse_meminfo(f.read())
                if v is not None:
                    return v
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
        except (ValueError, OSError, AttributeError, subprocess.SubprocessError):
            return 999.0  # unknown → never block launches on the RAM guard

    _table_cache: tuple[float, dict] = (0.0, {})

    def proc_table(max_age: float = 2.0) -> dict[int, tuple[int, int]]:
        """Every process (`ps`, same flags on macOS and Linux). Cached for a couple of seconds: one
        supervisor tick checks several sessions."""
        global _table_cache
        if time.time() - _table_cache[0] < max_age:
            return _table_cache[1]
        try:
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _table_cache = (time.time(), parse_ps(out))
        return _table_cache[1]

    def descendants(pid: int) -> list[int]:
        return tree_of(pid, proc_table(0))

    _peaks: dict[int, tuple[float, float | None, bool]] = {}  # pid -> (peak GB, cap, killed by cap)

    def over_cap(pid: int, mem_gb: float | None) -> float | None:
        """Watchdog: the session's tree RSS (GB) when it is above mem_gb, else None. Also remembers
        the peak, so the 'process gone' log line can say why it died."""
        if not pid:
            return None
        gb = tree_rss_gb(pid, proc_table())
        old = _peaks.get(int(pid), (0.0, None, False))
        hit = bool(mem_gb) and gb > float(mem_gb)
        _peaks[int(pid)] = (max(old[0], gb), mem_gb, old[2] or hit)
        return gb if hit else None

    def apply(pid: int, name: str, mem_gb: float | None, cpu_pct: float | None) -> str:
        parts = []
        if mem_gb:
            parts.append(f"mem<={mem_gb}GB (watchdog)")
        if cpu_pct and float(cpu_pct) < 100:
            try:
                os.setpriority(os.PRIO_PROCESS, int(pid), 10)
                parts.append("nice 10 (no hard CPU cap on this OS)")
            except (OSError, AttributeError):
                pass
        return " ".join(parts)

    def peak(pid: int) -> str:
        gb, cap, hit = _peaks.pop(int(pid), (0.0, None, False)) if pid else (0.0, None, False)
        if not gb:
            return ""
        return f"peak RAM {gb:.1f}" + (f"/{cap:g}" if cap else "") + " GB" + (" - HIT THE CAP (mem_gb)" if hit else "")
