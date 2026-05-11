# 每日自动化任务：登记新笔记 → 更新 Anki 状态 → 复习优先级 → 推送仪表板
# 每步状态写到 state/last_run.json，dashboard 会展示。

$PYTHON      = "C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
$SCRIPTS_DIR = $PSScriptRoot
$ANKI        = "C:\Users\bwica\AppData\Local\Programs\Anki\anki.exe"
$RUN_LOG     = "C:\claude\state\last_run.json"

# ── 跑步骤的辅助函数：捕获 exit code、时长，增量写 last_run.json ──
$Global:RunStart = Get-Date
$Global:Steps    = @()

function Update-RunLog {
    param([string]$FinalStatus = "running")
    $payload = @{
        kind        = "daily_anki_status"
        started_at  = $Global:RunStart.ToString("o")
        updated_at  = (Get-Date).ToString("o")
        status      = $FinalStatus
        steps       = $Global:Steps
    }
    $payload | ConvertTo-Json -Depth 5 | Set-Content -Path $RUN_LOG -Encoding UTF8
}

function Step {
    param([string]$Name, [scriptblock]$Block)
    Write-Host "▶ $Name $(Get-Date -Format 'HH:mm:ss')..."
    $start = Get-Date
    $err = $null
    $rc = 0
    try {
        & $Block
        $rc = $LASTEXITCODE
        if ($null -eq $rc) { $rc = 0 }
    } catch {
        $err = $_.Exception.Message
        $rc = -1
    }
    $end = Get-Date
    $entry = @{
        name        = $Name
        status      = if ($rc -eq 0 -and -not $err) { "ok" } else { "failed" }
        rc          = $rc
        error       = $err
        started_at  = $start.ToString("o")
        ended_at    = $end.ToString("o")
        duration_s  = [int](($end - $start).TotalSeconds)
    }
    $Global:Steps += $entry
    Update-RunLog
    if ($entry.status -eq "failed") {
        Write-Host "  ✗ failed (rc=$rc) $err" -ForegroundColor Red
    } else {
        Write-Host "  ✓ ok ($($entry.duration_s)s)"
    }
}

# ── State 滚动备份（不入 last_run.json，太碎）──
$BackupDir = "C:\claude\state\backup"
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$Today = Get-Date -Format "yyyyMMdd"
Copy-Item "C:\claude\state\note-states.json" "$BackupDir\note-states-$Today.json" -Force -ErrorAction SilentlyContinue
$RecordsArchive = "$BackupDir\anki-records-$Today.zip"
if (-not (Test-Path $RecordsArchive)) {
    Compress-Archive -Path "C:\claude\anki\records\*" -DestinationPath $RecordsArchive -Force -ErrorAction SilentlyContinue
}
Get-ChildItem "$BackupDir\note-states-*.json"  | Sort-Object Name -Descending | Select-Object -Skip 7 | Remove-Item -ErrorAction SilentlyContinue
Get-ChildItem "$BackupDir\anki-records-*.zip"  | Sort-Object Name -Descending | Select-Object -Skip 7 | Remove-Item -ErrorAction SilentlyContinue

Update-RunLog "running"

# Step 0: 确保 Obsidian Sync daemon 在跑 + 启动 Anki(AnkiConnect 需要 GUI)
Step "启动应用" {
    $daemon = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -match 'obsidian-headless' }
    if (-not $daemon) {
        Start-ScheduledTask -TaskName 'Obsidian Headless Sync' -ErrorAction SilentlyContinue
    }
    if (-not (Get-Process -Name "anki" -ErrorAction SilentlyContinue)) { Start-Process $ANKI }
    # daemon 拉起后几秒就能首跑;Anki 启动 + AnkiConnect 上线大概 30-60 秒
    Start-Sleep -Seconds 90
}

# Step 1: 登记新笔记
Step "登记新笔记" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "register_notes.py")
}

# Step 2: 更新 Anki 状态（写 frontmatter + record）
Step "更新 Anki 状态" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "anki_status.py") `
        --all --write-frontmatter --write-record --wait-seconds 300
}

# Step 3: 计算复习优先级
Step "计算复习优先级" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "review_priority.py") `
        --write-frontmatter --write-record
}

# Step 4: 重建必复习牌组
Step "重建必复习牌组" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "build_review_deck.py")
}

# Step 5: 清理孤儿（不致命，失败不影响后续）
Step "清理孤儿" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "cleanup_orphans.py") --apply
}

# Step 6: 导出仪表板
Step "导出仪表板" {
    & $PYTHON (Join-Path $SCRIPTS_DIR "export_dashboard.py")
}

# Step 7: SCP 推送
Step "推送仪表板" {
    $DASHBOARD = "C:\claude\dashboard"
    scp "$DASHBOARD\index.html" "$DASHBOARD\dashboard.json" "root@31.220.31.30:/root/webapp/data/dashboard/"
}

# ── 总结 ──
$failed = $Global:Steps | Where-Object { $_.status -ne "ok" }
$finalStatus = if ($failed.Count -gt 0) { "failed" } else { "ok" }
Update-RunLog $finalStatus

if ($finalStatus -eq "ok") {
    Write-Host "✓ 全部完成 $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Green
} else {
    Write-Host "✗ 有 $($failed.Count) 步失败 $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Red
}
