@echo off
rem Double-click to install Claude Pocket for this Windows user (see README.md).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
