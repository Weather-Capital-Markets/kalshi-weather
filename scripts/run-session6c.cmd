@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-session6c.ps1"
exit /b %ERRORLEVEL%
