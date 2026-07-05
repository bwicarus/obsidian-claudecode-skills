# bwicarus.space webapp 开发指南

Flask 多用户应用 + nginx 反代 + systemd service 的完整开发流程。

## 总览

```
浏览器 / iPad / Tailscale
        ↓ HTTPS (公网) 或 100.x.x.x (Tailscale)
        ↓
[nginx :443/:80] → location 路由
        ↓ /control, /dashboard, /profile, /admin, /history, /api, /auth, /login, /register, /logout
        ↓
[Flask webapp :5000]  ←  webapp/app.py（systemd webapp.service，Pi 上是 gunicorn）
        ├── 7 个 blueprint：control / skilltree / pdf_reader / fitness / insights / voice / assistant（+ pdf 蓝图上再挂 epub_assistant）
        ├── templates/*.html
        └── data/users/<u>/* + data/{dashboard,history}_template/
```

## ⚠ 静态文件由 nginx 直服，不经 gunicorn（改前端静态必读）

**`/static/*`（含 `/static/pdf/*`、`/static/pdfjs/*`、`nav.js`、`voice.js`、`fitness/`、`qa/` 等）全部由 nginx 从 `/var/www/html/static/` 直接 serve，永远不走 gunicorn。** 实测（Pi）：

```
curl http://127.0.0.1:5000/static/pdf/reader.js   → 404   （gunicorn 自带 static_folder = webapp/static，是另一个空/不同目录，根本没有这些文件）
curl https://<host>/static/pdf/reader.js          → 200 application/javascript   （nginx 从 /var/www/html/static/pdf/reader.js 直服）
```

nginx 两个 server 块各有 `location ^~ /static/pdf/ { … try_files $uri =404; }` + `^~ /static/pdfjs/`，`^~` 前缀优先级压过 proxy，其余 `/static/*` 落 `location / { try_files … }` 也从 `/var/www/html` 出。**含义**：

- 前端静态改动（`_server_deploy/static/pdf/reader.js`、`reader.src/*` 等）**部署目标是 `/var/www/html/static/`**，不是 `webapp/` 下的 Flask static。
- **验证静态改动别用 `:5000`**（那里恒 404），要走 **nginx base**（`https://<host>/static/...`）或直接 diff 部署目标文件 `/var/www/html/static/...`。
- 源码在 git `_server_deploy/static/`（`reader.js` 由 `reader.src/*.js` 拼），部署 = `sudo cp` 到 `/var/www/html/static/`（qa CDN 镜像、nav.js 同理，见 CLAUDE.md「服务器侧自动化」）。

## 源码位置

### git 仓库（`/c/claude/_server_deploy/`，作为部署参考）

| 路径 | 部署到服务器 |
|---|---|
| `_server_deploy/app.py` | `webapp/app.py`（git tracked，~1020 行，cp/scp 部署） |
| `_server_deploy/control.py` | `webapp/control.py` |
| `_server_deploy/templates/control.html` | `webapp/templates/control.html` |
| `_server_deploy/qa_server.py` | `claude/_server_deploy/qa_server.py`（git pull 拿到）|
| `_server_deploy/{skilltree,pdf_reader,fitness,fitness_coach,insights,voice,assistant,epub_assistant,youtube_*}.py` | `webapp/`（各 blueprint，app.py 末尾 `register_*` 挂载）|
| `_server_deploy/static/*` | **`/var/www/html/static/`**（nginx 直服，不进 `webapp/`，见上「静态文件由 nginx 直服」）|

> 部署根按环境换：Pi = `/home/bwicarus/webapp/`（**当前主力**）、VPS = `/root/webapp/`（⏸ 暂停）。下文沿用 VPS `/root/...` 视角写，Pi 换 `/home/bwicarus/...`。`_server_deploy/app.py` 是 Flask 主入口，**完全在版本控制下**，可像 control.py 一样 cp/scp 部署。

### 服务器（真实部署路径）

| 路径 | 内容 |
|---|---|
| `/root/webapp/app.py` | Flask 主入口（765 行，由各种 routes 直接挂 @app.route；源码 = git `_server_deploy/app.py`） |
| `/root/webapp/control.py` | 控制面板 blueprint（git tracked，scp 部署）|
| `/root/webapp/.env` | SECRET_KEY / RELAY_KEY / 其他 webapp env |
| `/root/webapp/templates/*.html` | Jinja2 模板 |
| `/root/webapp/data/users/<username>/{dashboard,history,private}/` | 用户私有数据 |
| `/root/webapp/data/{dashboard,history}_template/` | 缺省回落 |
| `/root/webapp/data/qa.json` | 历史 QA 数据（部分功能用） |
| `/root/webapp/data/app.db` | SQLite（用户、token、邀请码、QA 历史） |

