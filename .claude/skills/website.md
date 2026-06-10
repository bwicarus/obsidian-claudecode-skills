# Skill: website

管理部署在 `bwicarus.space` 的个人网站。

## 触发方式
用户提到网站、部署页面、修改样式、添加新路由、更新仪表板等任务时加载。

> ⚠️ **2026-05-14 起 webapp 已迁多用户架构**：真实源码在 `_server_deploy/`（repo 里的 `webapp/` 目录是迁移前的单用户旧版，已不再部署）。权威细节看 `references/webapp-development.md`（routes 清单 / 鉴权 / SQLite schema / nginx / 部署流程）+ `references/linux-server-migration.md`。下文已按现状修订，但新增页面（/pdf /insights /skilltree /control /fitness 等）以 reference 为准。

---

## 服务器信息

| 项目 | 值 |
|------|-----|
| 地址 | `root@31.220.31.30`（VPS） |
| 域名 | `bwicarus.space`（HTTPS，Let's Encrypt） |
| Web 服务器 | nginx（静态+反代）+ webapp `127.0.0.1:5000`（Linux 上是 gunicorn）+ Flask 5002（stocks-webapp，独占） |
| Python | 3.10.12（`python3`） |
| webapp | 多用户 Flask（源码 `_server_deploy/app.py`），systemd `webapp.service` 管理 |
| SSH 认证 | 密钥已配置，无需密码 |

> ⏸ **VPS 自 2026-06-10 起暂停**：自动化单元已 disable，只剩公网 `webapp.service` 在跑，代码停在 2026-05-28（改动须先 git pull + 重部署）。**当前主力实例是 Pi**（webapp 代码在 `/home/bwicarus/webapp/`，入口 `https://bwicarus.taile44d0c.ts.net`，nginx 配置独立于 VPS 版）。

> **路径所有权**：本 skill 只负责 `/root/webapp/`（端口 5000，dashboard / qa / history / private）。
> `/root/stocks-webapp/`（端口 5002，独立 `stocks-webapp.service`）属用户独占，**绝不要改、删、scp 进入这个目录**，nginx 配置也不要动。

---

## 本地文件结构

```
C:\claude\
  _server_deploy\                  ← Flask 多用户应用源码（部署 = 纯 cp 到 /root/webapp/）
    app.py                         ← 路由、登录、邀请码注册、/api/upload
    control.py / skilltree.py / pdf_reader.py / insights.py / fitness*.py ...
    templates\                     ← login / control / skilltree / pdf / fitness 模板
    nginx\bwicarus.conf            ← 仅 VPS 的 nginx 配置（Pi 的独立，绝不可覆盖）
  webapp\                          ← ⚠️ 迁移前单用户旧版，已不再部署，仅存档
  dashboard\                       ← 仪表板静态文件
    index.html                     ← 仪表板页面
    dashboard.json                 ← 由 export_dashboard.py 生成，不手动编辑
  scripts\
    export_dashboard.py            ← 聚合数据生成 dashboard.json
```

## 服务器文件结构

```
/var/www/html/                     ← nginx 公开静态根目录
  index.html                       ← 主页（直接在服务器编辑）
  static/                          ← nav.js / client 安装包 / qa 静态资源

/root/webapp/                      ← Flask 应用（Pi 在 /home/bwicarus/webapp/）
  app.py + control.py + ...
  .env                             ← SECRET_KEY 等（勿传给 AI）
  templates/
  data/
    users/<username>/{dashboard,history,private}/   ← 各用户私有数据
    dashboard_template/ history_template/           ← 用户目录缺文件时的共享回落

/etc/nginx/sites-enabled/default   ← VPS nginx 配置（Pi: /etc/nginx/sites-available/bwicarus，结构不同）
/etc/systemd/system/webapp.service ← Flask 服务定义（gunicorn 127.0.0.1:5000）
```

---

## 已有页面

