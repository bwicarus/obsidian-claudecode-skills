# Raspberry Pi 5 完整实例部署（2026-05-15）

把 bwicarus 项目从 VPS 复制到 Pi 5 8GB 上，跟 `bwicarus.space` **功能完全对等**的实例，
通过 Tailscale 私网访问。本文档是这次部署的实操记录 + 可复用 checklist。

> 配套指南：[linux-server-migration.md](linux-server-migration.md)（VPS 版迁移）/
> [server-side-claude-code.md](server-side-claude-code.md)（在 Pi 或 VPS 上继续这个项目）/
> [pi-snippets.md](pi-snippets.md)（SSH 客户端 Snippets 清单）

## 最终拓扑

```
公网                              Tailscale 内网 100.x.x.x
─────────                          ──────────────────────
> ⏸ **状态注记（2026-06-10）：VPS 已暂停，Pi 是唯一活跃实例。** 本文档是 2026-05-15 那次「从 VPS 复制到 Pi」的实操记录：下面所有 `rsync $VPS:` / `ssh $VPS` 的 bootstrap 步骤**今天不能照跑**（VPS 自动化单元已 disable、代码冻结在 2026-05-28）。要新起一台实例，数据源改为 GitHub 仓库 + AnkiWeb + Obsidian Sync 云端，或从 Pi 直接 rsync。拓扑图与下方「跟服务器对等度」表保留作历史记录。
  └─ 1175 笔记 vault                 └─ 1175 笔记 vault（独立副本）
  └─ 5634 Anki 卡                   └─ 5634 Anki 卡（独立副本，AnkiWeb sync）
  └─ webapp/qa-server/...           └─ 同上 6 个项目 systemd unit + nginx
  └─ Let's Encrypt (bwicarus.space) └─ Let's Encrypt (Tailscale Cert 自动签)
  └─ daily timer 04:00 CST          └─ daily timer **01:00**（`references/systemd/bwicarus-daily.timer`，原 04:00，73d8eb6 起提前以错开时段）
```

两端**完全独立**：服务器下线 Pi 仍 100% 可用。共享 source of truth 是 GitHub 仓库 + AnkiWeb + Obsidian Sync 云端。

## 硬件 / OS

- Raspberry Pi 5 8GB（aarch64）
- Debian 13 trixie
- 235 GB NVMe SSD（用了 ~6GB after deployment）
- Tailscale 已装并加入 tailnet（hostname `bwicarus`，IP `100.101.15.57`；部署时 1.96.4，之后自动升级，现 1.98.3）

## 部署 checklist（参考实际操作顺序）

### 阶段 0：SSH 探活 + 安全

```bash
# 在 VPS 上跑（从你信任的机器到 Pi）
ssh bwicarus@100.101.15.57          # 首次密码登录
ssh-copy-id bwicarus@100.101.15.57  # 写 authorized_keys
# Pi 上彻底关密码登录：
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

`bwicarus` 用户默认 `NOPASSWD: ALL` sudo（Pi OS 给 admin 用户的默认）。

### 阶段 1：基础依赖

```bash
# NodeSource 22 仓库（apt 自带 20，但 obsidian-headless 要 22+）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -

sudo apt-get install -y --no-install-recommends \
  git nginx xvfb \
  python3-pip python3-venv python3-flask python3-werkzeug python3-pil \
  nodejs sqlite3 build-essential pkg-config tmux \
  tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
  ca-certificates curl apt-file sshpass

sudo apt-file update

# obsidian-headless（vault sync）
sudo npm install -g obsidian-headless

# Claude CLI:用 native installer(装到 ~/.local/bin,自更新可用)。
# ⚠ 别用 npm 全局装 claude-code:自更新替换不了 /usr/bin/claude → 版本永远停在装机当天
#   (VPS 踩过:5-14 npm 装 2.1.141,自更新只会下到 ~/.local 但被 /usr/bin 挡住,2026-06-10 卸 npm 版修复)
curl -fsSL https://claude.ai/install.sh | bash
# Codex CLI（npm 全局）
sudo npm install -g @openai/codex

