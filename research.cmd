@echo off
rem Convenience wrapper so you can run the CLI without `pip install -e .`.
rem Anchors to the repo root, then runs the CLI with the PROJECT venv's Python.
rem The global Python lacks the CLI deps (rich/httpx), so bare `python` crashes
rem at startup ("the window just closes"). The venv is the only interpreter that
rem has them.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [research] .venv not found at "%~dp0.venv\".
    echo Create it and install the project, e.g.:
    echo     python -m venv .venv ^&^& .venv\Scripts\pip install -e .
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m research_assistant.cli %*

rem If it errored AND we were double-clicked (no args -> interactive REPL), hold
rem the window open so the message is readable instead of flashing closed.
if errorlevel 1 if "%~1"=="" pause
