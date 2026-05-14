# Obsidian 笔记管理项目

## Vault 位置
- Vault 根目录：`C:\obsidian\`
- 本项目目录：`C:\claude\`（管理脚本和配置，不含笔记）

## Sync
- 用 `obsidian-headless`(npm 包)做后台 sync,不依赖 Obsidian GUI
- Windows 计划任务「Obsidian Headless Sync」开机/登录自启,启动器在 `bin/start_obsidian_sync.ps1`,日志写到 `state/logs/obsidian_sync.log`
- 远端 vault:Obsidian Sync(Plus 订阅),Asia 区
- iPad 端写完几秒内 Windows 端就能看到,没有等待窗口期

## 学习方向
自学：英语、日语、计算机科学（当前重心：大学数学、大学物理）

## 技能 (Skills)
| 指令 | 功能 |
|------|------|
| `/登记新笔记` | 自动扫描新增/已修改笔记，执行完整流程：PDF 标注 → 摘要入索引 → 关联链接 → Anki 制卡 |
| `/summarize` | 对指定笔记生成关键词+摘要，写入知识索引 |
| `/connect` | 在知识索引中查找关联笔记，在原文末尾插入 Obsidian 链接 |
| `/pdf-mark` | 提取 PDF 指定页面和像素区域的内容（文字层或 OCR） |
| `/img-mark` | 对笔记中的图片链接生成内容描述标注（脚本预处理 + Claude Code 读图分析 + 脚本写回） |
| `/anki` | 对指定笔记或目录生成 Anki 卡片：agent 判断制卡，脚本同步到 Anki |
| `/website` | 管理个人网站 bwicarus.space：部署页面、更新仪表板、nginx 配置 |
| `/openai-cli-chat` | Codex CLI 多轮对话实现模板：`codex_call()` + `--image` 附图 + 应用层历史拼接 |

## 关键文件

**索引**
- `index/knowledge-index.md` — 主索引（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超 30 条时自动拆分）

**参考规范**（各 skill 按需加载）
- `references/index-format.md` / `vault-structure.md` / `obsidian-syntax.md`
- `references/anki-selection-rules.md` / `anki-card-format.md`
- `references/pdf-annotation-format.md`
- `references/ipad-remote-qa.md` — iPad 远程截图问答操作指南（链路、快捷指令、URL 模板、排错）
- `references/linux-server-migration.md` — 2026-05-14 服务器迁移完整指南（systemd 服务、路径、踩坑速查）
- `references/systemd/*.service|*.timer` — 服务器 systemd unit 文件副本（xvfb-99 / anki-headless / obsidian-sync / qa-server / bwicarus-daily.service|timer）
- `references/server-side-claude-code.md` — 在服务器侧用 Claude Code 继续这个项目（tmux 会话、memory 同步、跨机器切换）
- `references/client-exe-development.md` — bwicarus-client.exe 开发指南（launcher + core 架构、build_core.py、deploy_core.sh、版本号管理、跟 webapp 控制面板的职责对照）
- `references/webapp-development.md` — bwicarus.space webapp 开发指南（Flask routes 清单、鉴权、SQLite schema、模板两套主题、nginx 反代、部署流程、改 control.py 流程）
- `references/ipad-switch-to-server.md` — iPad 快捷指令切换到服务器的完整步骤
- `references/prompts/*.md` — AI prompt 模板（analyze / analyze_excalidraw / find_related / anki_cards / image_describe）

**脚本**
- `scripts/config.py` — 集中管理路径和常量（其他脚本从这里读）
- `scripts/register_notes.py` — 笔记登记编排（批量或单步）；CLI: `--note PATH --only {pdf,img,summarize,connect,anki,all}`
- `scripts/summarize_note.py` / `connect_note.py` / `annotate_note.py` / `annotate_images.py` / `pdf_extract.py`
- `scripts/pending_notes.py` — 扫描待登记笔记（新增/已修改，命名规则 `[0-9A-Fa-f]{3}-*.md`）
- `scripts/anki_from_note.py` / `anki_status.py` / `daily_anki_status.ps1`
- `scripts/review_priority.py` — 知识图谱复习优先级（激活扩散 + Anki 薄弱度）
- `scripts/note_state.py` — 笔记内容哈希 + 失败追踪（连续失败 ≥3 次自动跳过）
- `scripts/backfill_back_links.py` — 一次性脚本，给存量笔记补全反向链接

**EXE 启动器**（双击运行，源码在 `launchers/`，编译产物在 `launchers/dist/`）
- `任务监视.exe` — 系统托盘 + 悬浮窗。开机启动后自动拉起 cmd_server / 截图问答 / relay；菜单可切换 AI 后端（Claude / GPT）
  **注：新架构下被 `_client/dist/bwicarus-client.exe` 替代，托盘 + 悬浮窗 + cmd_server 全部整合进去**
- `登记新笔记.exe` — 调用 `scripts/register_notes.py` 编排脚本，直接执行完整流程（不经 Claude CLI）
- `上传网页.exe` — 依次执行仪表板同步四步：Anki 状态更新 → 复习优先级 → 生成 dashboard.json → SCP 推送到服务器
  **注：被客户端的「刷新并上传网页」按钮替代（流程一致：anki_status → review_priority → export_dashboard → 上传 → export_history → 上传，POST 替代 SCP）**
- `cmd_server.exe` / `截图问答.exe` / `relay.exe` — 由任务监视统一启动，不直接双击
  **注：cmd_server.exe 因 PyInstaller 命名冲突 broken，且已整合进客户端为线程；截图问答整合进客户端 qa_browser 模块**

## bwicarus-client（多用户客户端）

源码：`_client/launcher/` + `_client/core/`。launcher 是 PyInstaller --onefile，core 是热更新 zip 包（每次启动自动拉服务器最新版）。

**架构两层**：

- **Launcher**（`bwicarus-client.exe`）— 大约 33MB，PyInstaller 单文件
  - 首次启动弹「选择数据保存位置」窗口，写指针文件 `%APPDATA%\bwicarus-client\datadir.txt`
  - 之后每次启动读 manifest 检查 core 版本，新版自动下载 + 解压 + 加载
- **Core**（`%LOCALAPPDATA%\bwicarus-client\core\<version>\`）— 业务逻辑 zip，可热更
  - 服务端 `static/client/core-<v>.zip` + `manifest.json`，build 脚本：`_client/build/build_core.py`

**关键模块**（`_client/core/`）

| 文件 | 职责 |
|---|---|
| `gui.py` | customtkinter 主窗口，7 个 tab + 滚动布局 + 日志折叠 |
| `wizard.py` | 首次启动 4 步配置向导：登录 / Obsidian / Anki / AI |
| `auth.py` | device-link OAuth：浏览器登录 → loopback callback → token 自动配置 |
| `api_client.py` | 服务端 HTTP（`/api/upload/<dataset>` 等），bearer token |
| `uploader.py` | `upload_dataset(client, dir, "dashboard"|"history")`，白名单 JSON+图片，**不传 HTML/JS/CSS** |
| `cmd_server_thread.py` | 9090 端口 HTTP server。路由：`/list`、`/run/<cmd>`（iPad 触发 register/upload）、`/qa`（iPad 截图注入，转发到 qa_browser daemon `/api/inject-image`）|
| `qa_browser.py` | 截图问答。两种入口：(a) 本机 ctrl+shift+q `launch()` 临时启动；(b) `start_server_daemon()` 常驻 0.0.0.0:9091 让 iPad 通过 Tailscale 直连完整对话页（cfg `qa_remote_daemon` 控制）|
| `runner.py` | `run_script(path, ..., python_exe)` subprocess 跑主项目脚本，所有 subprocess 自动 CREATE_NO_WINDOW |
| `paths.py` | 数据目录抽象：`app_dir()` 读 `BWICARUS_APP_DIR` / pointer / 默认；`derive_paths(project_root)` 从主项目根派生所有子路径 |
| `watcher.py` | watchdog 监听 vault，防抖+冷却后触发 register |
| `scheduler.py` | 每日定时任务（默认 04:00） |
| `floating_window.py` | 任务进度悬浮窗，半透明置顶，可拖动 / 鼠标穿透 |
| `tray.py` | pystray 托盘图标 + 菜单 |
| `hotkey.py` | 全局快捷键（默认 ctrl+shift+q 触发截图问答） |
| `startup.py` | 写 HKCU\\...\\Run 实现开机自启 |
| `ai_backends.py` | 5 个 AI 后端 adapter：claude_cli / codex_cli / claude_api / openai_api / ollama |

**主窗口 7 个 tab**

| Tab | 内容 |
|---|---|
| 基础 | server_url / api_token / 测试连接 / 一键登录 / 跑配置向导 / 数据目录显示 / 开机自启 |
| AI | 后端下拉 + settings（动态字段，secret 字段带 [粘贴][复制] 按钮） |
| Anki | exe_path / connect_url / ping / 启动 Anki / **AnkiConnect 不可达时自动重启** 开关 (`cfg.anki.auto_restart`) |
| 笔记登记 | vault_path / 立即运行登记 / **立即跑完整定时任务** / 刷新并上传网页 / 登记后自动上传开关 / 每日定时 + **完成后上传网页**子开关 / vault watcher |
| 任务监视 | 悬浮窗位置 / 鼠标穿透 / 远程触发（cmd_server 端口+密钥） |
| 截图问答 | 习题/错题子目录 / 浏览器路径 / 快捷键 / 远程访问 + 子开关「iPad 远程截图问答」(常驻 daemon :9091) |
| 高级 | 主项目根目录 + 7 个派生路径（默认从根派生，可单独覆盖）|

**与主项目 scripts 的关系**

客户端**调用**主项目脚本，不复制其逻辑。主项目脚本（`register_notes.py` / `anki_status.py` / `review_priority.py` / `build_review_deck.py` / `cleanup_orphans.py` / `export_dashboard.py` / `export_history.py`）通过 subprocess 调用。

**三个按钮职责清晰分开（0.9.32+）**：

| 按钮 / 触发 | 流程 | 含必复习计算？|
|---|---|---|
| 「立即运行登记新笔记」 | `register_notes.py` 单步 | 否 |
| 「刷新并上传网页」 | anki_status → review_priority → export_dashboard → upload dashboard → export_history → upload history | 否（只读 Anki）|
| **「立即跑完整定时任务」** / **凌晨定时** | ensure_alive → register → anki_status → review_priority → **build_review_deck** → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync | **是** |

**完整 daily 流程**由 `gui.py::_full_daily_pipeline` 实现，等价主项目 `daily_anki_status.ps1`。凌晨定时和「立即跑完整定时任务」按钮共用，仅 `force_restart` 入参不同：
- 凌晨：`force_restart = cfg.scheduled_register.wake_anki`（默认 True）
- 手动按钮：`force_restart = cfg.anki.auto_restart`（默认 False）

**关键 cfg 开关**：
- `auto_upload_after_register`（默认 False）— 「立即登记」结束后是否自动接「刷新并上传网页」
- `scheduled_register.upload_after`（默认 False）— 凌晨 / 「立即跑完整定时任务」结束后是否 upload dashboard + history
- `scheduled_register.wake_anki`（默认 True）— 凌晨触发时是否 force_restart Anki（杀僵尸 + 重启 + 轮询 ≤180s）
- `anki.auto_restart`（默认 False）— AnkiConnect 不可达时手动按钮场景是否自动 force_restart Anki

watcher 触发只调 `_run_register`（register 单步），**不**跑完整 daily。

**服务端**

`_server_deploy/` — Flask 多用户应用：

| 路由 | 用途 |
|---|---|
| `/login` / `/logout` / `/register` | 邀请码注册 + session 登录 |
| `/profile/` | 改密码 / 管理 API token（已生成的 token 可随时显示+复制）/ 下载客户端 .exe |
| `/admin/` | 邀请码管理 / 用户列表 |
| `/dashboard/` / `/history/` / `/private/` | 用户的私有目录，缺失文件回落到 `dashboard_template/` / `history_template/` |
| `/api/upload/<dataset>` | 客户端 POST 上传（bearer token），admin 用户上传的 HTML/CSS/JS 自动同步到 template |
| `/auth/device-link` | 客户端登录回调：登录后跳到 loopback `http://127.0.0.1:PORT/auth-cb?token=...` |
| ~~`/qa/` / `/qa/update`~~ | 已废弃。nginx `/qa` 反代 2026-05-11 删，路由保留但外部不可达。iPad 截图问答改走客户端本机 daemon（见下节）|

数据：`/root/webapp/data/users/<username>/{dashboard,history,private}/` + `/root/webapp/data/{dashboard_template,history_template}/`

## iPad 远程截图问答

iPad 通过 Tailscale 直接访问本机 qa_browser daemon —— **跟本地按 `ctrl+shift+q` 看到的页面 100% 一致**（同一份 HTML、同一组 API、同一个进程内的 state / SQLite / vault 访问）。**不经过 bwicarus.space 公网服务端**。

**架构**：

```
iPad 拍照 → POST cmd_server :9090/qa?key=<API_KEY>  body 含 base64 截图
              ↓
           cmd_server 转发到 daemon /api/inject-image
              ↓
           qa_browser daemon (0.0.0.0:9091) 解码（含 HEIC→PNG）→ 写文件 → 注入 state → session.reset()

iPad 浏览器 → http://<Tailscale-IP>:9091   完整 qa_browser HTML + 所有 /api/*
              （拿到截图、输入问题、Markdown + MathJax 渲染、保存到 vault、历史侧栏、AI 后端切换等全部功能）
```

**开关**（GUI 截图问答 Tab）：

```
远程访问 [✓] 允许局域网其他设备访问对话窗（监听 0.0.0.0）
            [✓] └─ iPad 远程截图问答（常驻 daemon :9091，cmd_server :9090 /qa 注入截图）
```

cfg 字段 `qa_remote_access`（父）+ `qa_remote_daemon`（子）。父开关关 → 子开关自动 disable。

**iPad 端配置**：见 `references/ipad-remote-qa.md`。要点：
- 拍照前快捷指令加「转换图像 → JPEG」（HEIC 也行，daemon 自动转 PNG，但 JPEG 体积更小）
- API key 在 `%LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt`
- 浏览器 URL：`http://<Tailscale-IP>:9091`（直连 daemon，不经 cmd_server）

**state 共享隐患**：本机按 `ctrl+shift+q` 启的临时 server 跟 daemon **共用** 模块级 `state` 字典。同时操作会串扰（截图覆盖、session 混合）。单人多端通常不撞，遇到问题先各自结束当前会话再开新的。

## 服务器侧自动化（bwicarus.space Linux）

2026-05-14 起整套工作流也跑在 `bwicarus.space` 服务器上（Ubuntu 22.04，1 vCPU / 3.8GB RAM）。**长期目标是关掉 Windows**，所有功能由服务器 + web 控制面板代替。完整指南见 [`references/linux-server-migration.md`](references/linux-server-migration.md)。**在服务器侧继续这个项目**见 [`references/server-side-claude-code.md`](references/server-side-claude-code.md)（推荐 `tmux` + `cd /root/claude && claude` 模式）。

**服务器 Tailscale 接入**：服务器加入了用户的 tailnet（hostname `bwicarus-3`），IP 用 `ssh root@bwicarus.space 'tailscale ip -4'` 查（当前 `100.110.193.39`）。iPad 通过 Tailscale 私网访问 qa_browser / cmd_server，**不走公网**。OpenVPN Access Server 在 :914/:943 跟 Tailscale 共存（之前确认过不冲突）。

**iPad 端口入口（Tailscale 内）**：

| 用途 | 端点 |
|---|---|
| 浏览器看截图问答页面 | `http://<Tailscale-IP>:9091/` |
| POST 截图注入 | `http://<Tailscale-IP>:9090/qa?key=<KEY>`（key 在 `/root/claude/state/qa-server-data/cmd_server_key.txt`） |
| 触发 register / daily / ankiweb-sync | `http://<Tailscale-IP>:9090/run/<cmd>?key=<KEY>` |

**关键设施**：

| 项 | 路径 | 说明 |
|---|---|---|
| 主项目 | `/root/claude/` | git clone 自 GitHub，跟本机 `C:\claude\` 同步 |
| Vault | `/root/obsidian/` | obsidian-headless sync 拉，1175 笔记 |
| Anki | `/opt/anki-venv/` + `/root/.local/share/Anki2/User 1/` | aqt 25.2.7 + Xvfb 跑 GUI，5634 卡 |
| 环境变量 | `/root/claude/.env` + `/etc/profile.d/claude.sh` | `CLAUDE_PROJECT` / `OBSIDIAN_VAULT` / `APP_PYTHON` / `APP_CLAUDE` / `APP_CODEX` / `ANKI_CONNECT_URL` / `AI_SETTINGS_FILE` |
| **systemd 服务** | `/etc/systemd/system/` | `xvfb-99` + `anki-headless` + `obsidian-sync` + `qa-server` + `bwicarus-daily.timer` (04:00) + `tailscaled` + `webapp` |
| **控制面板** | `https://bwicarus.space/control/` | 替代 Windows 客户端 EXE。3-panel 布局：状态（系统+Daily）/ 操作（触发+日志）/ 设置（AI 后端+所有同步开关）+ 左侧滑出 drawer 含可编辑导航链接，需登录 |
| **qa-server daemon** | systemd `qa-server.service` | 跑 iPad 截图问答 daemon (`:9091`) + cmd_server (`:9090`)，复用 `_client/core/qa_browser.py` + `cmd_server_thread.py`，ExecStartPre sed 替换 jsdelivr CDN URL 为 `bwicarus.space/static/qa/` + 去掉 `--dangerously-skip-permissions` + 加 `--allowedTools Read`（这 3 个 patch 必须保留，git pull 覆盖后 service restart 时自动重新 patch）|
| **服务器侧配置** | `/root/claude/state/server-config.json` | 控制面板「设置」面板写入，所有 Windows EXE 客户端开关同步在此（sidebar_links 自定义链接、anki.auto_restart、auto_upload_after_register、scheduled_register.{wake_anki,upload_after}、qa_remote_daemon、qa_exercises_subdir、qa_wrong_subdir）|

**控制面板源码**（全部在 git，部署 = 纯 cp）：
- `_server_deploy/app.py` → 部署到 `/root/webapp/app.py`（含 `/api/nav-links` 路由、`register_control` 导入、`/control` 进 `PROTECTED_PREFIXES` / `NAV_INJECT_PREFIXES`）
- `_server_deploy/control.py` → 部署到 `/root/webapp/control.py`
- `_server_deploy/templates/control.html` → 部署到 `/root/webapp/templates/control.html`
- `_server_deploy/static/nav.js` → 部署到 `/var/www/html/static/nav.js`（全站通用左侧导航 + per-user 链接持久化）
- `nginx` 加 `location /control { proxy_pass http://127.0.0.1:5000; ... }`（在 nginx 配置里，不在 git）

**跨平台改动**（让脚本可在 Windows + Linux 跑）：
- `config.py` AI_SETTINGS_FILE 加 env 优先
- `ai_client.py` 改 import config，`_run_hidden` 跨平台，Linux 上 Claude CLI 不加 `--dangerously-skip-permissions`（root 禁用）
- `service_switch.py` 加 `WINDOWS = sys.platform == "win32"` 守卫
- `pending_notes.py` 路径从硬编码改 `from config import VAULT_ROOT`
- `scripts/daily_anki_status.py`（新增 Linux 版 daily 编排）

**Anki 25 headless 关键坑**（见 reference）：
- 必须 `QTWEBENGINE_DISABLE_SANDBOX=1` + `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox`（root 用户）
- profile 自动创建有 i18n backend 依赖问题，用 Python `ProfileManager._loadMeta + create("User 1")` 绕开
- obsidian-headless 必须 Node 22+（全局 `WebSocket` API）+ `npm rebuild better-sqlite3`
- 二进制名是 `ob`，子命令是 `sync-setup`（不是 `init`）

## 电源 / 屏幕守护

主项目客户端**不再**处理睡眠/屏幕策略——这事归独立项目 `C:\autoscreen\`（详见其 README）。

- 客户端跑着 ≠ 系统不睡（早期 0.9.23 的 antisleep 模块已撤回 0.9.24）
- 「合盖关屏 + 远程可达」由 autoscreen 用 `SetThreadExecutionState(ES_SYSTEM_REQUIRED)` 在内存里动态控制，不需要永久改 powercfg
- autoscreen 登录自启，托盘图标只剩 Pause/Resume 一个开关

## AI 后端
- 项目只有一个根目录 `C:\claude\`，AI 后端只是 `%LOCALAPPDATA%\截图问答\settings.json` 里的字段
- `scripts/ai_client.py` 的 `ask()` 每次调用都重新读取 settings，所以切换 AI 不需要重启服务
- 切换由 `service_switch.py switch <claude|gpt>` 完成，亦可在任务监视托盘菜单点选
  - `claude` → `{"backend": "auto-claude"}`，限流时降级到 Codex
  - `gpt` → `{"backend": "codex", "model": "gpt-5.5"}`，限流时降级到 Claude

重新编译方法：
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe --onefile --noconsole --distpath launchers/dist --workpath launchers/build --specpath launchers launchers\<脚本名>.py
```

**状态文件（仅脚本读写，不传给 AI）**
- `anki/records/*.json` — Anki 制卡记录（含 `section_hashes`：各节内容哈希，用于增量制卡）
- `state/note-states.json` — 各 skill 最后处理的内容哈希 + 失败追踪（`failure_count` / `last_error`）
- `state/active_tasks.json` — 任务追踪（task_tracker 写，任务监视读）
- `state/logs/ai_calls.log` — AI 调用日志（5MB 滚动）
- `state/backup/` — 每日 `daily_anki_status.ps1` 自动备份 7 天的 note-states 和 anki-records

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- `/登记新笔记` 自动扫描 vault 根目录符合命名规则的待处理笔记；流程中自动调用 `annotate_images.py` 处理笔记内图片；其余 skill 只对用户**明确指定**的文件操作
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名
- `anki/records/` 和 `state/note-states.json` 是脚本状态文件，只由脚本读写，**不要传给 AI**
- Anki 制卡按节（`#` 标题）追踪哈希；只有内容变动的节才送给 AI 判断，新卡追加到已有 record，旧卡不删除
- Anki 制卡默认不修改原 md；同步结果写入 `anki/records/`

## 自动化任务

**三套等价的"每日 04:00 任务"**（同一逻辑，三个运行环境）：

| 任务 | 运行环境 | 实现 |
|---|---|---|
| Windows 计划任务「Obsidian Anki 每日状态更新」 | 主项目本机 PowerShell | `scripts/daily_anki_status.ps1` |
| bwicarus-client 凌晨定时（0.9.32+） | 客户端进程 | `_client/core/gui.py::_full_daily_pipeline` |
| **服务器 systemd timer `bwicarus-daily.timer`**（2026-05-14+） | bwicarus.space VPS | `scripts/daily_anki_status.py` (Linux) |

两边都跑：`ensure_alive → register_notes → anki_status → review_priority → build_review_deck → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync`。

**Step 0 ensure_alive 行为**：
- ping AnkiConnect `/version` — 通则直接进入下一步
- 不通 + `force_restart=True` → `taskkill /F anki.exe` → 启动 Anki → 轮询上线 ≤ 180s
- 主项目 ps1 用 `Ensure-AnkiConnect` 函数（force_restart 始终 True），客户端用 `AnkiClient.ensure_alive`

**「Obsidian Headless Sync」**：登录时启动 sync daemon，持续后台运行（不在 daily 流程内）。

**state 备份**（仅主项目 ps1 做）：每天备份 `state/note-states.json` 和 `anki/records/` 到 `state/backup/`，保留 7 天。

**注意**：daily_anki_status.ps1 必须保持 **UTF-8 with BOM**（Windows PowerShell 5.1 调 `-File X.ps1` 默认按 GBK 解码无 BOM 中文 → "字符串缺少终止符"）。Edit / Write 修改后立刻补 BOM。
