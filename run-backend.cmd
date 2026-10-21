@echo off
rem Starts the HRMS backend for local development. The steps are in run-backend.ps1; this
rem wrapper lets the script be double-clicked or run from cmd. Arguments are passed through,
rem for example: run-backend.cmd -Database hrms_local_test
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-backend.ps1" %*
exit /b %errorlevel%
