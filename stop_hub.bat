@echo off
chcp 65001 >nul
echo Stopping Antigravity Hub (8765 / 8766 + SSH Tunnel)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop_hub.ps1" -RepositoryRoot "%~dp0."
exit /b %errorlevel%