# PEP 668 系统 Python（Debian 13 默认锁 pip）
sudo pip3 install --break-system-packages pymupdf pytesseract
```

### 阶段 2：clone 项目

```bash
cd ~ && git clone https://github.com/bwicarus/obsidian-claudecode-skills.git claude
```

### 阶段 3：Anki + Xvfb

```bash
sudo mkdir -p /opt/anki-venv && sudo chown bwicarus:bwicarus /opt/anki-venv
python3 -m venv /opt/anki-venv --system-site-packages
/opt/anki-venv/bin/pip install --upgrade pip
/opt/anki-venv/bin/pip install aqt    # ARM64 wheel 已有，5 min 装完

# Qt6 运行时库（headless 必须）
sudo apt-get install -y --no-install-recommends \
  libegl1 libgl1 libglx-mesa0 libxkbcommon0 libxkbcommon-x11-0 \
  libfontconfig1 libdbus-1-3 libnss3 libasound2t64 \
  libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxslt1.1 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-shape0 libxcb-xkb1 \
  libpulse0 libpulse-mainloop-glib0 \
  libxfixes3 libwebpdemux2 libwebpmux3 libminizip1t64 liblcms2-2 libsnappy1v5

# Profile 创建 —— Anki 25 不会自己建，绕开 i18n bug：
DISPLAY=:99 QTWEBENGINE_DISABLE_SANDBOX=1 QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox \
  /opt/anki-venv/bin/python3 -c '
from aqt.profiles import ProfileManager
pm = ProfileManager(base="/home/bwicarus/.local/share/Anki2")
pm._loadMeta()
pm.create("User 1")
'

# AnkiConnect addon 从 VPS 直接 rsync 过来（懒人办法）；或 git clone 官方 repo
rsync -avz $VPS:.local/share/Anki2/addons21/2055492159 ~/.local/share/Anki2/addons21/
```

### 阶段 4：vault sync

```bash
# 跳过 Obsidian E2E 密码 prompt：直接复用 VPS auth_token + config
scp $VPS:/root/.config/obsidian-headless/auth_token ~/.config/obsidian-headless/
VAULT_ID=$(ssh $VPS 'ls /root/.config/obsidian-headless/sync/')
ssh $VPS "cat /root/.config/obsidian-headless/sync/$VAULT_ID/config.json" \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)
c["vaultPath"]  = "/home/bwicarus/obsidian"
c["deviceName"] = "pi-bwicarus"
print(json.dumps(c, indent=2))
' > ~/.config/obsidian-headless/sync/$VAULT_ID/config.json

mkdir -p ~/obsidian
ob sync --path ~/obsidian        # 首次全量，1.4GB / ~3min
```

### 阶段 5：webapp + nginx + systemd

```bash
# 部署目录
mkdir -p ~/webapp/{templates,data/{users,dashboard_template,history_template/images}}
sudo mkdir -p /var/www/html/static/qa
sudo chown bwicarus:bwicarus /var/www/html/static -R