| URL | 需登录 | 本地源文件 | 服务器路径 |
|-----|--------|-----------|-----------|
| `https://bwicarus.space/` | 否 | （直接在服务器编辑） | `/var/www/html/index.html` |
| `https://bwicarus.space/login` | 否 | `_server_deploy\templates\login.html` | `/root/webapp/templates/login.html` |
| `https://bwicarus.space/dashboard/` | **是** | `dashboard\index.html` | `/root/webapp/data/users/<user>/dashboard/`（缺文件回落 `data/dashboard_template/`） |
| `https://bwicarus.space/private/` | **是** | — | `/root/webapp/data/users/<user>/private/` |

其余页面（`/control` `/skilltree` `/pdf` `/insights` `/profile` `/admin` `/private/fitness` 等）见 `references/webapp-development.md` 的 routes 清单。

---

## Flask 应用架构（`_server_deploy/app.py`，多用户版）

```
/login /logout /register      → 邀请码注册 + session 登录（SQLite users 表，不再是单用户 PASSWORD_HASH）
/profile/                     → 改密码 / API token 管理 / 下载客户端
/admin/                       → 邀请码管理 / 用户列表
/dashboard/ /history/ /private/ → data/users/<username>/<dataset>/；dashboard、history 缺文件
                                  回落共享模板 data/<dataset>_template/（_serve_user，app.py:519）
/api/upload/<dataset>  POST   → 客户端上传（bearer token）；admin 上传的 HTML/CSS/JS 同步到模板
/auth/device-link             → 客户端 OAuth 登录回调
/qa/ /qa/update               → 已废弃：路由保留但 nginx /qa 反代 2026-05-11 已删，外部不可达
```

`before_request` 钩子按 `PROTECTED_PREFIXES`（app.py:334）拦截：
```python
PROTECTED_PREFIXES = ("/dashboard", "/private", "/history", "/qa", "/profile", "/admin", "/auth", "/control", "/pdf", "/insights")
```

Session 有效期 30 天（`permanent_session_lifetime`）。

### 添加新的受保护路径（标准流程）

**① 在 `_server_deploy/app.py` 加路由**（用户私有数据用 `_serve_user` 模式，普通页直接 `render_template`）。

**② 在 `PROTECTED_PREFIXES` 元组加前缀**；如要全站导航注入，另看 `NAV_INJECT_PREFIXES`。

**③ nginx 加反代 location：**
- VPS：改 `_server_deploy/nginx/bwicarus.conf`，部署到 `/etc/nginx/sites-enabled/default`
- Pi：**手工 patch** `/etc/nginx/sites-available/bwicarus` 对应 server 块（Tailscale 证书配置，**绝不可用 git 版 cp 覆盖**）

**④ 部署（源码全在 git，部署 = 纯 cp）：**
```bash
# 服务器上（VPS=/root，Pi=/home/bwicarus）
git -C <项目根> pull
cp <项目根>/_server_deploy/app.py <webapp目录>/app.py
sudo systemctl restart webapp
sudo nginx -t && sudo systemctl restart nginx   # 新增 location 块 reload 可能不完全生效，用 restart
```

---

## 部署命令速查

### 仪表板同步（含 Anki 状态更新）

**必须按顺序执行，不可跳过 Anki 更新步骤。**

