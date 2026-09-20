@echo off
rem ============================================================
rem  Rebuild the packaged app (dist\Mikasa) from the current tree.
rem
rem  WHY THIS FILE EXISTS: the equivalent one-liner
rem      .venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\Mikasa.spec
rem  has two traps -- it only works when the current directory is the repo
rem  root (otherwise ".venv" is not found), and PyInstaller prints nothing
rem  for long stretches, so it looks like it hung. This file cds to the repo
rem  root itself, says what it is doing, and waits at the end so the window
rem  does not vanish before you can read the result.
rem
rem  WHY --clean IS NOT OPTIONAL: packaging/Mikasa.spec caches the EXE-level
rem  intermediates. After an icon or resource change, skipping the clean gives
rem  you "new timestamp, old icon" -- a build that looks fresh and is not.
rem
rem  Next step after this finishes: double-click tools\apply_icon.bat
rem  (that copies dist\Mikasa over the installed app the desktop shortcut
rem  points at, and refreshes the shortcuts' icons).
rem
rem  PURE ASCII on purpose -- see the note in refresh-icons.bat about chcp.
rem ============================================================
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [x] Not found: %CD%\%PY%
    echo     Run this file from inside the mikasa repo, or activate the venv first.
    pause
    exit /b 1
)

if /i "%~1"=="-DryRun" (
    echo repo root : %CD%
    echo would run : "%PY%" -m PyInstaller --clean --noconfirm packaging\Mikasa.spec
    exit /b 0
)

echo.
echo   Rebuilding Mikasa from %CD%
echo   This takes 1-2 minutes. It prints little along the way -- that is normal.
echo.

"%PY%" -m PyInstaller --clean --noconfirm packaging\Mikasa.spec
if errorlevel 1 (
    echo.
    echo   [x] Build FAILED -- scroll up for the last ERROR line.
    pause
    exit /b 1
)

echo.
echo   Done: dist\Mikasa\Mikasa.exe is rebuilt.
echo   Next: double-click tools\apply_icon.bat to put it on your desktop app.
echo.
pause
