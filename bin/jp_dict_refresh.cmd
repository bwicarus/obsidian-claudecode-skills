@echo off
rem JP dictionary cache refresh: regenerate stale Japanese entries (katakana without source_word first).
rem Windows scheduled task "JP Dict Refresh" runs this nightly at 03:30. ASCII only: cmd.exe reads this
rem file in the OEM code page, so non-ASCII comments get mangled into stray commands.
set "CLAUDE_PROJECT=C:\tmp\reader-card-anchor-release"
set "OBSIDIAN_VAULT=C:\obsidian"
set "OBSIDIAN_VAULT_NAME=Obsidian Vault"
set "PY=C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%CLAUDE_PROJECT%\state\logs" mkdir "%CLAUDE_PROJECT%\state\logs"
set "LOGFILE=%CLAUDE_PROJECT%\state\logs\jp-dict-refresh.log"
rem ReaderPC not running (user quit / crashed) -> skip. Background tasks must not
rem keep loading the machine after the server app is closed (user 2026-09-08).
"%PY%" -X utf8 "%CLAUDE_PROJECT%\scripts\lib\readerpc_gate.py" --check >> "%LOGFILE%" 2>&1
if errorlevel 10 exit /b 0
echo [%date% %time%] jp-dict-refresh>> "%CLAUDE_PROJECT%\state\logs\jp-dict-refresh.log"
"%PY%" -X utf8 "%CLAUDE_PROJECT%\scripts\vocab\refresh_stale_jp_cache.py" --limit 200 --batch 8 >> "%CLAUDE_PROJECT%\state\logs\jp-dict-refresh.log" 2>&1
