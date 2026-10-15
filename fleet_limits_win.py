"""Windows resource caps for ccfleet sessions (Job Objects). Imported only on win32 by fleet_limits.

Every session's claude.exe is put in its own Job Object; child processes (bun, vite, tsc,
powershell deploy scripts…) inherit it, so one session's build can't freeze the host:
- memory: JOB_OBJECT_LIMIT_JOB_MEMORY — allocations beyond the cap fail (the build errors out
  instead of paging the machine to death);
- CPU: hard cap as % of the whole machine (JobObjectCpuRateControlInformation).
Services started via NSSM/schtasks are not children, so production processes are unaffected.
The job outlives the supervisor (no KILL_ON_JOB_CLOSE): restarting ccfleet never kills sessions.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
CPU_RATE_ENABLE, CPU_RATE_HARD_CAP = 0x1, 0x4
PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class EXTENDED(ctypes.Structure):
    _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class CPU_RATE(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_ram_gb() -> float:
    m = MEMSTATUS()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 1024 ** 3


def apply(pid: int, name: str, mem_gb: float | None, cpu_pct: float | None) -> str:
    """Put pid in a named job with the given caps. Returns a short description ('' if no caps)."""
    if not mem_gb and not cpu_pct:
        return ""
    job = k32.CreateJobObjectW(None, f"ccfleet-{name}-{pid}")
    if not job:
        raise OSError(f"CreateJobObject failed: {ctypes.get_last_error()}")
    if mem_gb:
        ext = EXTENDED()
        ext.Basic.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
        ext.JobMemoryLimit = int(mem_gb * 1024 ** 3)
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(ext), ctypes.sizeof(ext)):
            raise OSError(f"memory limit failed: {ctypes.get_last_error()}")
    if cpu_pct:
        cr = CPU_RATE(CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, max(1, min(10000, int(cpu_pct * 100))))
        if not k32.SetInformationJobObject(job, JobObjectCpuRateControlInformation, ctypes.byref(cr), ctypes.sizeof(cr)):
            raise OSError(f"cpu cap failed: {ctypes.get_last_error()}")
    h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    ok = h and k32.AssignProcessToJobObject(job, h)
    err = ctypes.get_last_error()
    if h:
        k32.CloseHandle(h)
    if not ok:
        k32.CloseHandle(job)
        raise OSError(f"AssignProcessToJobObject failed: {err}")
    _jobs[pid] = (job, mem_gb)  # kept open so peak() can say how close the tree came to its cap
    return ((f"mem<={mem_gb}GB " if mem_gb else "") + (f"cpu<={cpu_pct}%" if cpu_pct else "")).strip()


_jobs: dict = {}


def peak(pid: int) -> str:
    """After a session died: the most memory its whole process tree ever committed vs its cap —
    'peak 9.9/10 GB' means the cap killed it (allocations fail at the cap, Claude Code then crashes)."""
    job, cap = _jobs.pop(pid, (None, None))
    if not job:
        return ""
    ext = EXTENDED()
    ok = k32.QueryInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(ext), ctypes.sizeof(ext), None)
    k32.CloseHandle(job)
    if not ok:
        return ""
    gb = ext.PeakJobMemoryUsed / 1024 ** 3
    hit = bool(cap) and gb >= cap * 0.97
    return f"peak RAM {gb:.1f}" + (f"/{cap:g}" if cap else "") + " GB" + (" - HIT THE CAP (mem_gb)" if hit else "")
