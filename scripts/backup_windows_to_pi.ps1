# Windows → Pi 手动备份（用户 2026-09-02 拍板："pi 的本地备份应该是手动从 Windows 上进行的"）。
# Windows 是唯一服务器；Pi 只收这份快照，不再被任何客户端直接写。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\backup_windows_to_pi.ps1 [-Root C:\tmp\reader-card-anchor-release]
# 需要：ssh 别名 `pi`（~/.ssh/config），Pi 上存在 ~/backups/windows-server/。
param(
  [string]$Root = "C:\tmp\reader-card-anchor-release",
  [string]$PiTarget = "pi:~/backups/windows-server"
)
$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$items = @(
  @{ Src = Join-Path $Root "webapp-data"; Name = "webapp-data" },
  @{ Src = Join-Path $Root "state";       Name = "state" },
  @{ Src = Join-Path $env:LOCALAPPDATA "BWReader"; Name = "bwreader-localappdata" }   # 书库/词组/词典留底/预处理缓存
)
# 排除可重建的大缓存（与盘点文档第二节 C 类一致）
$exclude = @("pdf-page-img", "pdf-page-backups", "backup-pdfs", "book-preprocess", "model3d",
             "google-vision-ocr", "web-rescache", "epub-extract", "pdf-compressed", "logs", "pc-ocr-cache", "models",
             # BWReader 本地目录里可重建/可重装的大块(首跑实测 8.5G,其中 venv 5.5G、历次 ReaderPC 发布 3.3G)
             "reader-pc-ocr-venv", "ReaderPC-Server", "garbage-assistant")
ssh pi "mkdir -p ~/backups/windows-server" | Out-Null
foreach ($item in $items) {
  if (-not (Test-Path $item.Src)) { Write-Host "[skip] $($item.Src) 不存在"; continue }
  $tar = Join-Path $env:TEMP "bw-backup-$($item.Name)-$stamp.tgz"
  $exArgs = @($exclude | ForEach-Object { "--exclude=$_" })
  Write-Host "[pack] $($item.Name) → $tar"
  $tarExe = Join-Path (Join-Path $env:SystemRoot 'System32') 'tar.exe'   # 系统 bsdtar,别被 PATH 里的 Git tar 抢走
  $parent = Split-Path $item.Src
  $leaf = Split-Path $item.Src -Leaf
  & $tarExe -czf $tar @exArgs -C $parent $leaf 2>&1 | ForEach-Object { Write-Host "  tar: $_" }
  if (-not (Test-Path $tar)) { Write-Host "[fail] $($item.Name) 打包失败(exit=$LASTEXITCODE),跳过"; continue }
  Write-Host "[send] $tar ($([math]::Round((Get-Item $tar).Length/1MB,1)) MB) → $PiTarget/"
  & scp -q $tar "$PiTarget/"
  if ($LASTEXITCODE -ne 0) { Write-Host "[fail] scp exit=$LASTEXITCODE" }
  Remove-Item $tar -Force -ErrorAction SilentlyContinue
}
# Pi 侧只保留最近 7 份
ssh pi "cd ~/backups/windows-server && ls -1t | tail -n +22 | xargs -r rm -f; ls -lh | tail -n 6"
Write-Host "[done] $stamp"
