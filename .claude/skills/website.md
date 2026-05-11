# Skill: website

管理部署在 `bwicarus.space` 的个人网站。

## 触发方式
用户提到网站、部署页面、修改样式、添加新路由、更新仪表板等任务时加载。

---

## 服务器信息

| 项目 | 值 |
|------|-----|
| 地址 | `root@31.220.31.30` |
| 域名 | `bwicarus.space`（HTTPS，Let's Encrypt） |
| Web 服务器 | nginx（静态）+ Flask 5000（webapp）+ Flask 5002（stocks-webapp，独占） |
| Python | 3.10.12（`python3`） |
| Flask | 2.0.1，由 systemd `webapp.service` 管理 |
| SSH 认证 | 密钥已配置，无需密码 |

> **路径所有权**：本 skill 只负责 `/root/webapp/`（端口 5000，dashboard / qa / history / private）。
> `/root/stocks-webapp/`（端口 5002，独立 `stocks-webapp.service`）属用户独占，**绝不要改、删、scp 进入这个目录**，nginx 配置也不要动。

---

## 本地文件结构

```
C:\claude\
  webapp\                          ← Flask 应用源码
    app.py                         ← 路由定义、登录逻辑
    templates\
      login.html                   ← 登录页
  dashboard\                       ← 仪表板静态文件
    index.html                     ← 仪表板页面（手动 SCP）
    dashboard.json                 ← 由 export_dashboard.py 生成，不手动编辑
  scripts\
    export_dashboard.py            ← 聚合数据生成 dashboard.json
```

## 服务器文件结构

```
/var/www/html/                     ← nginx 公开静态根目录
  index.html                       ← 主页（直接在服务器编辑）

/root/webapp/                      ← Flask 应用
  app.py
  .env                             ← SECRET_KEY / PASSWORD_HASH（勿传给 AI）
  templates/
    login.html
  data/
    dashboard/
      index.html                   ← 仪表板页面
      dashboard.json               ← 每日自动推送
    private/                       ← 未来私人内容

/etc/nginx/sites-enabled/default   ← nginx 配置
/etc/systemd/system/webapp.service ← Flask 服务定义
```

---

## 已有页面

| URL | 需登录 | 本地源文件 | 服务器路径 |
|-----|--------|-----------|-----------|
| `https://bwicarus.space/` | 否 | （直接在服务器编辑） | `/var/www/html/index.html` |
| `https://bwicarus.space/login` | 否 | `webapp\templates\login.html` | `/root/webapp/templates/login.html` |
| `https://bwicarus.space/dashboard/` | **是** | `dashboard\index.html` | `/root/webapp/data/dashboard/index.html` |
| `https://bwicarus.space/private/` | **是** | （待建） | `/root/webapp/data/private/` |

---

## Flask 应用架构（app.py）

```
/login   GET/POST  → 登录页，验证后写 session
/logout  GET       → 清除 session，跳回登录
/dashboard/<path>  → send_from_directory /root/webapp/data/dashboard/
/dashboard/graph-settings.json  GET/POST → dashboard 图谱设置持久化（受 session 保护）
/private/<path>    → send_from_directory /root/webapp/data/private/
/history/<path>    → send_from_directory /root/webapp/data/history/
/qa/, /qa          → 渲染 qa.html（私有，登录后访问）
/qa/update  POST   → 接收外部相机/relay 推送（X-API-Key 鉴权）
```

`before_request` 钩子：访问 `/dashboard`、`/private`、`/history` 时若无 session 则跳转 `/login?next=<原路径>`。

Session 有效期 30 天（`permanent_session_lifetime`）。

### 添加新的受保护路径（标准流程）

**① 在 `app.py` 加路由：**
```python
@app.route("/newpage/")
@app.route("/newpage/<path:filename>")
def newpage(filename="index.html"):
    return send_from_directory("/root/webapp/data/newpage", filename)
```

**② 在 `app.py` 的 `PROTECTED` 元组加路径：**
```python
PROTECTED = ("/dashboard", "/private", "/newpage")
```

**③ 在 nginx 配置加反代：**
```nginx
location /newpage { proxy_pass http://127.0.0.1:5000; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-Proto $scheme; }
```

**④ 部署：**
```powershell
scp "C:\claude\webapp\app.py" root@31.220.31.30:/root/webapp/
ssh root@31.220.31.30 "nginx -t && systemctl reload nginx && systemctl restart webapp"
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

**⑤ 推送到服务器：**
```powershell
scp "C:\claude\dashboard\index.html" "C:\claude\dashboard\dashboard.json" root@31.220.31.30:/root/webapp/data/dashboard/
```

### 登录页改动
```powershell
scp "C:\claude\webapp\templates\login.html" root@31.220.31.30:/root/webapp/templates/
ssh root@31.220.31.30 "systemctl restart webapp"
```

### Flask app.py 改动
```powershell
scp "C:\claude\webapp\app.py" root@31.220.31.30:/root/webapp/
ssh root@31.220.31.30 "systemctl restart webapp"
```

### nginx 配置改动
```bash
ssh root@31.220.31.30 "nginx -t && systemctl reload nginx"
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
