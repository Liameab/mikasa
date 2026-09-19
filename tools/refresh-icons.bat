@echo off
rem ============================================================
rem  Rebuild the Windows icon cache -- double-click me (or run in a terminal).
rem
rem  Named with a HYPHEN, not an underscore, on purpose: the chat/editor
rem  pipeline escapes "_" in pasted text, so "refresh_icons.bat" arrived in
rem  the terminal as "refresh\_icons.bat" and failed. Keep this name hyphenated.
rem
rem  WHY THIS FILE IS PURE ASCII: a previous version of it printed
rem  Chinese after a `chcp 65001`. Changing the code page in the
rem  middle of a batch file is a known cmd.exe bug -- it keeps
rem  reading the file by byte offset, so the multi-byte characters
rem  shifted the line boundaries and every following line came out
rem  mangled ("rem" -> "RE", "echo" -> "cho", ...). Same reason
rem  Mikasa.bat at the repo root is ASCII-only. Do NOT put Chinese
rem  in this file, and do NOT add chcp here.
rem
rem  WHY IT IS A PURE .BAT (no .ps1 sidecar): on this machine .ps1 is
rem  associated with PyCharm, so double-clicking one opens the IDE
rem  instead of running it (it surfaced a PyCharm lock error once).
rem  A .bat is always executed.
rem
rem  WHY IT IS NEEDED AT ALL: Explorer caches icons by path+mtime in
rem  iconcache_*.db and does not reliably invalidate them when the
rem  target exe or the shortcut changes; `ie4uinit -show` alone was
rem  not enough after the Mikasa icon swap (2026-09-19).
rem
rem  The four steps are the standard "icon cache fix": notify the
rem  shell -> stop Explorer -> drop the cache DBs -> start Explorer.
rem  Those .db files are pure cache and Windows rebuilds them at
rem  once. Only side effect: taskbar and open folder windows blink.
rem ============================================================
setlocal
title Mikasa - rebuild icon cache

echo.
echo   Rebuilding the Windows icon cache.
echo   (use this when the desktop still shows the OLD icon after an icon swap)
echo.

echo   [1/4] Tell the shell that icons changed ...
ie4uinit.exe -ClearIconCache >nul 2>nul
ie4uinit.exe -show >nul 2>nul

echo   [2/4] Stopping Explorer (the taskbar will blink, that is normal) ...
taskkill /F /IM explorer.exe >nul 2>nul
timeout /t 2 /nobreak >nul

echo   [3/4] Deleting the icon cache DBs (Windows rebuilds them) ...
del /F /Q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db" >nul 2>nul

echo   [4/4] Starting Explorer again ...
start "" explorer.exe

echo.
echo   Done. The desktop icon should be the new one now.
echo.
pause
