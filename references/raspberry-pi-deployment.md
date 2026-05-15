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
bwicarus.space (VPS, 1vCPU/3.8G)   bwicarus.taile44d0c.ts.net (Pi 5, 8GB)
  └─ 1175 笔记 vault                 └─ 1175 笔记 vault（独立副本）
  └─ 5634 Anki 卡                   └─ 5634 Anki 卡（独立副本，AnkiWeb sync）
  └─ webapp/qa-server/...           └─ 同上 7 个 systemd 服务
  └─ Let's Encrypt (bwicarus.space) └─ Let's Encrypt (Tailscale Cert 自动签)
  └─ daily timer 04:00 CST          └─ daily timer 04:00 JST（错开 1 小时）
```

两端**完全独立**：服务器下线 Pi 仍 100% 可用。共享 source of truth 是 GitHub 仓库 + AnkiWeb + Obsidian Sync 云端。

## 硬件 / OS

- Raspberry Pi 5 8GB（aarch64）
- Debian 13 trixie
- 235 GB NVMe SSD（用了 ~6GB after deployment）
- Tailscale 1.96.4 已装并加入 tailnet（hostname `bwicarus`，IP `100.101.15.57`）

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

# Claude / Codex CLI（npm 全局）
sudo npm install -g @anthropic-ai/claude-code @openai/codex

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
cp $SRC/app.py $SRC/control.py ~/webapp/
cp -r $SRC/templates/* ~/webapp/templates/
cp $SRC/static/nav.js /var/www/html/static/

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
WEBAPP_DASHBOARD_DIR=/home/bwicarus/webapp/data/users/bwicarus/dashboard
EOF
chmod 600 ~/webapp/.env

# 项目 .env（systemd 全用这个）
cat > ~/claude/.env <<EOF
CLAUDE_PROJECT=/home/bwicarus/claude
OBSIDIAN_VAULT=/home/bwicarus/obsidian
APP_PYTHON=/usr/bin/python3
APP_PYTHONW=/usr/bin/python3
APP_CLAUDE=/usr/bin/claude
APP_CODEX=/usr/bin/codex
ANKI_CONNECT_URL=http://127.0.0.1:8765
AI_SETTINGS_FILE=/home/bwicarus/claude/state/ai-settings.json
EOF
```

7 个 systemd unit 写在 [systemd/](systemd/)，Pi 版用 `User=bwicarus` + `/home/bwicarus/` 路径。

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

nginx 配置见 `_server_deploy/nginx/`，Pi 版的只在 `[bwicarus|Tailscale DNS|100.101.15.57]` 几个 host 上监听 80/443。

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
sudo systemctl enable --now xvfb-99 anki-headless obsidian-sync qa-server webapp nginx
sudo systemctl enable --now bwicarus-daily.timer
sudo systemctl start bwicarus-daily.service        # 首次手动跑，验证 10 步全 ok
```

预期：10 步全 ✓（smoke tests / AnkiConnect / register-空 / anki_status / review / build_review_deck / cleanup / export_dashboard / deploy_dashboard / AnkiWeb sync）。

## 跟服务器对等度

| 维度 | 服务器 | Pi |
|---|---|---|
| systemd 服务 | 7 个 | 7 个 |
| webapp 路由 | 完整 | 完整 |
| Vault | 1175 md / 1.4GB | 1175 md / 1.4GB |
| Anki | 33 decks / 5634 cards / 3258 notes | 完全一致 |
| AI CLI | Claude / Codex | 完全一致 |
| PDF / OCR | PyMuPDF + tesseract | 完全一致 |
| qa-server CDN | bwicarus.space 镜像 | Pi nginx 镜像 |
| SSL | Let's Encrypt (Certbot) | Let's Encrypt (Tailscale Cert) |
| 公网入口 | ✅ | ❌ 仅 Tailscale |

## 已知差异（设计）

- **AnkiWeb 同步**：两边都是 sync client，共享同一账户的 hkey。两边 daily 错开（VPS 04:00 CST = UTC+8 / Pi 04:00 JST = UTC+9），自然差 1 小时不撞。
- **webapp 用户表独立**：Pi 上 `/home/bwicarus/webapp/data/app.db` 是 Pi 自己的 SQLite，跟 VPS 分离。admin `bwicarus` 用同 PASSWORD_HASH 在两边都能登录；其它注册用户在 Pi 上不自动出现。
- **Obsidian Sync**：两边都是 sync 客户端，云端是真相源，多设备 supported。
- **Memory**：两边 auto memory 是分支快照。在 Pi 上累积的新 memory 不自动回流 VPS，反之亦然。

## 跨平台修复（部署这次顺带提交的）

让 `_server_deploy/` 的脚本能在不同 `CLAUDE_PROJECT` 路径下跑：

- `3b81243` `_server_deploy/control.py` / `qa_server.py` 走 env var
- `0bdc99c` `daily_anki_status.py` `WEBAPP_DASHBOARD_DIR` 走 env var
- `9d79606` `scripts/platform_utils.py` + `register_notes.py::ensure_obsidian_synced` Linux 分支

下次起新实例（第二台 Pi / Mac mini / 别人的 VPS）：复用本文档，预期 30 min 内全套跑起来。
