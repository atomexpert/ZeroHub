@echo off
chcp 65001 >nul 2>&1
title Antigravity Hub — Настройка Docker-песочницы
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_sandbox_win.ps1" -UserHome "%USERPROFILE%"
exit /b %ERRORLEVEL%
