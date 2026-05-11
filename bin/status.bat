@echo off
echo === System Status ===

echo.
echo [relay]
call C:\claude\bin\relay.bat status

echo.
echo [Obsidian]
tasklist /fi "imagename eq Obsidian.exe" 2>nul | findstr /i "Obsidian.exe" >nul
if not errorlevel 1 (echo   running) else (echo   stopped)

echo.
echo [Anki]
tasklist /fi "imagename eq anki.exe" 2>nul | findstr /i "anki.exe" >nul
if not errorlevel 1 (echo   running) else (echo   stopped)
