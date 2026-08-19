# Linux 服务器迁移指南（2026-05-14）

> ⏸ **状态注记（2026-06-10）：VPS 已暂停。** 自动化单元（xvfb-99 / anki-headless / obsidian-sync / qa-server / bwicarus-daily.timer）已 `systemctl disable`（重启也不复活），只保留公网 `webapp.service`；代码停在 2026-05-28（落后 main），**重新启用前必须先 `git pull` + 重部署**。当前唯一活跃实例是 **Pi**（部署见 [`raspberry-pi-deployment.md`](raspberry-pi-deployment.md)）。⚠ VPS 的 OS hostname 实际也是 `bwicarus`（跟 Pi 撞名，Tailscale 设备名才是 `bwicarus-3`），**判定环境用路径**（`/root/claude` = VPS，`/home/bwicarus/claude` = Pi），别用 hostname。本文其余内容保留作 VPS 部署/踩坑参考——操作类段落（systemd / 控制面板 / daily 验证）描述的运行态现在在 Pi 上，VPS 上同款命令仅在重新启用后有效。

把整套 Windows 工作流（vault + Anki + 笔记登记 + 仪表板）搬到 `bwicarus.space` 服务器，目标：Windows 可以关机，所有功能由服务器和 web 控制面板代替。

## 服务器现状

- Ubuntu 22.04.5 / 2 vCPU / 7.8GB RAM（迁移当时 1 vCPU / 3.8GB，后升配）/ 39GB 可用磁盘
- 已跑：nginx + Flask webapp (:5000) + OpenVPN Access Server；Anki + obsidian-headless 等自动化 2026-06-10 起已停（见顶部注记）
- 时区：Asia/Shanghai (CST，比本机 JST 早 1h)

## 关键路径

| 路径 | 内容 |
|---|---|
| `/root/claude/` | 主项目（git clone 自 GitHub），等同本机 `C:\claude\` |
| `/root/obsidian/` | vault（obsidian-headless 同步），等同本机 `C:\obsidian\` |
| `/opt/anki-venv/` | Anki Python venv（605MB），含 aqt 25.2.7 + PyQt6 + better-sqlite3 |
| `/usr/lib/node_modules/obsidian-headless/` | obsidian-headless 0.0.8 npm 全局包 |
| `/root/claude/.env` | 项目环境变量（systemd EnvironmentFile） |
| `/etc/profile.d/claude.sh` | 同 .env，给交互式 shell 用 |
| `/root/.local/share/Anki2/User 1/` | Anki profile + collection.anki2（16MB，5634 卡）|
| `/root/.claude/.credentials.json` | Claude CLI token（已登录） |
| `/root/.codex/auth.json` | Codex CLI token（已登录） |
| `/root/state/...` | 旧的（已迁到 `/root/claude/state/`） |

## 环境变量（`.env`）

```
CLAUDE_PROJECT=/root/claude
OBSIDIAN_VAULT=/root/obsidian
APP_PYTHON=/usr/bin/python3
APP_PYTHONW=/usr/bin/python3
APP_CLAUDE=/root/.local/bin/claude   # 2026-06-10 起 native installer;npm 全局版已卸(自更新替换不了 /usr/bin → 永远旧版)
APP_CODEX=/usr/bin/codex
ANKI_CONNECT_URL=http://127.0.0.1:8765
AI_SETTINGS_FILE=/root/claude/state/ai-settings.json
WEBAPP_DASHBOARD_DIR=/root/webapp/data/users/bwicarus/dashboard
QA_PUBLIC_URL=https://bwicarus.space/qa
```
（Pi 实例把 `/root` 换成 `/home/bwicarus`，`QA_PUBLIC_URL` 用 Tailscale 地址。`WEBAPP_DASHBOARD_DIR` 由 daily 的 `deploy_dashboard` 读取，缺省落到硬编码 VPS 路径。）

## systemd services

| Unit | 干啥 |
|---|---|
| `xvfb-99.service` | 启 Xvfb :99（Anki GUI 的虚拟显示） |
| `anki-headless.service` | 启 Anki + AnkiConnect 8765（依赖 xvfb-99） |
| `obsidian-sync.service` | `ob sync --continuous` 持续同步 vault |
| `bwicarus-daily.service` | 一次性跑 daily 流程（`scripts/daily_anki_status.py`） |
| `bwicarus-daily.timer` | 每天 **01:00** 触发 daily.service（`Persistent=true`，含 missed-run 补跑；原 04:00，73d8eb6 起提前到用户不用 AI 的空闲时段）|
| `anki-sync-refresh.service`/`.timer` | review 数据变动时 AnkiWeb sync + dashboard refresh（后续新增） |
| `bwicarus-quick-sync.service`/`.timer` | vault 快速 sync + cleanup + KG prune，无 AI / 无 Anki（后续新增） |
| `book-ocr.service` + `book-ocr-watchdog.service`/`.timer` | Mokuro 日文教材 OCR 后台（后续新增） |
| `webapp.service` | Flask webapp（VPS 曾用 dev server；Pi 现为 gunicorn，见 `webapp-development.md`） |
| `qa-server.service` | iPad 截图问答 daemon :9091 + cmd_server :9090 |

所有 unit 文件源码在 `references/systemd/`（OCR / quick-sync / sync-refresh 这几套是 2026-05-14 之后新增的，文件都已纳入该目录）。`systemctl enable` 后开机自启。

> ⚠ **VPS 代码/部署冻结在 2026-05-28**，所以 VPS 上**没有**之后新增的这几套 unit（都只在 Pi 跑，副本仍在 `references/systemd/`）：`bwicarus-backup.{service,timer}`（每日 03:30 备份）、`yolo-figures.{service,timer}`（6h 闲时 DocLayout-YOLO 框图）、`figures-describe.{service,timer}`（夜间裁图描述）、`push-big-files.{service,timer}`（仅 Pi，4h 推 >200MB 文件到 PC）、`concept-graph.{service,timer}`（02:30 概念网流水线，已从 daily 拆出）、`mcp-server.service`（MCP 门面 `--http 8766`）、`voice-rt.service`（豆包实时 S2S 中继）、`rbi-server.service`（Pi 真 Chrome + rrweb 推 iPad）、`reader-context-push.service`（inotify + SSH 推阅读器上下文快照到 Windows PC）。完整清单以 Pi 为准，见 [`raspberry-pi-deployment.md`](raspberry-pi-deployment.md)。

## webapp 控制面板

URL：`https://bwicarus.space/control/`（需登录）

