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
[Flask webapp :5000]  ←  /root/webapp/app.py（systemd webapp.service）
        ├── control.py blueprint（控制面板）
        ├── templates/*.html
        └── data/users/<u>/* + data/{dashboard,history}_template/
```

## 源码位置

### git 仓库（`/c/claude/_server_deploy/`，作为部署参考）

| 路径 | 部署到服务器 |
|---|---|
| `_server_deploy/app.py` | `/root/webapp/app.py`（git tracked，765 行，scp 部署） |
| `_server_deploy/control.py` | `/root/webapp/control.py` |
| `_server_deploy/templates/control.html` | `/root/webapp/templates/control.html` |
| `_server_deploy/qa_server.py` | `/root/claude/_server_deploy/qa_server.py`（git pull 拿到）|

> 注：`_server_deploy/app.py` 是 Flask 主入口，**完全在版本控制下**，可像 control.py 一样 scp 部署。仓库里另有一个废弃的 `webapp/app.py`（旧 stub，~3.7KB）应清理删除。

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
| `/control/api/status` | GET | 系统状态 JSON |
| `/control/api/config/schema` | GET | server-config 字段 schema |
| `/control/api/ipad-config` | GET | iPad 端配置（cmd_server key / 端点等）|
| `/control/api/quota-log` | GET | 额度消耗日志（state/quota_log.json）|
| `/control/api/quota-now` | GET | 当前额度快照 |
| `/control/api/trigger/<action>` | POST | 触发 register/daily/anki-restart/ankiweb-sync/switch-ai |
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

> **PDF reader 完整路由**：pdf_reader.py（url_prefix=`/pdf`）实际有约 30+ 路由，上表只列了核心几条。手写笔（`/api/ink`）、生词系统（`/api/vocab-*`、`/api/page-vocab-marks`、`/api/dict-quick`）、语法分析（`/api/grammar-*` 7 条）、句子翻译（`/api/translate-sentence`/`-config`/`-dismiss`）、异步草稿（`/api/snippets-to-async`+`/api/job-status`）等新功能未在此罗列，完整清单见 pdf_reader.py / [`pdf-reader.md`](pdf-reader.md)。

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

## webapp.service 配置

```ini
# /etc/systemd/system/webapp.service
[Unit]
Description=bwicarus.space Flask webapp
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/webapp
EnvironmentFile=/root/webapp/.env
ExecStart=/usr/bin/python3 /root/webapp/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**注意**：`app.py` 末尾 `if __name__ == "__main__": app.run(host="127.0.0.1", port=5000, threaded=True)` 是阻塞调用 —— 任何 `if __name__` 之后的代码**永远不会执行**。所以 `control.py` import 必须在 `if __name__` **之前**：

```python
# ... 所有 routes 定义

# 控制面板
from control import register_control
register_control(app)

if __name__ == "__main__":
    # threaded=True：慢的 AI 请求（语法分析/翻译走 claude_cli）不再阻塞其他请求
    app.run(host="127.0.0.1", port=5000, threaded=True)
```

> `webapp.service` 的 systemd unit 文件**未**纳入 `references/systemd/` 副本（那目录只有 anki / qa-server / xvfb / obsidian-sync / bwicarus-daily 等），上面的 ini 是手抄，无法跟真机 unit 对照。

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
