@echo off
rem KJ knowledge nodes: ingest the bridge card-binding ledger + pull Anki review snapshots into mastery.
rem Windows scheduled task "KJ Anki Sync" runs this every 15 minutes. ASCII only: cmd.exe reads this
rem file in the OEM code page, so non-ASCII comments get mangled into stray commands.
set "CLAUDE_PROJECT=C:\tmp\reader-card-anchor-release"
set "OBSIDIAN_VAULT=C:\obsidian"
set "OBSIDIAN_VAULT_NAME=Obsidian Vault"
set "PY=C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%CLAUDE_PROJECT%\state\kj" mkdir "%CLAUDE_PROJECT%\state\kj"
set "LOGFILE=%CLAUDE_PROJECT%\state\kj\anki-sync.log"
rem ReaderPC not running (user quit / crashed) -> skip. Background tasks must not
rem keep loading the machine after the server app is closed (user 2026-09-08).
"%PY%" -X utf8 "%CLAUDE_PROJECT%\scripts\lib
"%PY%" -X utf8 "%CLAUDE_PROJECT%\scripts\lib\readerpc_gate.py" --check >> "%LOGFILE%" 2>&1
if errorlevel 10 exit /b 0
echo [%date% %time%] anki-sync>> "%CLAUDE_PROJECT%\state\kj\anki-sync.log"
"%PY%" "%CLAUDE_PROJECT%\scripts\kj\cli.py" anki-sync >> "%CLAUDE_PROJECT%\state\kj\anki-sync.log" 2>&1
