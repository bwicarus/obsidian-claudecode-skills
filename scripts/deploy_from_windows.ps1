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

# ── ② Pi 上拉到同一个提交 ───────────────────────────────────────────────
Write-Host '── 在 Pi 上拉取同一分支' -ForegroundColor Cyan
$pull = "cd $PiRoot && git fetch origin --prune && git checkout $branch && git pull --ff-only && git rev-parse HEAD"
$piHead = (ssh $PiHost $pull | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { Fail 'Pi 侧 git 拉取失败(可能有未提交改动或分支冲突),请先上机处理。' }
if ($piHead -ne $local) { Fail "Pi 的 HEAD ($($piHead.Substring(0,8))) 与本地 ($($local.Substring(0,8))) 不一致,已停止。" }
Write-Host "── Pi HEAD 与本地一致 ✓" -ForegroundColor Cyan

# ── ③ 调真正的部署脚本(门禁一条不少)────────────────────────────────────
$args = if ($PreflightOnly) { '--preflight-only' } else { '' }
Write-Host "── 触发 Pi 部署 $args" -ForegroundColor Cyan
ssh $PiHost "cd $PiRoot && bash scripts/deploy_reader.sh $args"
$rc = $LASTEXITCODE
if ($rc -ne 0) { Fail "部署脚本退出码 $rc —— 回滚与取证由 Pi 侧事务负责,去 Pi 上看 deploy-backups。" }
Write-Host '✅ 完成' -ForegroundColor Green
