@echo off

title Mikasa Launcher

rem ============================================================
rem  Mikasa - double-click launcher (local profile, port 8787)
rem  Behavior: auto-start Ollama if not running (waits for it);
rem  if Mikasa is already up on 8787 -> just open the browser;
rem  otherwise start the service in a minimized window and open
rem  the browser once it is ready.
rem  Stopping: close the minimized "Mikasa service" window.
rem  Ollama window may be minimized as resident service - you can
rem  close it after Mikasa stops. Double-click again to restart.
rem  Probe /api/folders: only new builds expose this endpoint,
rem  a 404 means an old build is running (prevent false-open).
rem  This file is pure ASCII (no Chinese) so it survives GitHub
rem  preview and any text editor encoding. Keep it that way.
rem ============================================================

set "ROOT=%~dp0"

rem ------- auto-start Ollama (M4 moved models to D:\ollama\models) -------
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
    where ollama.exe >nul 2>nul
    if errorlevel 1 (
        echo [hint] ollama.exe not found in PATH - free chat unavailable.
    ) else (
        if not defined OLLAMA_MODELS set "OLLAMA_MODELS=D:\ollama\models"
        rem Context window: Ollama runtime default is only 2048 tokens while
        rem qwen3:8b supports 40960 - every RAG prompt (system + ~10 chunks +
        rem question ~= 7k tokens) was silently truncated at 2048, usually
        rem dropping tokens from the START (i.e. the system prompt with the
        rem citation/refusal rules). 16384 gives comfortable headroom.
        if not defined OLLAMA_CONTEXT_LENGTH set "OLLAMA_CONTEXT_LENGTH=16384"
        echo [start] launching Ollama model service...
        start "Ollama service" /min ollama.exe serve
    )
)

rem wait up to 20s for Ollama; failure does not block Mikasa startup
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 20;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:11434/'; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 1 }; if($ok){exit 0}else{exit 1}"
if errorlevel 1 echo [hint] Ollama not ready in 20s - free chat unavailable (kb unaffected)

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8787/api/folders'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"

if not errorlevel 1 (
    start "" "http://127.0.0.1:8787/"
    exit /b 0
)

echo [start] Mikasa service not running yet - starting it...
if exist "%ROOT%.venv\Scripts\python.exe" (
    start "Mikasa service" /min cmd /c "cd /d ""%ROOT%"" && "".venv\Scripts\python.exe"" -m mikasa serve --profile local --port 8787"
) else (
    start "Mikasa service" /min cmd /c "cd /d ""%ROOT%"" && mikasa serve --profile local --port 8787"
)

powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 60;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8787/api/folders'; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 1 }; if($ok){ Start-Process 'http://127.0.0.1:8787/' } else { Write-Host '[timeout] open http://127.0.0.1:8787/ manually - check the service window log'; Start-Sleep -Seconds 6 }"

exit /b 0
