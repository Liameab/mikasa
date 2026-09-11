@echo off
title Mikasa Uninstall

rem ============================================================
rem  Mikasa uninstaller - double-click this file to uninstall.
rem
rem  This file is copied into the install folder by install.ps1,
rem  so the user can uninstall from there as well as from
rem  Windows "Apps & features". All logic is in uninstall.ps1.
rem
rem  Keep this file PURE ASCII (same rule as Mikasa.bat).
rem  Your data lives in %LOCALAPPDATA%\Mikasa and is NOT removed.
rem ============================================================

setlocal
rem  IMPORTANT: do NOT cd into this script's own directory. Windows refuses
rem  to delete a directory that is any process's current working directory,
rem  so an uninstaller running from inside the install folder cannot remove
rem  that folder (it would leave an empty shell behind and still report
rem  success). %~dp0 is used explicitly below, so the CWD is free to leave.
cd /d "%TEMP%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
if errorlevel 1 (
    echo.
    echo [failed] uninstall did not complete - see the message above.
)

echo.
pause