API：
- `GET /control/api/status` → AnkiConnect / 服务状态 / AI backend / last_run.json
- `POST /control/api/trigger/<action>` → register / daily / anki-restart / ankiweb-sync / switch-ai

实现：`/root/webapp/control.py` + `templates/control.html`，由 `app.py` 末尾 `register_control(app)` 注册路由。

## 踩坑速查

### Anki 25 在 Linux headless 上的关键坑

1. **`-l en -p "User 1"` 不会自动创建 profile**（Anki 25 行为变了）。要先用 Python 建：
   ```python
   from aqt.profiles import ProfileManager
   pm = ProfileManager(base="/root/.local/share/Anki2")
   pm._loadMeta()  # 不调 setupMeta（会触发 _ensureProfile + tr i18n 抛 NoneType）
   pm.create("User 1")  # SQL only，安全
   ```
   ⚠️ `pm.profiles()` 内部也调 `_ensureProfile`！要用 `pm.db.execute("SELECT name FROM profiles WHERE name != '_global'")`。

2. **`Running as root without --no-sandbox is not supported`** —— QtWebEngine 拒绝 root 启动。两个 env 缺一不可：
   ```
   QTWEBENGINE_DISABLE_SANDBOX=1
   QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox
   ```

3. **AnkiWeb full download** API：`col.full_download` 已废弃，改用：
   ```python
   col.close_for_full_sync()
   col.full_upload_or_download(auth=auth, server_usn=0, upload=False)
   ```
   `result.required=3` 表示 FULL_DOWNLOAD（不是 FULL_SYNC=2）。还要更新 `auth.endpoint = result.new_endpoint`。

4. **AnkiConnect 插件**直接 `git clone https://github.com/FooSoft/anki-connect.git` + 拷贝 `plugin/*` 到 `/root/.local/share/Anki2/addons21/2055492159/`。

### obsidian-headless 在 Linux 上的坑

1. **二进制是 `ob`**，不是 `obsidian-headless`。子命令：`login` / `logout` / `sync-list-remote` / `sync-list-local` / `sync-create-remote` / **`sync-setup`** / `sync` / `sync-config` 等。**不是 `init`**！
2. **prebuild better-sqlite3 跟 Node 版本不匹配**：obsidian-headless 0.0.8 用全局 `WebSocket`（Node 21+），但 prebuild binary 给 Node 22 (`NODE_MODULE_VERSION 127`)。
   - 服务器装了系统 Node 20 + nvm Node 22。系统 Node 20 缺 WebSocket 启动崩。
   - 解决：用 nvm Node 22 跑 `npm rebuild better-sqlite3`（在 obsidian-headless 包目录）+ 改 cli.js shebang 写死 `/root/.nvm/versions/node/v22.13.1/bin/node`。
3. **`ob sync`（不带 `--continuous`）单次同步就退出**，第一次可能没拉完所有内容。要重跑或者直接用 `--continuous`。

### Claude CLI / Codex CLI 在 root 上的坑

