@echo off
chcp 65001 >nul
title Antigravity Hub Portable Launcher
cd /d "%~dp0"

set "CUDA_VISIBLE_DEVICES="
set "ORT_DISABLE_ALL_GPU=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONIOENCODING=utf-8"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import uvicorn, fastapi, aiosqlite" >nul 2>&1
    if not errorlevel 1 goto :launch_hub
    echo [Antigravity Hub] Detected relocated or incomplete .venv - rebuilding portable environment...
    rmdir /s /q ".venv" >nul 2>&1
) else (
    echo [Antigravity Hub] Initializing portable .venv environment...
)

where python >nul 2>&1
if not errorlevel 1 (
    python -m venv ".venv"
) else (
    py -3 -m venv ".venv"
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python 3 interpreter not found in PATH. Install Python 3.11+ and retry.
    pause
    exit /b 1
)

echo [Antigravity Hub] Installing dependencies (0%% GPU mode)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.txt.
    pause
    exit /b 1
)

:launch_hub
if exist ".venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0start_hub_hidden.pyw" --open
) else (
    start "" "%~dp0.venv\Scripts\python.exe" "%~dp0start_hub_hidden.pyw" --open
)
exit /b 0
