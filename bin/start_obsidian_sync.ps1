# Obsidian Headless Sync launcher
# Runs node + cli.js in foreground (this shell waits for it to exit).
# Combined with the scheduled task's "restart on failure" setting, this gives us crash-restart.
# Scheduled task should invoke this with -WindowStyle Hidden so no console flashes.

$ErrorActionPreference = 'Stop'
$logDir = 'C:\claude\state\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$out = Join-Path $logDir 'obsidian_sync.log'

if ((Test-Path $out) -and ((Get-Item $out).Length -gt 5MB)) {
    $bak = "$out.1"
    Remove-Item $bak -ErrorAction SilentlyContinue
    Move-Item $out $bak
}

$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCmd) { throw "node not found in PATH" }
$cli = 'C:\Users\bwica\AppData\Roaming\npm\node_modules\obsidian-headless\cli.js'
if (-not (Test-Path $cli)) { throw "cli.js not found: $cli" }

Set-Location 'C:\obsidian'
"[$(Get-Date -Format 'o')] launcher starting node $cli sync --continuous" | Out-File -FilePath $out -Append -Encoding UTF8

# Run synchronously — this script blocks until node exits, so the scheduled task can detect failure and restart.
# ⚠ 2026-09-03 实锤:上面 $ErrorActionPreference='Stop' 时,Windows PowerShell 5.1 会把原生程序写到 stderr 的
#   任何一行(重定向到文件时)当成终止错误 —— 脚本在这里被掐断,下面那行"node exited"从来没写过,
#   node 随宿主一起被计划任务收掉,看护每 5 分钟重拉一次 → "持续同步"实际退化成"每 5 分钟同步一次"。
$ErrorActionPreference = 'Continue'
& $nodeCmd.Source $cli sync --continuous *>> $out
$rc = $LASTEXITCODE
"[$(Get-Date -Format 'o')] node exited rc=$rc" | Out-File -FilePath $out -Append -Encoding UTF8
exit $rc
