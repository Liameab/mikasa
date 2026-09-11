@echo off
title Mikasa Setup

rem ============================================================
rem  Mikasa installer - double-click this file to install.
rem
rem  All logic lives in install.ps1 next to this file; this
rem  wrapper only lifts the PowerShell execution policy for
rem  this one run (no system-wide change, no admin needed).
rem
rem  Keep this file PURE ASCII: it must survive GitHub preview
rem  and any text editor encoding (same rule as Mikasa.bat).
rem  The Chinese UI text lives in install.ps1 (UTF-8 with BOM).
rem
rem  Requires the "Mikasa" folder to sit NEXT TO this file -
rem  the installer refuses to run if it cannot find Mikasa.exe.
rem ============================================================

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo [failed] installation did not complete - see the message above.
)

echo.
pause
