# 注册/更新 Windows 计划任务
# 移动项目文件夹后运行此脚本即可，无需手动编辑任务调度器

$SCRIPTS_DIR = $PSScriptRoot
$TASK_NAME   = "Obsidian Anki 每日状态更新"
$PS_SCRIPT   = Join-Path $SCRIPTS_DIR "daily_anki_status.ps1"

$action  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PS_SCRIPT`""

$trigger = New-ScheduledTaskTrigger -Daily -At "04:00"

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "每天04:00启动Obsidian同步，5分钟后查询Anki掌握情况写回笔记frontmatter" | Out-Null

Write-Host "任务已注册：$TASK_NAME"
Write-Host "执行脚本：$PS_SCRIPT"
Write-Host "下次运行：$(( schtasks /query /tn $TASK_NAME /fo LIST 2>&1 | Select-String 'Next Run' ) -replace '.*?:\s*','')"
