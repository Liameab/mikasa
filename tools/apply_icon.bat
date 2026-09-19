@echo off
rem ============================================================
rem  Double-click launcher for apply_icon.ps1.
rem  Keeps this file pure ASCII (survives any editor / encoding).
rem ============================================================
setlocal
set "PS1=%~dp0apply_icon.ps1"

where pwsh.exe >nul 2>nul
if not errorlevel 1 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
)

echo.
pause
