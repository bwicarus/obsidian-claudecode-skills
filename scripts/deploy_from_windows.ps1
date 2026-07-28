#Requires -Version 5.1
<#
.SYNOPSIS
  从 Windows 开发机远程触发 Pi 上的阅读器部署。

.DESCRIPTION
  这是一层**薄封装**,不含任何门禁逻辑 —— 部署与全部门禁仍由 Pi 上的
  scripts/deploy_reader.sh 执行,这里只负责:
    ① 确认本地无未提交改动、且已推送(否则 Pi 拉不到你要部署的代码)
    ② ssh 到 Pi 拉取同一分支
    ③ 调用真正的部署脚本,原样透传输出与退出码

  绝不在 Windows 侧复制门禁、绝不跳过任何一步。任一步失败即停。

.PARAMETER PreflightOnly
  只跑无副作用预检(Pi 上不创建备份/release/current)。

.EXAMPLE
  powershell -File scripts\deploy_from_windows.ps1 -PreflightOnly
  powershell -File scripts\deploy_from_windows.ps1
#>
param(
    [switch]$PreflightOnly,
    [string]$PiHost = 'bwicarus@100.101.15.57',
    [string]$PiRoot = '/home/bwicarus/claude'
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "❌ $msg" -ForegroundColor Red; exit 1 }

# 自定位仓库根:脚本可能被从任意目录以 -File 调用(实测踩过),不能假设 cwd。
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot '.git'))) { Fail "$RepoRoot 不是 git 仓库。" }
Set-Location $RepoRoot

# ── ① 本地状态必须干净且已推送 ──────────────────────────────────────────
$dirty = git status --porcelain
if ($dirty) {
    Write-Host $dirty
    Fail '本地有未提交改动。Pi 只能部署已推送的代码,先提交并 push。'
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
git fetch origin --quiet
$ahead = (git rev-list --count "origin/$branch..HEAD").Trim()
if ($ahead -ne '0') { Fail "本地领先远端 $ahead 个提交,先 git push。" }

$local = (git rev-parse HEAD).Trim()
Write-Host "── 本地 $branch @ $($local.Substring(0,8)),已与远端一致" -ForegroundColor Cyan

# ── ② 共享检出安全闸(Pi 侧只读判定,绝不远程改工作树)──────────────────
# Pi 的检出是共享的(我 + Codex),而且每晚 daily 会重写 anki/records 与
# dashboard.json —— "工作树是脏的"是常态,不能一刀切拒绝。闸门只拦真正危险的
# 那一种:**本次要拉的提交,恰好会改到别人正在改的文件**。
Write-Host '── 共享检出安全闸' -ForegroundColor Cyan
ssh $PiHost "cd $PiRoot && bash scripts/deploy_remote_guard.sh $branch"
switch ($LASTEXITCODE) {
    0 { }
    2 { Fail 'Pi 不在目标分支上。远程不切分支(共享检出),请上机确认。' }
    3 { Fail 'Pi 上有人正在改本次要拉的文件。不自动 stash/reset,请上机协调。' }
    default { Fail "安全闸执行失败(退出码 $LASTEXITCODE)。" }
}

# ── ③ Pi 上快进到同一个提交(只 ff,不切分支)───────────────────────────
Write-Host '── 在 Pi 上快进到同一提交' -ForegroundColor Cyan
$piHead = (ssh $PiHost "cd $PiRoot && git merge --ff-only origin/$branch >/dev/null && git rev-parse HEAD" | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { Fail 'Pi 侧快进失败,请上机处理。' }
if ($piHead -ne $local) { Fail "Pi 的 HEAD ($($piHead.Substring(0,8))) 与本地 ($($local.Substring(0,8))) 不一致,已停止。" }
Write-Host "── Pi HEAD 与本地一致 ✓" -ForegroundColor Cyan

# ── ④ 调真正的部署脚本(门禁一条不少)────────────────────────────────────
$args = if ($PreflightOnly) { '--preflight-only' } else { '' }
Write-Host "── 触发 Pi 部署 $args" -ForegroundColor Cyan
ssh $PiHost "cd $PiRoot && bash scripts/deploy_reader.sh $args"
$rc = $LASTEXITCODE
if ($rc -ne 0) { Fail "部署脚本退出码 $rc —— 回滚与取证由 Pi 侧事务负责,去 Pi 上看 deploy-backups。" }
Write-Host '✅ 完成' -ForegroundColor Green
