@echo off
chcp 65001 >nul
title Antigravity Hub — Переустановка окружения (.venv)
cd /d "%~dp0"

echo ==============================================================
echo   Antigravity Hub — Восстановление Python-окружения (.venv)
echo   Ваша база чатов на D:\ (data\hub.db и .env) не затрагивается.
echo ==============================================================
echo.

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY312%" (
    for /f "delims=" %%p in ('where py 2^>nul') do set "PY_LAUNCHER=%%p"
)

if exist "%PY312%" (
    echo [1/3] Найден Python 3.12: %PY312%
    if exist ".venv" rmdir /s /q ".venv"
    "%PY312%" -m venv ".venv"
) else if defined PY_LAUNCHER (
    echo [1/3] Использую py -3.12...
    if exist ".venv" rmdir /s /q ".venv"
    py -3.12 -m venv ".venv"
) else (
    echo [ОШИБКА] Python 3.12 не найден!
    echo Установите Python 3.12 с python.org и запустите этот скрипт снова.
    pause
    exit /b 1
)

echo [2/3] Обновление pip и установка зависимостей...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [3/3] Проверка наличия официального Antigravity CLI (agy.exe)...
if not exist "%LOCALAPPDATA%\agy\bin\agy.exe" (
    echo Внимание: agy.exe не найден. При необходимости установите его командой в PowerShell:
    echo   irm https://antigravity.google/cli/install.ps1 ^| iex
) else (
    echo Официальный agy.exe найден: %LOCALAPPDATA%\agy\bin\agy.exe
)

echo.
echo Готово! Теперь можно запускать start_hub.bat или start_hub_silent.vbs.
pause
