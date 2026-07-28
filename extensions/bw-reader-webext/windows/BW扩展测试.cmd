@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%F in ("%SCRIPT_DIR%*.ps1") do set "TEST_SCRIPT=%%~fF"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TEST_SCRIPT%"
if errorlevel 1 pause
