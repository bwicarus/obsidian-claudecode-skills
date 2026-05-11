@echo off
set RELAY=C:\claude\launchers\dist\relay.exe
set TUNNEL_MATCH=-R 5001:127.0.0.1:5001 root@bwicarus.space
if "%~1"=="start"  goto start
if "%~1"=="stop"   goto stop
if "%~1"=="status" goto status
echo Usage: relay [start/stop/status]
exit /b 1

:start
PowerShell -NoProfile -Command "$m='%TUNNEL_MATCH%'; $p=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like ('*' + $m + '*') }; if ($p) { exit 0 } else { exit 1 }"
if not errorlevel 1 (echo [relay: already running] & exit /b 0)
PowerShell -NoProfile -Command "Start-Process -FilePath '%RELAY%' -WindowStyle Hidden"
echo [relay: started]
exit /b 0

:stop
PowerShell -NoProfile -Command "$m='%TUNNEL_MATCH%'; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like ('*' + $m + '*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
taskkill /f /im relay.exe >nul 2>&1
echo [relay: stopped]
exit /b 0

:status
PowerShell -NoProfile -Command "$m='%TUNNEL_MATCH%'; $p=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like ('*' + $m + '*') }; if ($p) { '[relay: running]' } else { '[relay: not running]' }"
exit /b 0
