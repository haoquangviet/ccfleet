"""Per-session resource caps + host free-RAM check, dispatched by platform.

Windows: Job Objects (see fleet_limits_win) cap each session's memory/CPU, children included.
Elsewhere caps are a no-op and free RAM is read from the OS, so the host RAM guard still works.
Public API is identical on every platform:
  free_ram_gb() -> float
  apply(pid, name, mem_gb, cpu_pct) -> str   # short description, '' when nothing was applied
  peak(pid) -> str                           # peak memory of a finished session's job, if known
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    from fleet_limits_win import apply, free_ram_gb, peak  # noqa: F401

else:  # POSIX fallback (the console backend itself is Windows-only)
    import os

    def free_ram_gb() -> float:
        try:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
        except (ValueError, OSError, AttributeError):
            return 999.0  # unknown → never block launches on the RAM guard

    def apply(pid: int, name: str, mem_gb: float | None, cpu_pct: float | None) -> str:
        return ""

    def peak(pid: int) -> str:
        return ""
