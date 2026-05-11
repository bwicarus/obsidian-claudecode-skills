@echo off
if "%~1"=="" (echo Usage: anki ^<notefile^> & exit /b 1)
set CLAUDE=C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe
"%CLAUDE%" --dangerously-skip-permissions -p "/anki C:\obsidian\%~1" -C "C:\claude"