1. **Claude CLI 拒绝 root 用 `--dangerously-skip-permissions`**：`cannot be used with root/sudo privileges for security reasons`。
   - 修复：`ai_client.py::claude_raw` 在 Linux 上不加这个 flag（用默认权限模式，简单 prompt 不会触发提示）。
2. **`codex exec "prompt"` 必须在 git repo 内跑**，否则要 `--skip-git-repo-check`。`stdin` 要 `</dev/null` 否则触发"读 stdin 补充输入"模式卡住。

### ssh 长命令 + 后台进程的坑

1. `ssh server '...'` 内启动 background 进程后 ssh **不会立即返回**，会等 stdin/stdout fd。
2. 解决：写 shell 脚本到本机 → `scp` 上去 → `ssh server 'bash /tmp/script.sh'`。或者 `setsid` / `systemd-run` 让进程完全 detach。
3. 一次 ssh 跑大量命令时，输出经常被截断。把关键输出存 `/tmp/log` 然后 `tail -c 1500` 比 `head` 更稳。

## 跨平台代码改动总览

| 文件 | 改动 |
|---|---|
| `scripts/config.py` | `AI_SETTINGS_FILE` 支持 `AI_SETTINGS_FILE` env 优先 |
| `scripts/ai_client.py` | 用 config 模块代替 Windows 硬编码；`_run_hidden` 跨平台；Linux 上 Claude 不加 `--dangerously-skip-permissions` |
| `scripts/service_switch.py` | 加 `WINDOWS = sys.platform == "win32"` 守卫，Linux 上只跑切换逻辑（PowerShell / SSH tunnel / 服务管理跳过） |
| `scripts/pending_notes.py` | 路径从硬编码改用 `config.VAULT_ROOT` |
| `scripts/daily_anki_status.py` | **新增**，Linux 版 daily 编排（等价 ps1） |

## 现状（2026-05-14）

- ✅ 所有 systemd service 跑通：xvfb-99 / anki-headless / obsidian-sync / bwicarus-daily.timer
- ✅ AnkiConnect 在 :8765 监听，version 6
- ✅ Vault 完整同步：1175 笔记 / 175 图片 / 18 PDF / 1.4GB
- ✅ Anki collection 完整：5634 卡片 / 3258 笔记 / 33 个 deck
- ✅ Claude CLI + Codex CLI 都可调用
- ✅ webapp /control/ 控制面板上线
- ✅ Daily timer 设定 04:00 (Asia/Shanghai)

## Windows 端怎么办

服务器功能确认稳定 1-2 周后：
- 可以关掉 Windows 客户端 / 关掉 Windows 计划任务
- iPad 接入 endpoint 改为服务器（已通过 Tailscale 完成，见 `ipad-switch-to-server.md`）
- 本机做"备份角色"或者完全退役

## 2026-05-14 晚期补充：iPad / 控制面板完整上线

### Tailscale 接入
- 服务器加入 tailnet（`tailscale up --authkey=...` 一次性，**Tailscale 设备名** `bwicarus-3`；⚠ OS hostname 实际是 `bwicarus`，跟 Pi 撞名，见顶部注记）
- 当前 IP：`100.110.193.39`（用 `tailscale ip -4` 查）
- 跟现有 OpenVPN Access Server 不冲突（端口/interface/网段都错开）

### iPad 截图问答 daemon 上线
- `_server_deploy/qa_server.py` 入口 + `qa-server.service` systemd unit
- 复用 `_client/core/qa_browser.py` + `cmd_server_thread.py`
- 监听 `0.0.0.0:9091` (qa_browser daemon，iPad 直连)
- 监听 `0.0.0.0:9090` (cmd_server，iPad POST 注入截图 + 触发命令)
- API key: `/root/claude/state/qa-server-data/cmd_server_key.txt`
- BWICARUS_APP_DIR env 让客户端 `paths.py` 找对路径

### qa-server.service 必须的 sed patch（ExecStartPre，每次重启自动跑）

实际只有 3 条 ExecStartPre sed（下面这三行是 **VPS 上那份**的写法；⚠ 仓库里 `references/systemd/qa-server.service` 是 **Pi 版**——`Description=…(Pi 侧)`、`User=bwicarus`、路径全为 `/home/bwicarus/claude`，两条 CDN sed 的替换目标是 `http://bwicarus.taile44d0c.ts.net/static/qa/...` 而**不是** `bwicarus.space`）：
```
sed -i "s|https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js|https://bwicarus.space/static/qa/mathjax.js|g" qa_browser.py
sed -i "s|https://cdn.jsdelivr.net/npm/marked@9/marked.min.js|https://bwicarus.space/static/qa/marked.js|g" qa_browser.py
sed -i 's|"--dangerously-skip-permissions"|"--allowedTools", "Read"|g' ai_backends.py
```
3 个原因：
1. iPad 在中国访问 jsdelivr CDN 慢/被墙 → 本机 host MathJax + marked
2. Claude CLI 在 root 下拒绝 `--dangerously-skip-permissions`
3. 把该 flag 原地替换成 `--allowedTools Read`（一步搞定：既去掉被拒的 flag，又显式白名单 Read tool）