**① 检测 Anki 和 Obsidian 是否已在运行：**
```powershell
# 检查 Obsidian 进程
$obsidianRunning = (Get-Process -Name "Obsidian" -ErrorAction SilentlyContinue) -ne $null
# 检查 AnkiConnect
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8765" -Method Post `
        -Body '{"action":"version","version":6}' -ContentType "application/json" -TimeoutSec 3
    $ankiRunning = $r.error -eq $null
} catch { $ankiRunning = $false }
Write-Host "Obsidian: $obsidianRunning  Anki: $ankiRunning"
```
- 两者均为 `True` → 下一步使用 `--wait-seconds 0`（直接运行，不等待）
- 任意一个为 `False` → 下一步使用 `--wait-seconds 120`（允许自动启动并等待）

**② 更新所有笔记的 Anki 学习状态（写回 frontmatter 和 record）：**
```powershell
# 根据①的结果选择参数
& "C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe" "C:\claude\scripts\anki_status.py" --all --write-frontmatter --write-record --wait-seconds <0 或 120>
```

**③ 计算复习优先级（写回 frontmatter 和 record）：**
```powershell
& "C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe" "C:\claude\scripts\review_priority.py" --write-frontmatter --write-record
```

**④ 重新生成 dashboard.json（含最新 Anki 状态和优先级）：**
```powershell
& "C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe" "C:\claude\scripts\export_dashboard.py"
```

**⑤ 推送到服务器（三种等价入口，⚠️ 旧目标 `/root/webapp/data/dashboard/` 已没人读，别再 scp 过去）：**
- **Windows 客户端**：「刷新并上传网页」按钮 → `POST /api/upload/dashboard`（bearer token，落到 `data/users/<user>/dashboard/`）
- **服务器侧自动**：daily（01:00）和 `anki-sync-refresh.timer`（15min）跑完 export 后直接 cp 到 `<webapp>/data/users/bwicarus/dashboard/`（`WEBAPP_DASHBOARD_DIR`，见 `scripts/daily_anki_status.py` / `anki_sync_refresh.py`）
- **手动等价**（服务器上）：`cp dashboard/{dashboard.json,index.html} <webapp>/data/users/bwicarus/dashboard/`

### 登录页 / Flask 代码改动（源码全在 git，部署 = 纯 cp）
```bash
# 本地改 _server_deploy/... → git push；然后在服务器上：
git -C <项目根> pull
cp <项目根>/_server_deploy/templates/login.html <webapp目录>/templates/   # 或 app.py / control.py 等
sudo systemctl restart webapp
```

### nginx 配置改动
```bash
# VPS：改 _server_deploy/nginx/bwicarus.conf → cp 到 /etc/nginx/sites-enabled/default
# Pi：手工 patch /etc/nginx/sites-available/bwicarus（绝不可 cp 覆盖）
sudo nginx -t && sudo systemctl restart nginx   # 新增 location 用 restart，reload 可能不完全生效
```

### 新增公开静态页
```powershell
scp "本地\page.html" root@31.220.31.30:/var/www/html/
```

---

## Flask 服务管理

```bash
ssh root@31.220.31.30 "systemctl status webapp"   # 查看状态
ssh root@31.220.31.30 "systemctl restart webapp"  # 重启
ssh root@31.220.31.30 "journalctl -u webapp -n 30 --no-pager"  # 查日志
```

---

## 页面设计规范（保持一致性）

现有页面使用以下 CSS 变量：

```css
--bg:         #f0f2f5   /* 页面背景 */
--card:       #ffffff   /* 卡片背景 */
--text:       #1a1a1a   /* 主文字 */
--muted:      #6b7280   /* 次要文字 */
--border:     #e5e7eb   /* 分割线 */
--new:        #94a3b8   /* Anki 新卡 */
--learning:   #f59e0b   /* 学习中 */
--review:     #10b981   /* 复习 */
--relearning: #ef4444   /* 重学 */
--activation: #3b82f6   /* 激活度（蓝）*/
--weakness:   #f97316   /* 薄弱度（橙）*/
```

字体：`system-ui, -apple-system, sans-serif`
圆角：卡片 `12px`，按钮 `8px`，标签 `4-8px`
主按钮：`background: #1a1a1a; color: #fff`

---

## 注意事项
- `.env` 含密钥，不要传给 AI，不要上传 git
- `dashboard.json` 由脚本生成，不手动编辑
- nginx 的 `proxy_set_header` 行中 `$host` 等变量在 shell heredoc 中容易被展开，用 Python 写入配置或单引号包裹
