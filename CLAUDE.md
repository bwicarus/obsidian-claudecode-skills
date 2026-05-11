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
| `cmd_server_thread.py` | 9090 端口 HTTP server（取代旧 cmd_server.exe），iPad 快捷指令 POST `/run/newnote` 触发 register |
| `qa_browser.py` | 整合自 `launchers/截图问答.py`，浏览器跑本地 QA |
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
| Anki | exe_path / connect_url / ping / 启动 Anki |
| 笔记登记 | vault_path / 立即运行登记 / 刷新并上传网页 / 登记后自动上传开关 / 每日定时 / vault watcher |
| 任务监视 | 悬浮窗位置 / 鼠标穿透 / 远程触发（cmd_server 端口+密钥） |
| 截图问答 | 习题/错题子目录 / 浏览器路径 / 快捷键 |
| 高级 | 主项目根目录 + 7 个派生路径（默认从根派生，可单独覆盖）|

**与主项目 scripts 的关系**

客户端**调用**主项目脚本，不复制其逻辑。`register_notes.py` / `anki_status.py` / `review_priority.py` / `export_dashboard.py` / `export_history.py` 是主项目脚本，客户端通过 subprocess 调它们：

- 「立即运行登记新笔记」→ `register_notes.py`
- 「刷新并上传网页」→ `anki_status → review_priority → export_dashboard` 三步串跑 + 上传 dashboard.json，然后 `export_history` + 上传 history.json
- 默认登记后**不**自动上传（用户开关 `auto_upload_after_register` 默认 False，避免登记慢半小时）

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
| `/qa/` / `/qa/update` | 旧 relay endpoint，截图问答历史展示 |

数据：`/root/webapp/data/users/<username>/{dashboard,history,private}/` + `/root/webapp/data/{dashboard_template,history_template}/`

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
- 「Obsidian Anki 每日状态更新」每天 04:00 运行（`scripts/daily_anki_status.ps1`）
- 「Obsidian Headless Sync」登录时启动 sync daemon,持续后台运行
- 每日任务执行顺序：
  1. 确保 Obsidian Sync daemon 在跑(`Start-ScheduledTask`),启动 Anki(AnkiConnect 需要 GUI),等 90 秒
  2. **登记新笔记**（`register_notes.py`）：PDF 标注 / 图片标注 / 索引 / 关联 / Anki 制卡
  3. 更新 Anki 状态（`anki_status.py --all`）：写回 frontmatter 和 records
  4. 计算复习优先级（`review_priority.py`）：写回 frontmatter 和 records
  5. 推送仪表板（`export_dashboard.py` + scp）
