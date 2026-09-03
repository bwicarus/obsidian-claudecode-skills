# Windows 服务器看护（计划任务 BwicarusServer 每 5 分钟跑一次）。
# 2026-09-03 实锤两种死法：① 04:04 重启后 HKCU Run 项没执行；② 托盘守护自己卡死（日志停在 17:07），
#   而计划任务原先直接以两个常驻 pythonw 作动作 —— 动作不退出 = 任务实例一直"运行中"，
#   MultipleInstances=IgnoreNew 让之后每次触发都被忽略，看护形同虚设（LastTaskResult 0x800710E0）。
# 现在任务只跑这个短脚本：检查 5000 端口与两个守护进程，缺谁拉谁，然后立刻退出。
#   守护自己卡死（进程在、端口不通、超过 3 分钟）→ 杀掉重拉；两个守护都有单实例锁，重复拉起会自行退出。
param([string]$Root = "C:\tmp\reader-card-anchor-release")
$ErrorActionPreference = "Continue"
$pyw = "C:\Users\bwica\AppData\Local\Programs\Python\Python313\pythonw.exe"
$log = Join-Path $Root "webapp-data\server-watchdog.log"
function Log($m) { Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 }

$sup = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like "*$Root\_server_deploy\local_supervisor.pyw*" }
$side = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like "*$Root\scripts\windows_sidecar_services.py*" }
$portOk = $false
try { $r = Invoke-WebRequest -Uri "http://127.0.0.1:5000/login" -UseBasicParsing -TimeoutSec 8; $portOk = ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) } catch { $portOk = $false }
# 守护心跳(local_supervisor.pyw 每 2 秒写一次):超过 3 分钟没动 = 守护卡死,直接杀掉重拉
$hbFile = Join-Path $Root "webapp-data\local_supervisor.heartbeat"
$hbStale = $false
if ($sup -and (Test-Path $hbFile)) {
  try {
    $hb = [int64](Get-Content $hbFile -Raw).Trim()
    $now = [int64][double]::Parse((Get-Date -UFormat %s))
    if (($now - $hb) -gt 180) { $hbStale = $true; Log "守护心跳已 $($now - $hb) 秒没动" }
  } catch {}
}
if ($sup -and $hbStale) {
  foreach ($p in $sup) { Log "杀掉心跳停止的守护 pid=$($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
  Start-Sleep 2
  $sup = $null
}

# 守护在、端口不通 → 记一次;连续两次（≥5 分钟）仍不通就当它卡死，杀掉重拉
$stateFile = Join-Path $Root "webapp-data\server-watchdog.state"
$strikes = 0
if (Test-Path $stateFile) { try { $strikes = [int](Get-Content $stateFile -Raw) } catch { $strikes = 0 } }
if ($sup -and -not $portOk) {
  $strikes += 1
  Set-Content -Path $stateFile -Value $strikes
  Log "守护在(pid=$($sup.ProcessId -join ','))但 5000 不通，第 $strikes 次"
  if ($strikes -ge 2) {
    foreach ($p in $sup) { Log "杀掉疑似卡死的守护 pid=$($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match 'pythonw?\.exe"? app\.py$' } |
      ForEach-Object { Log "杀掉残留 Flask pid=$($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    Start-Sleep 2
    $sup = $null
    Set-Content -Path $stateFile -Value 0
  }
} else {
  if (Test-Path $stateFile) { Set-Content -Path $stateFile -Value 0 }
}
if (-not $sup) {
  Log "拉起托盘守护"
  Start-Process -FilePath $pyw -ArgumentList "`"$Root\_server_deploy\local_supervisor.pyw`"" -WorkingDirectory "$Root\_server_deploy" -WindowStyle Hidden
}
if (-not $side) {
  Log "拉起 sidecar 守护"
  Start-Process -FilePath $pyw -ArgumentList "`"$Root\scripts\windows_sidecar_services.py`"" -WorkingDirectory $Root -WindowStyle Hidden
}
exit 0