SRC=~/claude/_server_deploy
bash scripts/deploy_reader.sh          # app.py / control.py 都在部署清单内，别手工 cp
cp -r $SRC/templates/* ~/webapp/templates/
# nav.js 同样在部署清单内，已由上面的 deploy_reader.sh 一并投递

# 模板内容（从 VPS rsync 或 git 出来）
rsync -avz $VPS:/root/webapp/data/{dashboard_template,history_template}/ ~/webapp/data/
rsync -avz $VPS:/var/www/html/static/qa/ /var/www/html/static/qa/

# .env（生成独立 SECRET_KEY，复用 PASSWORD_HASH 跨设备同一登录密码）
cat > ~/webapp/.env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
PASSWORD_HASH=$(ssh $VPS 'grep PASSWORD_HASH /root/webapp/.env | cut -d= -f2-')
RELAY_KEY=$(openssl rand -hex 16)
WEBAPP_DATA=/home/bwicarus/webapp/data
CLAUDE_PROJECT=/home/bwicarus/claude
EOF
chmod 600 ~/webapp/.env

# 项目 .env（systemd 全用这个；bwicarus-daily.service 的 EnvironmentFile 指向它）
cat > ~/claude/.env <<EOF
CLAUDE_PROJECT=/home/bwicarus/claude
OBSIDIAN_VAULT=/home/bwicarus/obsidian
APP_PYTHON=/usr/bin/python3
APP_PYTHONW=/usr/bin/python3
APP_CLAUDE=/home/bwicarus/.local/bin/claude
APP_CODEX=/usr/bin/codex
ANKI_CONNECT_URL=http://127.0.0.1:8765
AI_SETTINGS_FILE=/home/bwicarus/claude/state/ai-settings.json
WEBAPP_DASHBOARD_DIR=/home/bwicarus/webapp/data/users/bwicarus/dashboard
EOF
```

> ⚠️ `WEBAPP_DASHBOARD_DIR` 必须放在**项目 `~/claude/.env`**（不是 `~/webapp/.env`）：
> `bwicarus-daily.service` 的 `EnvironmentFile` 指向 `/home/bwicarus/claude/.env`，
> `scripts/daily_anki_status.py` 从环境读 `WEBAPP_DASHBOARD_DIR`，缺省值是硬编码的 VPS
> 路径 `/root/webapp/data/users/bwicarus/dashboard`。若漏放进项目 .env，daily 的部署仪表板
> 步骤会落到不存在的 `/root` 路径，dashboard 静默不更新。

项目自带的 systemd unit 写在 [systemd/](systemd/)（Pi 版用 `User=bwicarus` + `/home/bwicarus/` 路径）：
`xvfb-99` / `anki-headless` / `obsidian-sync` / `qa-server` / `webapp` / `bwicarus-daily.{service,timer}`
这 6 个核心 unit，加上系统包 `nginx`。

另有几套 **2026-05-15 之后新增**的增量 timer（Pi 实机已在跑）：

- `anki-sync-refresh.{service,timer}`（2026-05-23）—— 每 15 分钟跑 `scripts/anki_sync_refresh.py`，
  复习数据有变化时做 Anki sync + 仪表板刷新。
- `bwicarus-quick-sync.{service,timer}`（2026-05-24）—— 每 15 分钟跑 `scripts/quick_sync.py`，
  vault 快速同步（清理 + KG prune，不跑 AI / 不动 Anki）。
- `push-big-files.{service,timer}`（2026-06-08）—— 每 4 小时把 vault 里 >200MB 的文件推到 PC，
  绕过 Obsidian Sync 单文件 200MB 上限。详见「大文件跨设备」一节。
- `bwicarus-backup.{service,timer}`（2026-06-07）—— 每日 **03:30** 跑 `scripts/backup_data.sh`，
  把 webapp/data 账号数据（app.db/fitness/youtube DB 走 `sqlite3 .backup` 在线一致备份 + 用户 json）
  + claude/state 学习状态备份到 `~/backups`（保留 14 份，排除可再生的 page-img/OCR/模型/搜索索引大缓存）。
- `yolo-figures.{service,timer}`（2026-06-20）—— 每 **6h** 闲时跑 `doclayout-venv/bin/python scripts/yolo_figures.py --all`，
  DocLayout-YOLO 给开了「插图描述」的书框图、写 `figures_geom`（Nice=19/CPUWeight=20 低优先级，Pi CPU ~6.7s/页，幂等）。
- `figures-describe.{service,timer}`（2026-06-20）—— 夜间跑 `scripts/describe_figures_batch.py --all`，
  读 YOLO 框做 gate（0 跳过 / 1 裁图 / ≥2 整页）用 off-peak AI 额度生成插图描述。
- `book-ocr.service` + `book-ocr-watchdog.{service,timer}`（2026-05-30）—— 日文扫描书后台 OCR
  （低优先级长任务）+ 每 15 分钟健康自检（详见 [`book-ocr-pipeline.md`](book-ocr-pipeline.md)）。
  这套 unit 踩过两个 **systemd 通用坑**（都 2026-06-10 修，写 timer/unit 时通用）：
  - **monotonic timer 重启后永不触发**：watchdog.timer 原来只有 `OnBootSec=5min + OnUnitActiveSec=15min`。
    开机很久后再 (re)start timer 时，OnBootSec 早已过去、service 又没跑过（OnUnitActiveSec 无参考点）→
    `systemctl list-timers` 显示 `Trigger: n/a`，**永不触发**（实际 5-30 起 11 天没自检过）；
    `Persistent=true` 只对 OnCalendar 生效，救不了 monotonic timer。修法：加 `OnActiveSec=15min`
    （以 timer 自身激活时刻为基准，(re)start 后必有首跑）。
  - **`StartLimitIntervalSec` / `StartLimitBurst` 属 `[Unit]` 段**：book-ocr.service 旧版误放 `[Service]`，
    systemd 只在 journal 里报一行 `Unknown key ... ignoring` 实际未生效，防 fail-loop 形同虚设。

副本都在 `references/systemd/`，enable 方式同其它 timer（见阶段 10）。

`qa-server.service` 在 Pi 上还需要额外两个 env：

```
Environment=BWICARUS_APP_DIR=/home/bwicarus/claude/state/qa-server-data
Environment=WEBAPP_HISTORY_DIR=/home/bwicarus/webapp/data/users/bwicarus/history
```

`WEBAPP_HISTORY_DIR` 触发 `qa_browser._export_history_to_webapp()`：截图问答保存后
自动把 SQLite 内容导出 `history.json` + 拷贝截图到 webapp data 目录，让
`https://<host>/history/` 立刻看到新条目。未设时（Windows 客户端时代）走
旧 `scripts/export_history.py` + HTTP 上传流程。

### 阶段 6：nginx 反代 + Tailscale HTTPS

需要先在 Tailscale admin console 启用 **HTTPS Certificates** + **MagicDNS**。
然后：

```bash
sudo mkdir -p /etc/tailscale-certs && cd /etc/tailscale-certs
sudo tailscale cert bwicarus.taile44d0c.ts.net   # Let's Encrypt 真证书，3 个月有效
```

nginx 配置：git 的 `_server_deploy/nginx/bwicarus.conf` 是 **VPS 版**（`server_name bwicarus.space` + Certbot），**仓库里没有 Pi 版**。Pi 的配置只存在于机器上的 `/etc/nginx/sites-available/bwicarus`（Tailscale HTTPS Cert + 80/443 两个 server 块，server_name 是 `bwicarus` / MagicDNS / `100.101.15.57`），结构与 git 那份完全不同：**只能手工 patch，绝不可 cp 覆盖**（会冲掉 Tailscale 证书，全站挂）。同理别照 `_server_deploy/nginx/README.md` 的 `cp … /etc/nginx/sites-enabled/default` 在 Pi 上跑。

**⚠️ Pi nginx (`/etc/nginx/sites-available/bwicarus`) 必须给 `/pdf` 和 `/api` location 加 `proxy_read_timeout 300s`**（80 + 443 两个 server 块各 2 处）。默认 60s 下，PDF 阅读器的 AI 端点（`/pdf/api/grammar-analyze` 语法分析、`/pdf/api/dict`/`translate` 走 claude_cli 翻译）在 Pi 上有时跑 >60s → nginx 返回 502。一行 sed 即可：

```bash
sudo sed -i 's|client_max_body_size 50m; }|client_max_body_size 50m; proxy_read_timeout 300s; proxy_connect_timeout 20s; }|g' /etc/nginx/sites-available/bwicarus
sudo nginx -t && sudo systemctl reload nginx
```

配套：`_server_deploy/app.py` 的 `app.run(..., threaded=True)`（已进 git）——否则 Flask dev server 单线程，一个慢 AI 请求阻塞全部，dict/translate 跟着 502。

### 阶段 7：bootstrap 状态数据（避开 register 把 1175 笔记当新笔记）

```bash
rsync -avz $VPS:/root/claude/state/note-states.json ~/claude/state/
rsync -avz $VPS:/root/claude/anki/records/ ~/claude/anki/records/
rsync -avz $VPS:/root/claude/index/ ~/claude/index/
rsync -avz $VPS:/root/claude/state/ai-settings.json $VPS:/root/claude/state/server-config.json ~/claude/state/

# 状态文件里的路径替换（VPS /root → Pi /home/bwicarus）
sed -i 's|/root/obsidian|/home/bwicarus/obsidian|g' ~/claude/state/note-states.json
sed -i 's|/root/obsidian|/home/bwicarus/obsidian|g; s|/root/claude|/home/bwicarus/claude|g' ~/claude/anki/records/*.json

# Anki collection rsync（停两边 anki，56MB，1min）
ssh $VPS 'sudo systemctl stop anki-headless'
sudo systemctl stop anki-headless
rsync -avz --delete "$VPS:.local/share/Anki2/User 1/" ~/".local/share/Anki2/User 1/"
ssh $VPS 'sudo systemctl start anki-headless'
sudo systemctl start anki-headless
```

### 阶段 8：webapp 数据 + admin 私有目录

```bash
rsync -avz $VPS:/root/webapp/data/users/bwicarus/ ~/webapp/data/users/bwicarus/
```

这样 admin `bwicarus` 登录 Pi webapp 后立刻看到完整 history / dashboard 数据。

### 阶段 9：Claude / Codex 凭据 + auto memory

> ⚠️ **不要 scp `.credentials.json`！** OAuth refresh token 是 single-use：
> 两台机器共享同一份 credentials，谁先 refresh 谁就作废另一台的 token，
> 另一台下次启动报 `401 Invalid authentication credentials` /
> `Remote Control failed to connect: /login`（2026-05-15 实际踩过这个坑）。
>
> **每台机器必须各自独立 OAuth 一次。** 只有 settings / memory / codex 配置
> 可以 scp（无副作用）。

```bash
# 1. 各自独立 Claude 登录（device-flow OAuth）
cd ~/claude && claude
# 进 TUI 后 /login，浏览器开它给的 claude.com/cai/oauth/... URL，
# Authorize 后把 callback 的 code#state 粘回 terminal

# 2. 这些可以 scp（无副作用）：
scp $VPS:/root/.claude/settings.json ~/.claude/
scp $VPS:/root/.claude.json ~/                    # 账户元数据，含订阅信息
mkdir -p ~/.claude/projects/home-bwicarus--claude/memory
rsync -avz $VPS:/root/.claude/projects/root--claude/memory/ \
  ~/.claude/projects/home-bwicarus--claude/memory/

# 3. Codex CLI 同理——auth.json 也建议各自 codex login；
#    config.toml 可以 scp
scp $VPS:/root/.codex/config.toml ~/.codex/

# 验证
claude --print "ping"    # 回 "pong" 证明 OAuth 独立可用
codex --version
```

> **重要 1**：Pi 上 cwd 是 `/home/bwicarus/claude`，所以 memory 目录是
> `projects/home-bwicarus--claude/memory/`（双横杠），不是
> `-home-bwicarus-claude/`（单横杠用于 transcript jsonl）。
>
> **重要 2**：scp `.claude.json` 后第一次启动 claude 仍可能要 `/login`
> （因为没 credentials）——这是预期，跑一次 OAuth 即可。`.claude.json` 只是
> 账户元数据缓存，不含可用 token。

### 阶段 10：systemd + 跑首次 daily

```bash
```bash
sudo systemctl enable --now xvfb-99 anki-headless obsidian-sync qa-server webapp nginx
sudo systemctl enable --now bwicarus-daily.timer
# 增量 timer（后续陆续新增）
sudo systemctl enable --now anki-sync-refresh.timer bwicarus-quick-sync.timer push-big-files.timer \
     bwicarus-backup.timer yolo-figures.timer figures-describe.timer book-ocr-watchdog.timer \
     concept-graph.timer
# Pi 当前两项核心职责（AI 调用中继 / 设备间同步中继）对应的 unit，别漏：
sudo systemctl enable --now voice-rt.service        # 语音 realtime 中继 :8767，deploy_reader.sh 的健康门禁会探它
sudo systemctl enable --now reader-context-push.service mcp-server.service rbi-server.service
# 手表语音桥。⚠ 必须在第一次跑 deploy_reader.sh **之前** enable：部署脚本会在
# 安装新 unit 文件之前先 stop 它，对 systemd 还不认识的 unit 执行 stop 会让整次部署中止。
sudo systemctl enable --now watch-voice.service   # :8768，见 references/watch-voice-bridge.md
sudo systemctl start bwicarus-daily.service        # 首次手动跑，验证所有 step 全 ok
```
sudo systemctl enable --now bwicarus-daily.timer
sudo systemctl enable --now anki-sync-refresh.timer bwicarus-quick-sync.timer push-big-files.timer \
     bwicarus-backup.timer yolo-figures.timer figures-describe.timer book-ocr-watchdog.timer   # 增量 timer（后续陆续新增）
sudo systemctl start bwicarus-daily.service        # 首次手动跑，验证所有 step 全 ok
```

预期：所有 step 全 ✓。`scripts/daily_anki_status.py` 当前跑 **20 步**（含守门的 smoke tests）+ 2 个条件步骤：
smoke tests / 确保 AnkiConnect / AnkiWeb 同步（拉最新）/ 登记新笔记 / 更新 Anki 状态 /
计算复习优先级 / 薄弱卡 AI 改写 / 已掌握卡换问法 / 卡片质量体检 / 重建必复习牌组 /
清理孤儿 / KG 关联+掌握度 /〔通用语停用词 + 停用词复活赛：受 server-config `stopword_gov.enabled` 控制〕/
领域词典 / 融合权重学习 / 跨语言概念归一 / 学习近况 / 错误模式元画像 /
导出仪表板 / 部署仪表板 / AnkiWeb 同步。
（概念网三步已拆到独立的 `concept-graph.timer`，不在 daily 内。步数以 `main()` 里的 `step(...)` 调用为准，别写死。）
smoke tests / 确保 AnkiConnect / AnkiWeb 同步（拉最新）/ 登记新笔记 / 更新 Anki 状态 /
计算复习优先级 / 薄弱卡 AI 改写 / 已掌握卡换问法 / 卡片质量体检 / 重建必复习牌组 /
清理孤儿 / KG 关联+掌握度 / 导出仪表板 / 部署仪表板 / AnkiWeb 同步。
（脚本顶部 docstring 仍是早期 0-8 的旧编号，以 `main()` 里的 `step(...)` 调用为准。）

## 跟服务器对等度

| 维度 | 服务器 | Pi |
|---|---|---|
| systemd 服务 | 6 个项目 unit + nginx（+ 增量 timer 数套，见上） | 同上（另有仅 Pi 的 push-big-files.timer） |
| webapp 路由 | 完整 | 完整 |
| Vault | 1175 md / 1.4GB | 1175 md / 1.4GB |
| Anki | 33 decks / 5634 cards / 3258 notes | 完全一致 |
| AI CLI | Claude / Codex | 完全一致 |
| PDF / OCR | PyMuPDF + tesseract | 完全一致 |
| qa-server CDN | bwicarus.space 镜像 | Pi nginx 镜像 |
| SSL | Let's Encrypt (Certbot) | Let's Encrypt (Tailscale Cert) |
| 公网入口 | ✅ | ❌ 仅 Tailscale |

## 已知差异（设计）

- **AnkiWeb 同步**：两边都是 sync client，共享同一账户的 hkey。两边 daily 错开（VPS 04:00 CST = UTC+8 / Pi **01:00** JST，见 `references/systemd/bwicarus-daily.timer`，73d8eb6 起从 04:00 提前），不会撞。
- **webapp 用户表独立**：Pi 上 `/home/bwicarus/webapp/data/app.db` 是 Pi 自己的 SQLite，跟 VPS 分离。admin `bwicarus` 用同 PASSWORD_HASH 在两边都能登录；其它注册用户在 Pi 上不自动出现。
- **Obsidian Sync**：两边都是 sync 客户端，云端是真相源，多设备 supported。
- **Memory**：两边 auto memory 是分支快照。在 Pi 上累积的新 memory 不自动回流 VPS，反之亦然。

## 大文件跨设备（Obsidian Sync 200MB/文件上限旁路，2026-06-08）

Obsidian Sync **Plus** 单文件上限 **200MB**（Standard 仅 5MB）。≤200MB 的笔记/PDF 它自己双向同步；
**>200MB 的它永远不同步**。目前全 vault 仅 `応用情報技術者.pdf`（318M）超限
（`scripts/push_big_files_to_pc.py` docstring），其余都走 Obsidian 云端。这本就靠下面的旁路从 Pi 推到 PC。

### 机制：Pi 每 4h 主动推（不是 PC 拉）

`scripts/push_big_files_to_pc.py` 扫 `/home/bwicarus/obsidian`（`VAULT`，`:22`）下 **>200MiB**
（`THRESHOLD = 200*1024*1024`，`:24`）且非隐藏的文件，`scp` 到 PC 的 `C:/obsidian`
（`PC_VAULT`，`:23`）对应相对路径下。PC 地址硬编码 `bwicarus@100.99.9.124`（`PC`，`:21`，Tailscale IP）。

**为何 Pi 推而非 PC 拉**（`push_big_files_to_pc.py:8`）：PC 会睡眠 / 间歇掉 Tailscale，Pi 永远在线。
Pi push「PC 可达才推、不可达静默跳过、下次 timer 再补」对 PC 的不稳定最鲁棒；且 Pi→PC 免密 SSH
本来就通（见 memory `pc-ssh-access`），不用配新密钥。

**关键行为**：

- **PC 睡着静默跳过**：开头 `pc_reachable()`（`:34`，`ssh -o BatchMode=yes -o ConnectTimeout=8` 跑
  `cmd /c echo OK`）失败就 `print` 一行「PC 不可达…下次 timer 再补」并 **return 0**（成功退出，不报警）。
- **幂等不重传**：每个文件先 `pc_size()`（`:43`，PowerShell `Get-Item .Length`）拿 PC 上同路径大小，
  同名同大小就跳过（`:68`）。否则 `New-Item -Force` 建目标中间目录（`:74`）再 `scp`（`:78`，单文件超时
  3600s）。
- **PowerShell 调用**走 `_ps()`（`:27`）：把命令 UTF-16LE base64 后 `powershell -NoProfile -EncodedCommand`
  传过去（避开引号/编码坑）。
- 退出码：有失败返回 1，否则 0（`:91`）。

### systemd 安装（Pi 侧）

副本在 `references/systemd/push-big-files.{service,timer}`：

- `push-big-files.service`：`Type=oneshot`，`EnvironmentFile=/home/bwicarus/claude/.env`，
  `User=bwicarus`，`TimeoutStartSec=3600`（跟脚本里单文件 scp 超时对齐）。
- `push-big-files.timer`：`OnBootSec=5min` + `OnUnitActiveSec=4h` + `Persistent=true`
  （错过的周期开机补跑一次）。

enable：

```bash
sudo cp ~/claude/references/systemd/push-big-files.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now push-big-files.timer
# 手动验证一次：
sudo systemctl start push-big-files.service && journalctl -u push-big-files.service -n 20
```

### 背景事实：PC 那本停更其实不是尺寸问题

PC 的「Obsidian Headless Sync」计划任务从 **2026-05-15 起被禁用**（迁服务器时连同每日任务一起禁掉），
导致 PC vault **停更 3 周**——这是任务被禁、不是 318M 那本超限。**2026-06-08 已重新 Enable + Start**，
PC vault 重新跟 Obsidian 云端同步；超限那本则由本旁路补上。

PC 端现在由 `_server_deploy/local_supervisor.pyw` 的 `ensure_obsidian_sync()`（`local_supervisor.pyw:272`）
持续看护这个计划任务：`Get-ScheduledTask` 读 State（语言中立的 Disabled/Ready/Running），被禁 → `Enable-ScheduledTask`、
没跑 → `Start-ScheduledTask`（任务名 `Obsidian Headless Sync`，`:45`）。开机即看护一次 + 托盘有「保持 Obsidian 同步」开关。
所以「≤200MB 走 Obsidian 双向同步、>200MB 走 Pi push」两条链路在 PC 侧分别由 supervisor 和本旁路兜底。

## 跨平台修复（部署这次顺带提交的）

让 `_server_deploy/` 的脚本能在不同 `CLAUDE_PROJECT` 路径下跑：

- `3b81243` `_server_deploy/control.py` / `qa_server.py` 走 env var
- `0bdc99c` `daily_anki_status.py` `WEBAPP_DASHBOARD_DIR` 走 env var
- `9d79606` `scripts/platform_utils.py` + `register_notes.py::ensure_obsidian_synced` Linux 分支

下次起新实例（第二台 Pi / Mac mini / 别人的 VPS）：复用本文档，预期 30 min 内全套跑起来。

## 全站 PWA service worker：nginx 必加 `/sw.js`（2026-06-07，手工 patch）

全站 service worker `/sw.js`（`app.py` 路由，scope `/`）让 PC/iPad 重复打开秒回 + 数据本地缓存。但 **Pi nginx 是前缀代理 + `location / { try_files }` 静态兜底** → `/sw.js`（`.js`）落到静态兜底去找 `/var/www/html/sw.js` → 404 → SW 注册失败、全站 PWA 不生效。

**必须在 80 / 443 两个 server 块各加一条**（仿已有的 `location = /manifest.webmanifest`）：

```
location = /sw.js { proxy_pass http://127.0.0.1:5000; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-Proto https; }
```

一行 sed（在每个 manifest location 后插）：

```bash
sudo sed -i '/location = \/manifest\.webmanifest/a\    location = /sw.js { proxy_pass http://127.0.0.1:5000; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-Proto https; }' /etc/nginx/sites-available/bwicarus
sudo nginx -t && sudo systemctl reload nginx
```

⚠ **手工 patch** `/etc/nginx/sites-available/bwicarus`（跟 git 里 VPS 版结构不同，**绝不可 cp 覆盖**，否则冲掉 Tailscale 证书）。验证：`curl -sk https://bwicarus.taile44d0c.ts.net/sw.js` 应 `200` + `application/javascript`（不是 404 html）。`/pdf/sw.js` 走 `/pdf` 代理前缀，不用单独加。
