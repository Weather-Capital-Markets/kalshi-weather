@echo off
REM Session 2.5 runner — works when PowerShell script execution is disabled.
REM Usage from repo root:  scripts\run-session25.cmd [asos|clockb|window|all]
setlocal
set "STEP=%~1"
if "%STEP%"=="" set "STEP=all"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-session25.ps1" -Step %STEP%
exit /b %ERRORLEVEL%