### nginx 配置

| 路径 | 内容 |
|---|---|
| `/etc/nginx/sites-enabled/default` | 反代规则（每个 `/foo` location 一行）|
| `/root/nginx-backups/default.bak.<ts>` | 改前必备份（**别留在 sites-enabled/**，会被 nginx 当配置读冲突） |

## 现有路由清单

| 路由 | Method | 说明 |
|---|---|---|
| `/login` / `/logout` | GET/POST | session 登录 |
| `/register` | GET/POST | 邀请码注册 |
| `/profile/` | GET | 改密码 / API token 管理 / 下载客户端 .exe |
| `/api/change-password` | POST | 改密码 |
| `/api/tokens` | POST | 创建 API token |
| `/api/tokens/<id>` | DELETE | 删 API token |
| `/auth/device-link` | GET/POST | OAuth 登录回调 |
| `/admin/` | GET | 邀请码管理 / 用户列表（admin only） |
| `/admin/invites` | POST | 生成邀请码 |
| `/admin/invites/<token>/delete` | POST | 删邀请码 |
| `/admin/users/<id>/delete` | POST | 删用户 |
| `/dashboard/` | GET | 个人仪表板 |
| `/dashboard/graph-settings.json` | GET/POST | 图谱设置（per-user）|
| `/history/` | GET | 复习历史 |
| `/private/` | GET | 用户私有文件 |
| `/api/upload/<dataset>` | POST | 客户端上传（dashboard/history/private），bearer token |
| `/api/qa-history/<hid>/delete` | POST | 删一条 QA 历史（proxy 到 qa-server :9091）|
| `/api/nav-links` | GET/POST | per-user 导航链接持久化（nav.js 用）|
| `/control/` | GET | 控制面板首页 |
| `/control/api/status` | GET | 系统状态 JSON（systemd/AnkiConnect 慢字段走 5s TTL 缓存，见「控制面板状态轮询」节）|
| `/control/api/config/schema` | GET | server-config 字段 schema |
| `/control/api/ipad-config` | GET | iPad 端配置（cmd_server key / 端点等）|
| `/control/api/quota-log` | GET | 额度消耗日志（state/quota_log.json）|
| `/control/api/quota-now` | GET | 当前额度快照 |
| `/control/api/gemini-cost` | GET | Gemini 估算花费（按各次实际用的模型单价累计，AI Studio 无余额查询 API 只能本地累计）|
| `/control/api/kg-audit` | GET | KG 审查配置/状态（读 server-config `kg_audit.*` + `state/kg_audit.json`）|
| `/control/api/trigger/<action>` | POST | 触发 register/daily/upload/anki-restart/ankiweb-sync/switch-ai |
| `/control/api/trigger-log` | GET | webapp_trigger.log 末 N 行 |
| `/control/api/config` | GET/POST | 读写 server-config.json |
| `/control/api/kg-build` | POST | 新建书本（spawn `scripts/kg/build_nodes.py` + `scripts/kg/extract_edges.py` 后台任务） |
| `/control/api/kg-build-log` | GET | 新建书本 job 日志 |
| `/skilltree/<book>/` | GET | KG 可视化页（见 skill-tree-system.md） |
| `/pdf/` | GET | PDF 阅读器入口（PDF 列表） |
| `/pdf/view` | GET | PDF 阅读器主页 |
| `/pdf/file/<rel>` | GET | PDF 二进制 |
| `/pdf/api/page-chars` | GET | PyMuPDF char-level bbox（驱动 char-layer 选中）|
| `/pdf/api/page-nodes` | GET | 该页对应的 KG 节点 |
| `/pdf/api/dict` | GET | ECDICT 离线字典查询 |
| `/pdf/api/translate` | POST | AI 翻译（SSE 或 JSON） |
| `/pdf/api/explain` | POST | AI 解释（SSE 流式 + 自动上下文）|
| `/pdf/api/to-note` | POST | 选中 → vault 笔记 |
| `/pdf/api/upload` | POST | 上传 PDF 到 vault |
| `/pdf/api/list-pdfs` | GET | PDF 列表 JSON |
| `/pdf/api/snippets-to` | POST | 草稿 → 笔记 / Anki / 两者 |
| `/pdf/api/highlights` | GET/POST/PATCH/DELETE | 高亮 sidecar JSON 增删改查 |
| `/qa/` `/qa` | GET | 渲染 `qa.html`（旧 qa 流程，读 `data/qa.json`）|
| `/qa/update` | POST | 旧 relay 写 qa.json（X-API-Key = RELAY_KEY）|

> **PDF reader 完整路由**：pdf_reader.py（url_prefix=`/pdf`）现已有 **150+ 路由**（含 EPUB/HTML 阅读器、收藏夹、笔记页等），上表只列核心几条。未罗列的大类：阅读进度（`/api/reading-pos`）、收藏夹（`/api/favorites` CRUD + `/fav/open`↔`/fav/view` 打开跳转）、自建笔记页（`/api/userpages` CRUD + `/api/notes`/`/api/note-composite`）、EPUB 阅读器（`/epub/view`、`/api/epub-*` 一大批）、HTML 阅读器（`/html/view`、`/api/html-highlights`）、手写笔（`/api/ink`、`/api/epub-ink`）、生词系统（`/api/vocab-*`、`/api/page-vocab-marks`、`/api/dict-quick`、中日词典 `/api/dict-jp*`）、语法分析（`/api/grammar-*`）、句子翻译（`/api/translate-sentence`/`-config`/`-dismiss`）、OCR/预处理编排（`/api/preprocess-*`、`/api/compress-*`、`/api/reocr-page`）、图/公式（`/api/page-figures`、`/api/figure-crop`、`/api/formula-ocr`）、异步草稿（`/api/snippets-to-async`+`/api/job-status`）。**PDF 侧栏 Copilot** 由 `assistant.py`（`/pdf/chat`/`/history`/`/clear`/`/undo`/`/action-pref(s)`/`/prewarm`）提供；**EPUB Copilot** 由 `epub_assistant.py` 把 `/api/epub-assistant`、`/api/epub-convo[/append|/clear|/update-action]`、`/api/epub-action` 挂到同一 pdf 蓝图上。完整清单见 pdf_reader.py / [`pdf-reader.md`](pdf-reader.md)。

> **`/qa` 注意**：路由代码在 app.py 里仍 active（非废弃），iPad 现走 Tailscale 直连 qa-server :9091 不经 webapp（nginx `/qa` 反代 2026-05-11 已删，外部不可达）。但 `qa.html` 模板**不在**部署源目录 `_server_deploy/templates/`，只在废弃的 `webapp/templates/qa.html`，所以真机访问 `/qa` 会 TemplateNotFound。要么把模板加进部署源目录，要么真正删掉 `/qa`/`/qa/update` 两条路由。

详细 PDF reader 文档（含选中机制、高亮编辑、踩坑 17 条）：[`pdf-reader.md`](pdf-reader.md)

## 鉴权机制

```python
# /root/webapp/app.py
PROTECTED_PREFIXES = ("/dashboard", "/private", "/history", "/qa", "/profile", "/admin", "/auth", "/control", "/pdf")
PUBLIC_PREFIXES    = ("/login", "/logout", "/register", "/static")

@app.before_request
def require_login_global():
    # /api/upload 自带 bearer token，跳过 session 检查
    # PROTECTED_PREFIXES 路径无 session 重定向 /login
    # PUBLIC_PREFIXES 路径直接放过
    # 其他路径默认放过（注意：加新路由要决定属于哪个分类）
```

**加新路由时必做的事**：决定要不要登录 → 加 `/control` 这类 prefix 到 `PROTECTED_PREFIXES`。

## 数据存储

### SQLite（app.db）

`init_db()` 只 CREATE 三张表（users / invites / api_tokens）：

| 表 | 字段 |
|---|---|
| `users` | id / username / password_hash / role (admin/user) / created_at |
| `api_tokens` | id / user_id / token（**明文**，`secrets.token_urlsafe(32)` 直接 INSERT，非 hash）/ label / created_at / last_used_at |
| `invites` | token / created_by / created_at / used_by / used_at / expires_at / note（无 role 列，角色在 users 表）|

旧 QA 流程的数据存在文件 `data/qa.json`（`QA_FILE`），**不是** SQLite 表（app.db 里没有 qa_history 表）；新 QA 流程的 SQLite 在 `qa-server-data/` 下另一个 db（见 qa-browser-features.md）。

### 用户文件

`/root/webapp/data/users/<username>/{dashboard,history,private}/`：客户端 / 服务器 daily 流程上传 `dashboard.json` / `history.json` / 图片到这里。

`ALLOWED_DATASETS = {"dashboard", "history", "private"}` —— 三者都是合法上传 dataset（`/api/upload/<dataset>`）。private 也走 `_serve_user`，只是不享受 template fallback（template 同步只对 dashboard/history）。

Admin 用户上传的 HTML/CSS/JS 自动同步到 `dashboard_template/` / `history_template/`（其他用户没有这些文件时回落到 template）。

## 部署流程

### 改 control.py 或 control.html

```bash
# 本机编辑
nvim _server_deploy/control.py
nvim _server_deploy/templates/control.html

# scp 部署（不通过 git pull）
scp _server_deploy/control.py root@bwicarus.space:/root/webapp/control.py
scp _server_deploy/templates/control.html root@bwicarus.space:/root/webapp/templates/control.html

# 重启 webapp
ssh root@bwicarus.space 'systemctl restart webapp'

# 验证
curl -sI https://bwicarus.space/control/ -o /dev/null -w "HTTP %{http_code}\n"
# 期望 302 → /login（未登录）或 200（已登录）

# commit + push（让 _server_deploy/ 里的副本跟服务器同步）
git add _server_deploy/
git commit -m "control: ..."
git push origin main
```

### 改 app.py（罕见 + 慎重）

`app.py` 在 git（`_server_deploy/app.py`），流程跟 control.py 一致：本机改 → scp → restart → commit：

```bash
# 本机编辑
nvim _server_deploy/app.py

# scp 部署
scp _server_deploy/app.py root@bwicarus.space:/root/webapp/app.py

# 重启 + 验证
ssh root@bwicarus.space 'systemctl restart webapp && sleep 2 && systemctl is-active webapp || echo "FAILED, rollback!"'
ssh root@bwicarus.space 'journalctl -u webapp -n 20 --no-pager | tail'

# commit + push
git add _server_deploy/app.py
git commit -m "app: ..."
git push origin main
```

### 加新 location 到 nginx（少做）

```bash
ssh root@bwicarus.space

# 备份到 sites-enabled 外面（注意：bak 文件别留在 sites-enabled/ 否则 nginx 当配置读会冲突）
cp /etc/nginx/sites-enabled/default /root/nginx-backups/default.bak.$(date +%s)

# 编辑
nvim /etc/nginx/sites-enabled/default
# 加: location /new-prefix { proxy_pass http://127.0.0.1:5000; proxy_set_header Host $host; ... }

# 验证 + reload（不重启）
nginx -t && systemctl reload nginx
```

## webapp.service 配置（Pi 实机 = gunicorn）

```ini
# /etc/systemd/system/webapp.service（Pi）；副本在 references/systemd/webapp.service
[Unit]
Description=bwicarus webapp (Pi instance, gunicorn)
After=network.target

[Service]
Type=simple
User=bwicarus
WorkingDirectory=/home/bwicarus/webapp
EnvironmentFile=/home/bwicarus/webapp/.env
# 生产 WSGI：单 worker（保住进程内 _JOBS 断连恢复 + 后台线程共享内存）+ gthread 多线程并发 + timeout 600
ExecStart=/usr/bin/python3 -m gunicorn --workers 1 --threads 8 --worker-class gthread --timeout 600 --graceful-timeout 30 --bind 127.0.0.1:5000 --access-logfile - --error-logfile - app:app
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> 早期 VPS 版曾用 Flask dev server（`ExecStart=/usr/bin/python3 app.py`，User=root）；2026-06 起改 gunicorn（见下「生产 WSGI」节）。gunicorn 以 `app:app` 导入模块，`if __name__ == "__main__"` 块**不执行**，所以所有 `register_*(app)`（control/skilltree/pdf_reader/fitness/insights/voice/assistant）必须是**模块级**语句（在 `if __name__` 之前，app.py:993-1016 即如此）：

```python
# ... 所有 routes 定义
from control import register_control
register_control(app)
# ... skilltree / pdf_reader / fitness / insights / voice / assistant 依次 register
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)   # 仅本机裸跑调试用；生产走上面 gunicorn
```

> `webapp.service` 的 unit 文件**已**纳入 `references/systemd/webapp.service`，可跟真机对照。

## 本地实例（Windows）

同一份 `_server_deploy/app.py` 也能在 PC 前裸跑 localhost（**无 nginx / 无 gunicorn**），在 PC 前走本地算/本地 PDF/本地 Claude CLI，离开时 iPad 照走 Pi。三端共享 source of truth（Obsidian Sync + git + AnkiWeb）。入口 `_server_deploy/run_local.ps1`。

> ⚠ `run_local.ps1` 必须保持 **UTF-8 with BOM**（PowerShell 5.1 按 GBK 解码无 BOM 中文注释会炸）。Edit / Write 后立刻补 BOM。

### run_local.ps1 两种模式

1. **默认（推荐）**：用 `pythonw` 后台 detached 拉起托盘守护进程 `local_supervisor.pyw`，本 PS 窗口随即退出 —— **关窗不杀服务**（`run_local.ps1:85-97`）。守护自己负责健康检查 / 崩溃自动拉起 / 代码改动自动重启 / 开 Chrome `--app` 窗口 / 托盘菜单。
2. **`-Foreground`（仅调试）**：老的前台阻塞模式（直接 `python app.py`，Ctrl+C 停），自己开 Chrome `--app=…/dashboard/` 窗口（`run_local.ps1:73-84`）。

`run_local.ps1` 还做：首次生成 `.env.local`（随机 SECRET_KEY + 本机 vault/项目路径 + `SESSION_COOKIE_SECURE=0`，`run_local.ps1:23-40`）；加载 `.env.local` 到进程环境；依赖检查 `import flask, fitz, requests, jinja2, pystray, PIL`，缺则 `pip install`（`run_local.ps1:50-54`，比生产多了 `pystray`+`pillow` 给托盘）；提示一次性拷 PDF.js（不在 git）和 `app.db`（从 Pi 复用同账号登录）。选 `$Pythonw`：`$Python` 形如 `python.exe` → 同目录 `pythonw.exe`（缺则退回 `$Python`）；`$Python` 回退成 PATH 里的 `'python'` 时 → 用 `'pythonw'` 命令（避免弹控制台，`run_local.ps1:88-93`）。

### local_supervisor.pyw 守护架构

把本地 Flask 从「裸 dev server，关窗就宕」升级成大厂本地工具那套「本地 server + 托盘 + `--app` 窗口」范式（Jupyter/Gradio/Ollama 同路子），全程纯 Python，不引 Electron/Tauri/Node。`Supervisor` 类（`local_supervisor.pyw:302`）+ 托盘（`run_tray`）+ `monitor_loop` 后台线程（`local_supervisor.pyw:430`）。

| 能力 | 实现要点 | 代码 |
|---|---|---|
| **detached 后台** | `pythonw local_supervisor.pyw`，无控制台；Flask 子进程 `subprocess.Popen([sys.executable, "app.py"], creationflags=CREATE_NO_WINDOW)`，日志进 `FLASK_LOG` | `:330-348` |
| **崩溃自愈 + 退避** | `p.poll()` 检出退出 → `time.sleep(backoff)` 后 `restart()`，`backoff` 每次 ×2 封顶 30s。退避**按"稳定存活时长"重置而非每 tick**：存活超 `_STABLE_SECS=30` 才把 backoff/快失败计数归零 | `:448-468` |
| **连续快失败熔断** | 存活不到 `_FAST_FAIL_SECS=10` 算一次"快失败"；连续 `_FAST_FAIL_LIMIT=5` 次 → `_halted=True` 暂停自动重启 + 托盘气泡报警（端口占用/缺 SECRET_KEY/代码错时不刷爆日志），等用户托盘「重启服务」手动解除 | `:449-458` |
| **Job Object** | `CreateJobObjectW` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`(0x2000)，Flask 子进程 `AssignProcessToJobObject`。守护进程一旦消失（含被任务管理器强杀）Flask 随之被带走 → **不留残留占 5000 端口**。失败则降级靠熔断兜底 | `:157-226` |
| **单实例锁** | `CreateMutexW` 命名 mutex `Local\bwicarus-local-supervisor`（per-session）+ `WinDLL(use_last_error=True)` 读 `ERROR_ALREADY_EXISTS`(183)；已在运行 → MessageBox 提示后静默退出 | `:135-150` |
| **代码变更自动重启** | 轮询 `DEPLOY_DIR/*.py` 的 mtime，变了 debounce 1.5s 后 `restart`（治「改端点要手动重启」）。`auto_reload` 默认开，托盘可切 | `:419-474` |
| **开机自启** | 写 `HKCU\…\Run` 值名 `BwicarusLocal`，命令 = `"pythonw" "local_supervisor.pyw 绝对路径"`（源码模式有效） | `:240-268` |
| **看护 Obsidian 同步** | `ensure_obsidian_sync()` 用 `Get-ScheduledTask … .State`（语言中立枚举，不解析中文 schtasks）看「Obsidian Headless Sync」计划任务：`Disabled`→`Enable-ScheduledTask`、非 `Running`→`Start-ScheduledTask`。开机即看护一次 + 每 `_SYNC_CHECK_TICKS=150` tick(~5min)一次。`keep_sync` 默认开，托盘可切。防住「任务被禁没人发现」（PC vault 曾因此停更 3 周）| `:272-298, 433-442` |
| **启动前预检** | `_preflight()` 查 `requests`/`pystray`/`PIL` + `SECRET_KEY`，缺则原生 `MessageBox` 报清楚（pythonw 无控制台时唯一可见报错途径），**绝不静默闪退**。绕过 run_local.ps1 直接双击/HKCU 自启时这是唯一护栏 | `:561-578` |

**健康检查**：`_wait_healthy` GET `http://127.0.0.1:5000/`，任何 HTTP 响应（含 302/401）都算活；首次健康后 `_first_open` 才开一次 Chrome `--app` 窗口（之后崩溃重启不再弹窗，用户原窗口自动重连）。

**托盘菜单**（绿色 L 图标，`run_tray`，`:544-554`）：打开 webapp 窗口 / 重启服务（manual 重启会清熔断+快失败计数）/ 打开日志 / ☑ 开机自启 / ☑ 代码变更自动重启 / ☑ 保持 Obsidian 同步 / 退出。

**数据/日志**：`DATA_DIR` = `WEBAPP_DATA` 或 `<项目根>/webapp-data`；守护日志 `local_supervisor.log`、Flask 日志 `local_flask.log`。

### 控制面板的本地实例适配（n/a 灰点不报警）

`/control/api/status` 经 `get_status_cached()` 调 `get_systemd_statuses(units)`（批量版，见下「控制面板状态轮询」）—— `sys.platform=="win32"` 直接返回全 `"n/a"`（`control.py:52-53`，本地实例无 systemd，`xvfb`/`anki-headless`/`obsidian-sync`/`qa-server`/`tailscaled` 是 Pi 专属服务）。`control.html` 前端据此区分：

- `svc()` 里 `val==='n/a'` → dot 类 `na`（`.dot.na` 灰点 `#6b7280` + 不报警，`control.html:124,361`），值显示「本地不适用」（`control.html:364`）。
- 状态总判定：`local = data.anki_headless==='n/a'` 识别本地实例；`svcOk` 把 `'active'` 和 `'n/a'` 都算正常；`allOk = svcOk && (ankiOk || local)` → **本地实例不因 Pi 专属服务 n/a 或 Anki 未开而报「有问题」**，meta 显示「本地实例正常」（`control.html:374-377`）。

## 现有模板

| 模板 | 用途 |
|---|---|
| `login.html` | 登录页 |
| `register.html` | 注册（邀请码） |
| `profile.html` | 个人页（改密 / token / 下载 client）—— 浅色 + 白卡 + indigo 风格 |
| `admin.html` | 管理后台 |
| `control.html` | 控制面板（深色毛玻璃 + 仿 dashboard panel-stack + 左侧 drawer） |
| `device_link.html` | OAuth 设备登录回调 |
| `qa.html` | 旧 qa 流程页面，**不在** 部署源目录 `_server_deploy/templates/`（只在废弃的 `webapp/templates/qa.html`）；`/qa` 路由仍在但模板缺失，访问会 TemplateNotFound |

`data/dashboard_template/index.html` 和 `data/history_template/index.html` 是仪表板/历史的页面（个人化数据由 JS fetch 加载），用户私有目录里有自己的副本时优先用私有的。

## 样式设计语言

**两套主题**：

1. **浅色卡片风**（profile / login / register）：
   - 背景 `#f0f2f5`，卡片 `#fff` + 14px 圆角 + 浅阴影
   - 标题 22px 700，副标 13px #6b7280
   - 主按钮黑色 `#1a1a1a`（hover `#333`）
   - 强调色 indigo `#6366f1`

2. **深色毛玻璃风**（dashboard / history / control）：
   - 背景深紫渐变 `#0f0c29 → #1a1a3e → #0d1b3e` + 3 层 radial 高光
   - 玻璃 panel 18px 圆角 + `backdrop-filter: blur(20px)` + `var(--glass-bg/border)` 半透明
   - 紫色调强调 `#a5b4fc` / `#c4b5fd`
   - 状态色：`--emerald` `--amber` `--rose` `--violet`
   - panel-stack 折叠/展开（单 active + 其他暗化 0.55）
   - 抽屉滑动 `cubic-bezier(0.4, 0, 0.2, 1)`

加新页面时**先决定**用哪套，参考最近的同主题文件。

## 测试 / debug 流程

```bash
# 本机看返回码
curl -sI https://bwicarus.space/<path>

# 服务器看 webapp 日志
ssh root@bwicarus.space 'journalctl -u webapp -n 50 --no-pager'

# 服务器看 nginx 日志
ssh root@bwicarus.space 'tail -30 /var/log/nginx/access.log'
ssh root@bwicarus.space 'tail -30 /var/log/nginx/error.log'

# python flask 直接跑（debug 模式）
ssh root@bwicarus.space
systemctl stop webapp
cd /root/webapp
python3 app.py    # 阻塞，看 stdout
# 修完 Ctrl+C
systemctl start webapp
```

## 客户端上传 API（被 EXE / `_full_daily_pipeline` 调用）

```python
# 客户端调（uploader 是模块函数，3 个参数：client, root, dataset）
from uploader import upload_dataset
upload_dataset(client, Path(dash_dir), "dashboard")
# 内部对每个文件调 api_client.upload(dataset, rel_path, data)：
#   POST /api/upload/dashboard，单文件 body（application/octet-stream）
#   相对路径走 X-Path header（不是 multipart files 复数）
# Header: Authorization: Bearer <api_token>

# 白名单：JSON + 图片（PNG/JPG/PDF），不传 HTML/JS/CSS
# admin 用户上传的 HTML/CSS/JS 自动同步到 template
```

## 全站 PWA service worker（2026-06-07）

让 PC/iPad **重复打开秒回 + 数据本地缓存**，Pi 仍是唯一写入源。`app.py`：

- `/sw.js` 路由（`_SITE_SW_TEMPLATE`，`Cache-Control: no-cache` + `Service-Worker-Allowed: /`）；`_PWA_HEAD` 注入 `navigator.serviceWorker.register('/sw.js',{scope:'/'})`（after_request `inject_nav` 给登录后页面注）。
- **VERSION = SW 代码的 md5**（`_SITE_SW_VERSION`）→ 改了缓存逻辑 version 自动变 → `activate` 清旧缓存，**不靠手动 bump**。
- 策略：`/static` + 数据 GET（`/api`、`*.json`、`/dashboard/`、`/history/`、`/insights`）→ **stale-while-revalidate**（先返本地缓存秒开、后台刷新）；导航/HTML → **network-first + 离线回退**；`login/logout/auth/admin`、POST、媒体 Range、`/pdf/`、跨源 → 放行不缓存；只缓存 `status===200 && type==='basic'`。
- **登出清缓存**：`/logout` 请求时 SW 清掉所有 `bw-*` 缓存（防共享设备/登出后私有数据 + `window.__USER__` 残留）。
- `/pdf/` 由它**自己的** `/pdf/sw.js`（缓存页图 + page-chars）管，scope 更具体优先；两者隔离共存。
- ⚠ **nginx 必须把 `/sw.js` 代理到 Flask**（前缀代理 + 静态兜底的站点会 404）→ 见 [`raspberry-pi-deployment.md`](raspberry-pi-deployment.md)（Pi 手工 patch，80/443 各一条 `location = /sw.js`）。验证：`curl -sk https://<host>/sw.js` 返 `application/javascript`。

## 安全 / 生产化加固（2026-06 全栈审计落地）

对照大厂/OWASP 标准的一轮加固。**源码在 git，部署见各项；Pi nginx 手工 patch 绝不 cp。**

### 生产 WSGI：gunicorn（替代 Flask dev server）
- `webapp.service` 的 `ExecStart` 改为 `gunicorn --workers 1 --threads 8 --worker-class gthread --timeout 600 --graceful-timeout 30 --bind 127.0.0.1:5000 --access-logfile - --error-logfile - app:app`。
- **必须单 worker**：`pdf_reader.py` 的 `_JOBS`(AI 断连恢复)+ 4 处后台 `threading.Thread` 是**进程内**状态;多 worker 会割裂(轮询打到别的 worker 拿不到结果)。单 worker + gthread = 等同原 `threaded=True` 的内存共享并发,但生产级。
- `timeout 600` 放过慢 AI/翻译;access log 进 journald(`journalctl -u webapp`)。
- 装:`/usr/bin/python3 -m pip install --break-system-packages -r _server_deploy/requirements.txt`(Debian PEP668)。unit 副本 `references/systemd/webapp.service`。

### nginx 加固（Pi `/etc/nginx/sites-available/bwicarus` 手工 patch，先 `nginx -t` 再 reload）
- **安全响应头**(443 server 块):HSTS `max-age=15552000` + `X-Content-Type-Options:nosniff` + `X-Frame-Options:SAMEORIGIN` + `Referrer-Policy:strict-origin-when-cross-origin`;两块都 `server_tokens off`。⚠ CSP 故意没加(站内大量内联脚本 + CDN MathJax,'unsafe-inline' 会抵消价值且易打挂)。
- **gzip**:`gzip on` 已在 nginx.conf;site 文件 http 顶部补 `gzip_proxied any`(否则代理响应不压)+ `gzip_types`(JSON/JS/CSS/svg)。
- **限流**:http 顶部 `limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/m`;**仅** `/login` `/register` 加 `limit_req zone=auth burst=5 nodelay`。**不限 /api**(PWA 高频调用会误伤)。
- **上传上限**:`client_max_body_size 0`(无限)→ `600m`(防 DoS,够大 PDF)。
- ⚠ nginx `add_header` 继承坑:有自己 `add_header` 的 location(如 `/static/pdfjs/`)不继承 server 级安全头;代理类 location 无 add_header → 正常继承。

### 数据稳健（对线上零影响）
- **SQLite WAL**:`app.py _tune_conn` + `fitness.py`/`youtube_subtitles.py` 的 `_db` 都加 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL` → 并发读写不再 "database is locked"。
- **原子写**:高频 sidecar(高亮 `_hl_save`/墨迹 `_ink_save`/偏好/词组)本已 tmp+replace;app.py 用户设置(graph-settings/nav-links)补 `_atomic_write_text`。
- **自动备份**:`scripts/backup_data.sh` + systemd `bwicarus-backup.timer`(每日 03:30)。只备份**不可再生**数据(webapp/data 的 app.db/youtube/fitness DB 走 `sqlite3 .backup` 在线一致备份 + 用户 json;claude/state 小状态文件),**排除** page-img/OCR/模型/搜索索引等可再生大缓存。保留 14 份,落 `/home/bwicarus/backups/`。

### 鉴权加固（app.py）
- **开放重定向**:`/login?next=` 经 `_safe_next()` 只放行站内相对路径(拒 `//host`、`http(s)://`、`javascript:`)。
- **CSRF**:零依赖会话级 token(`_csrf_token()`/`_csrf_ok()` + `context_processor` 注入 `csrf_token()`)。**仅**登录/注册两个公开 HTML 表单校验(模板加隐藏 `csrf_token` 域);JSON API 不走(靠 `SameSite=Lax` + session/Bearer,给所有 fetch 串 token 风险高收益低)。`SameSite=Lax` 本已挡跨站带 cookie 的 POST。
- session cookie 早已 `HttpOnly + SameSite=Lax + Secure`,SECRET_KEY 走 env(无需改)。`SESSION_COOKIE_SECURE` 随环境降级:`os.environ.get("SESSION_COOKIE_SECURE","1") not in ("0","false","False")`(`app.py:33`),**默认仍 Secure(生产不变)**;本地裸 http 实例由 `.env.local` 设 `SESSION_COOKIE_SECURE=0` 关掉,兼容非 Chrome 浏览器(注:Chrome/Firefox 对 127.0.0.1/localhost 视作 secure context,即便 True 本地也能登录,此处降级只为边界场景)。

**部署回顾**:Python 改 `cp _server_deploy/*.py /home/bwicarus/webapp/` + `systemctl restart webapp`;模板 `cp templates/*.html`;nginx `nginx -t && systemctl reload nginx`;systemd `cp references/systemd/*.{service,timer} /etc/systemd/system/ && daemon-reload`。VPS 实例同源码,nginx 用 git 的 `_server_deploy/nginx/bwicarus.conf`(需同步加这些 header/gzip/limit_req)。

## 控制面板状态轮询（/control/api/status 缓存 + 前端节流，2026-06）

面板轮询最快 500ms 一发，原实现每发都 spawn 5 次 `systemctl is-active` + ping AnkiConnect，单 worker gunicorn 上会吃满 CPU。现拆成「慢字段缓存 + 快字段现读」：

### 后端（control.py）

- **`get_systemd_statuses(units)`**（`control.py:50`）：**一次** `systemctl is-active <u1> <u2> ...` 批量查全部 unit（`_SYSTEMD_UNITS` = anki-headless / xvfb-99 / obsidian-sync / qa-server / tailscaled）。不看 returncode（任一 unit 非 active 整体就非零），stdout **每行按参数顺序对应一个 unit**。Windows 本地实例直接返回全 `"n/a"`。
- **5s TTL 缓存 + 单飞**（`get_status_cached()`，`control.py:102`）：慢字段（5 个 systemd 状态 + `ankiconnect_version` ping）进模块级 `_status_cache`，`_STATUS_TTL=5.0`。过期时由抢到 `_status_lock`（**非阻塞** acquire）的那个请求重算，其余并发请求直接拿旧值不阻塞；只有冷启动（缓存还没有值）才阻塞等首算。
- **快字段不进缓存**：`ai_backend` / `last_run` / `active_tasks` 每请求现读（任务进度依赖新鲜度，且读文件便宜，`control_status`，`control.py:168-177`）。
- **主动失效**：`/control/api/trigger/<action>` 入口处 `_status_cache["ts"] = 0`（`control.py:337`，触发类操作可能改服务状态，让状态点最多 1 个轮询周期内反映）；`/control/api/config` POST 重启 qa-server 那条分支同样失效（`control.py:264`）。

### 前端（control.html `tick()` 轮询链，`control.html:681-694`）

- `document.hidden` 时跳过本轮（后台标签页不发请求，省服务器）。
- `await refreshStatus()` —— 防慢请求时轮询堆叠，同一时刻 in-flight ≤ 1。
- `setTimeout(tick, interval)` 放在 **finally**：改成 await 后 fetch 异常会把递归 setTimeout 链整个断掉，必须 finally 续命（旧版 tick 不 await、fire-and-forget，没这问题但请求会堆叠）。
- 自适应间隔：默认 `NORMAL_INTERVAL=1500ms`；`Date.now() < _fastPollUntil` 时用 `FAST_INTERVAL=500ms` —— 有活跃任务 +10s、daily `running` +60s、手动 trigger 后 +30s。
- 「操作」panel 处于 active 时才连带刷 `refreshTriggerLog()`（同样 await）。
