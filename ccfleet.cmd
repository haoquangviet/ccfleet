@echo off
REM ccfleet shim: uses the repo's .venv when present, else python on PATH.
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0ccfleet.py" %*
) else (
  python "%~dp0ccfleet.py" %*
)