注：`_client/core/ai_backends.py` 源码现已内置 `--allowedTools Read` 且不含 `--dangerously-skip-permissions`，所以第 3 条 sed 当前命中目标串已不存在、是幂等保底（git pull 万一引回旧写法时兜底）。

### Web 控制面板（`/control/`）
- 完整复刻 dashboard 视觉风格（深紫渐变背景 + 毛玻璃 panel + indigo/violet 强调）
- 3-panel 布局（用户最后决定）：
  1. **状态** = 系统状态 + Daily 进度
  2. **操作** = 触发操作 + 触发日志（实时滚动）
  3. **设置** = AI 后端切换 + Anki/笔记登记/凌晨定时/iPad 截图问答 所有开关
- panel 折叠/展开动画完全跟 dashboard 同款（flex-grow + cubic-bezier 0.4s + opacity 暗化）
- 左侧 drawer 完全照搬 dashboard `.graph-drawer` 镜像（含 SVG turbulence noise 颗粒）：
  - 默认关闭，左侧把手 grip 18×96 玻璃条
  - 点把手或 ESC 切换 `translateX(-102%) ↔ 0`
  - 悬浮模式 checkbox 决定挤压主内容 vs 浮在上面
  - 抽屉内含可编辑的导航链接列表（`server-config.json::sidebar_links`）+ 编辑按钮
  - 编辑链接 → 右侧嵌套子抽屉（仿 graph-settings-drawer）增删改/排序
- 配置写到 `/root/claude/state/server-config.json`（sidebar_links + anki.auto_restart + auto_upload_after_register + scheduled_register.{wake_anki,upload_after} + qa_remote_daemon + qa_exercises_subdir + qa_wrong_subdir）

### iPad Safari 适配
- `html, body { height: 100dvh }`（动态视口）避免 100vh 把底部 panel 推出视野
- `.panel-stack { overflow-y: auto }` 兜底滚动

### 完整 daily 流程验证
- `scripts/daily_anki_status.py::main` 现在跑 **22 步**（2026-08-19 核实；开头另有总开关 `server-config` 的 `daily.enabled`，false 则 timer 照常触发但脚本 `write_run("skipped")` 空跑退出）。下面这份编号列表是 2026-05 的旧子集，**权威流程直接读 `scripts/daily_anki_status.py::main`**——它比下表多出：通用语停用词 / 停用词复活赛（受 `stopword_gov.enabled`、`ai_judge` 控制）、领域词典 / 融合权重学习 / 跨语言概念归一（`attention_profile.py --domain-dict|--fit|--concepts`）、学习近况（`learning_situations.py --daily`）、错误模式元画像（`error_meta_profile.py --gen`）；概念网三步已拆到独立的 `concept-graph.timer`（02:30）：
  1. smoke tests（守门，任一 fail 立即 abort，不动 Anki / vault / dashboard）
  2. 确保 AnkiConnect
  3. AnkiWeb 同步（拉最新 —— 在读 Anki 数据**之前**先拉，吃进 AnkiDroid 等其它设备的复习记录；失败不阻断）
  4. 登记新笔记（`register_notes.py --no-update-kg`，全 vault 约 6 分钟）
  5. 更新 Anki 状态（anki_status）
  6. 计算复习优先级（review_priority）
  7. 薄弱卡 AI 改写（step_weak）
  8. 已掌握卡换问法（step_antimodel）
  9. 卡片质量体检（step_quality）
  10. 重建必复习牌组（build_review_deck）
  11. 清理孤儿（cleanup_orphans --apply）
  12. KG 关联+掌握度（run_kg_link_mastery，含 link_with_ai + audit_kg）
  13. 导出仪表板（export_dashboard）
  14. 部署仪表板（deploy_dashboard）
  15. AnkiWeb 同步（推送本次改动）
  - 三个 AI 卡维护步骤（7/8/9）各由 server-config `weak_card_refresh` / `card_antimodel` / `card_quality` 的 `enabled` 单独控制。
- `state/last_run.json` 实时记录每步进度，控制面板"状态"panel 实时显示

### 在服务器侧继续这个项目
见独立 reference [`server-side-claude-code.md`](server-side-claude-code.md)。
- 在服务器 ssh + tmux + claude 模式
- memory 通过 `scp -r` 同步（新版 Claude Code 用**单横杠** project key，memory 与 transcripts 同目录：VPS=`-root-claude`、Pi=`-home-bwicarus-claude`；旧「双横杠」目录已废弃。同步前 `ls -td ~/.claude/projects/*/memory | head -1` 确认活跃目录）
