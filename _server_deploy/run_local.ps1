# bwicarus webapp —— Windows 本地实例启动器(Flask-only,无 nginx/gunicorn)
# 用途:在 PC 前走 localhost,本地算/本地 PDF/本地 Claude CLI;离开时 iPad 照走 Pi。
# 三端共享 source of truth(Obsidian Sync + git + AnkiWeb)。
# 用法:右键「用 PowerShell 运行」,或 `powershell -ExecutionPolicy Bypass -File run_local.ps1`
#   默认:用 pythonw 后台拉起「托盘守护进程」(local_supervisor.pyw)后本窗口即退出 —— 关窗不再杀服务,
#         崩溃自动拉起、代码改动自动重启、托盘菜单(打开/重启/日志/自启)。这是推荐用法。
#   -Foreground:老的前台阻塞模式(直接 python app.py,Ctrl+C 停),仅供调试。
# 首次会生成 .env.local;pdfjs 需一次性拷过来(见末尾提示)。
param([switch]$Foreground)
$ErrorActionPreference = "Stop"

$ProjectRoot = if ($env:CLAUDE_PROJECT) { $env:CLAUDE_PROJECT } else { "C:\claude" }
$Vault       = if ($env:OBSIDIAN_VAULT) { $env:OBSIDIAN_VAULT } else { "C:\obsidian" }
$DeployDir   = Join-Path $ProjectRoot "_server_deploy"
$DataDir     = Join-Path $ProjectRoot "webapp-data"
$EnvFile     = Join-Path $ProjectRoot ".env.local"
$Python      = "C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# ── 1) 首次生成 .env.local(随机 SECRET_KEY;路径指向本机 vault/项目)──────────────
if (-not (Test-Path $EnvFile)) {
  $bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $secret = -join ($bytes | ForEach-Object { '{0:x2}' -f $_ })
  @(
    "SECRET_KEY=$secret",
    "WEBAPP_DATA=$DataDir",
    "CLAUDE_PROJECT=$ProjectRoot",
    "OBSIDIAN_VAULT=$Vault",
    "OBSIDIAN_VAULT_NAME=Obsidian Vault",
    "AI_SETTINGS_FILE=$ProjectRoot\state\ai-settings.json",
    "APP_CLAUDE=claude",
    "APP_PYTHON=$Python",
    "SESSION_COOKIE_SECURE=0"
    # 注意:不设 PDF_XACCEL → 走 send_file(无 nginx);不设 PASSWORD_HASH → 见下方账号说明
    # SESSION_COOKIE_SECURE=0:本地裸 http,关掉 Secure 兼容非 Chrome 浏览器(Chrome 对 127.0.0.1 本就放行)
  ) | Set-Content -Encoding UTF8 $EnvFile
  Write-Host "[run_local] 已生成 $EnvFile" -ForegroundColor Green
}

# ── 2) 加载 .env.local 到进程环境 ─────────────────────────────────────────────
Get-Content $EnvFile | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
  }
}

# ── 3) 检查依赖(缺则装;Windows 不用 gunicorn;pystray+pillow 给托盘守护进程)─────────
& $Python -c "import flask, fitz, requests, jinja2, pystray, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[run_local] 安装缺失依赖..." -ForegroundColor Yellow
  & $Python -m pip install flask requests pymupdf jinja2 pystray pillow
}

# ── 4) pdfjs 检查(阅读器 ES module 需要;不在 git,需一次性拷过来)────────────────
$PdfjsMjs = Join-Path $DeployDir "static\pdfjs\pdf.mjs"
if (-not (Test-Path $PdfjsMjs)) {
  Write-Host "[run_local] ⚠ 缺 PDF.js,阅读器会挂。一次性从 Pi 拷:" -ForegroundColor Red
  Write-Host "  scp -r bwicarus@100.101.15.57:/var/www/html/static/pdfjs $DeployDir\static\" -ForegroundColor Yellow
}

# ── 5) 账号:首次需 app.db。从 Pi 拷可复用同密码登录;或设 PASSWORD_HASH 引导新 admin ──
$Db = Join-Path $DataDir "app.db"
if (-not (Test-Path $Db)) {
  Write-Host "[run_local] ⚠ 无 app.db。复用 Pi 同账号:" -ForegroundColor Yellow
  Write-Host "  scp bwicarus@100.101.15.57:/home/bwicarus/webapp/data/app.db $DataDir\" -ForegroundColor Yellow
  Write-Host "  (或在 .env.local 设 PASSWORD_HASH=<werkzeug hash> 引导新 admin 'bwicarus')" -ForegroundColor Yellow
}

# ── 6) 启动 ────────────────────────────────────────────────────────────────────
Set-Location $DeployDir
if ($Foreground) {
  # 调试模式:前台阻塞跑 + 自己开 Chrome --app 窗口(老行为)
  Write-Host "[run_local] 前台启动 http://127.0.0.1:5000/ (Ctrl+C 停)" -ForegroundColor Green
  $pf86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
  $chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "$pf86\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($chrome) { Start-Process $chrome -ArgumentList "--app=http://127.0.0.1:5000/dashboard/" }
  else { Start-Process "http://127.0.0.1:5000/" }
  & $Python app.py
} else {
  # 推荐模式:pythonw 后台拉起托盘守护进程(detached),本窗口随即退出 —— 关窗不杀服务。
  # 守护自己负责:健康检查、崩溃自动拉起、代码改动自动重启、开 Chrome --app 窗口、托盘菜单。
  if ($Python -match 'python\.exe$') {
    $Pythonw = $Python -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path $Pythonw)) { $Pythonw = $Python }   # 同目录没 pythonw 才退回 python
  } else {
    $Pythonw = 'pythonw'                                     # $Python 是 PATH 里的 'python' → 用 'pythonw' 命令(避免弹控制台)
  }
  Start-Process -FilePath $Pythonw -ArgumentList "`"$DeployDir\local_supervisor.pyw`"" -WorkingDirectory $DeployDir
  Write-Host "[run_local] 托盘守护已后台启动(右下角托盘有绿色 L 图标)。" -ForegroundColor Green
  Write-Host "[run_local] 关窗不影响服务;停止请在托盘图标右键→退出。调试用前台:run_local.ps1 -Foreground" -ForegroundColor Green
}
