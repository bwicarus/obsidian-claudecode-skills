# 在服务器侧继续这个项目（Claude Code Linux）

> ⏸ **状态注记（2026-06-10）：服务器侧 = Pi。VPS 已暂停**——自动化单元（xvfb-99 / anki-headless / obsidian-sync / qa-server / bwicarus-daily.timer）已 `systemctl disable`，只剩公网 `webapp.service`；代码停在 2026-05-28，重新启用前必须先 `git pull` + 重部署。日常接续工作一律 SSH 进 **Pi**（`ssh bwicarus@bwicarus.taile44d0c.ts.net`）；本文涉及 VPS 的命令/路径（`root@bwicarus.space`、`/root/claude` 等）保留作参考，仅在 VPS 重新启用后有效。⚠ VPS 的 OS hostname 实际也是 `bwicarus`（跟 Pi 撞名，Tailscale 设备名才是 `bwicarus-3`），判定在哪台机用路径（`/root/claude` = VPS，`/home/bwicarus/claude` = Pi），别用 hostname。

2026-05-14 起整套工作流迁到 `bwicarus.space` 服务器；**2026-05-15** 又
mirror 到 Raspberry Pi 5，此后 Pi 成为**唯一活跃实例**（VPS 见顶部注记）。这份指南教你（或下一个
session 的 Claude Code）**从 Windows / 任意机器 切换到任一 Linux 实例**继续协作。

Pi 部署细节见 [`raspberry-pi-deployment.md`](raspberry-pi-deployment.md)。

## 两个 Linux 实例已就绪的一切

| 项 | VPS (`bwicarus.space`,⏸ 暂停,代码停 5-28) | Pi (`bwicarus.taile44d0c.ts.net`,**主力**) |
|---|---|---|
| 项目代码 | `/root/claude/` | `/home/bwicarus/claude/` |
| Vault | `/root/obsidian/` (约 1300+ md) | `/home/bwicarus/obsidian/` (约 1300+ md) |
| Anki | `/root/.local/share/Anki2/User 1/` | `/home/bwicarus/.local/share/Anki2/User 1/` |
| Anki venv | `/opt/anki-venv/` | `/opt/anki-venv/`（同位置） |
| Claude CLI | `/root/.local/bin/claude`（native installer;2026-06-10 卸掉了挡道的 npm 版 /usr/bin/claude,PATH 加了 ~/.local/bin,另有 `/usr/local/bin/claude` symlink 兜底——旧 shell/非登录 shell 也找得到,自更新只改 ~/.local 内层链接不受影响） | `/home/bwicarus/.local/bin/claude`（native installer，经 PATH 调用，非 /usr/bin） |
| Codex CLI | `/usr/bin/codex` v0.130+ | `/usr/bin/codex`（symlink，同左） |
| OAuth 凭据 | `/root/.claude/.credentials.json` | `/home/bwicarus/.claude/.credentials.json` |
| Python / Node | 3.10 / 22 (nvm) | 3.13 / 22 (NodeSource) |
| 项目 .env | `/root/claude/.env` | `/home/bwicarus/claude/.env` |
| 用户 | `root` | `bwicarus`（NOPASSWD sudo） |
| MagicDNS | `bwicarus-3.taile44d0c.ts.net` | `bwicarus.taile44d0c.ts.net` |

memory（跨会话）路径取决于 cwd（cwd 决定 project key）。**新版 Claude Code 已改 project-key 编码：memory 现与 jsonl transcripts 同处「单横杠」目录**，旧的「双横杠」目录已废弃（仅留陈旧的 feedback 文件，别再往那找）：

| 实例 | memory 目录（活跃，单横杠）| jsonl transcripts（同目录）| 旧废弃目录（双横杠）|
|---|---|---|---|
| VPS | `/root/.claude/projects/-root-claude/memory/` | `.../-root-claude/*.jsonl` | `root--claude/`（旧，本机无法核实 VPS，按 Pi 同理推断已迁单横杠）|
| Pi | `/home/bwicarus/.claude/projects/-home-bwicarus-claude/memory/` | `.../-home-bwicarus-claude/*.jsonl` | `home-bwicarus--claude/`（旧，冻结于 2026-05-15）|

不确定时用 `ls -td ~/.claude/projects/*/memory 2>/dev/null | head -1` 取最近更新的 memory 目录。

## 切换工作流的两种方式

### A. 长任务（推荐）—— tmux + Claude Code

Pi（日常用这个）：
```bash
ssh bwicarus@100.101.15.57        # 或 bwicarus@bwicarus.taile44d0c.ts.net
tmux new -A -s claude
cd ~/claude
claude
```

VPS（⏸ 暂停中，重新启用后才适用）：
```bash
ssh root@bwicarus.space
tmux new -A -s claude    # -A：没就建，有就 attach（不会撞 duplicate）
cd /root/claude
claude                   # 进入交互模式（Remote Control 默认开启，iPad App 可见）
# 工作中如果要离开：Ctrl+B 然后 D（脱离 tmux，会话保留）
# 回来：ssh + tmux attach -t claude
```

好处：
- 断网 / 退出 SSH，Claude Code 会话保留在 Linux
- 下次回来直接接着用
- 不依赖你 Windows / 本地机器在线
- Tailscale 内网随时随地接入

### A2. 接续旧 session（jsonl 同步过来后）

把 VPS 的 session 同步到 Pi 之后，Pi 上 `claude --resume` 能列出 / 接续。

```bash
# 在 VPS 上 rsync 当前 session 给 Pi
SESSION=<session-id>     # 从 echo $CLAUDE_CODE_SESSION_ID 拿
rsync -avz /root/.claude/projects/-root-claude/${SESSION}.jsonl \
  bwicarus@100.101.15.57:.claude/projects/-home-bwicarus-claude/
rsync -avz /root/.claude/projects/-root-claude/${SESSION}/ \
  bwicarus@100.101.15.57:.claude/projects/-home-bwicarus-claude/${SESSION}/
rsync -avz /root/.claude/tasks/${SESSION}/ \
  bwicarus@100.101.15.57:.claude/tasks/${SESSION}/

# Pi 上接续
ssh bwicarus@100.101.15.57
cd ~/claude
claude --resume <session-id>
```

**重要**：避免**两边同时活跃**。jsonl 会从 resume 点分叉，下次同步就有冲突。
单边工作，确认要切换时先 `/exit` 一边，再 rsync 最新到另一边。

### B. 一次性查询 —— 单次 prompt

```bash
ssh bwicarus@bwicarus.taile44d0c.ts.net 'cd ~/claude && claude -p "项目当前状态"'
# VPS（暂停中）同理：ssh root@bwicarus.space 'cd /root/claude && claude -p "..."'
```

好处：单次问答，不需要建立持久会话。

## 跟 Windows 上的 Claude Code 切换的注意点

| 维度 | Windows 端（你现在）| 服务器端（迁过去后） |
|---|---|---|
| 工作目录 | `C:\claude\` | `/root/claude/` |
| Claude Code 版本 | Opus 4.7 1M context | 默认（Sonnet 4.6）—— 要 Opus 加 `--model claude-opus-4-7` |
| 内存 / 上下文 | 完整保留（这个对话窗）| 全新会话 |
| 操作 Windows | 直接 | **可 SSH 进**（2026-06-05 配通）：从 Pi `ssh bwicarus@100.99.9.124`(Tailscale `bwicarus-2`)免密(Pi ed25519 公钥已加 PC 的 `administrators_authorized_keys`)。前提 PC 开机+Tailscale 在线；默认 shell 有 Zellij TUI 噪声 → 用 `cmd /c "..."` + `tail` 取 `Bye from Zellij!` 之后的真输出。详见 memory `pc-ssh-access` |
| 操作服务器 | ssh + 132ms RTT | **本地直接** bash / file 编辑（即时） |
| Memory（auto memory） | `~/.claude/projects/C--claude/memory/` (Windows 私有) | `/root/.claude/projects/-root-claude/memory/`（服务器侧，新版单横杠；用户切换时手动 scp 同步） |

### 让上下文跨机器同步

**项目层面**（git 同步）：
- `CLAUDE.md` / `references/*` / 脚本代码 —— git push/pull
- 服务器侧 `git pull origin main` 拉最新

**会话层面**（不能自动同步）：
- 你正在 Windows 上的某个对话的"已经发生过什么"无法传到服务器
- 解决：写好 `CLAUDE.md` + `references/` 让新会话能从文档恢复理解
- memory 文件：scp 命令同步（见下方）

### 同步 auto memory 到服务器

```bash
# Windows → 服务器
scp -r ~/.claude/projects/C--claude/memory/ \
    root@bwicarus.space:/root/.claude/projects/-root-claude/

# 反向：服务器 → Windows（如果服务器侧也产生了新 memory）
scp -r root@bwicarus.space:/root/.claude/projects/-root-claude/memory/ \
    ~/.claude/projects/C--claude/
```

注意：服务器侧的 project name 取决于 cwd（`/root/claude`）。新版 Claude Code 用「单横杠」编码（`-root-claude`），memory 与 transcripts 同目录；旧的双横杠目录 `root--claude/` 已废弃。同步前用 `ls -td /root/.claude/projects/*/memory | head -1` 确认实际活跃目录。

## 服务器侧 Claude Code 该知道的关键事实

下次会话在服务器跑时，先让它**读这两个文件**就能恢复完整理解（Pi 上把 `/root/claude` 换成 `/home/bwicarus/claude`）：
- `/root/claude/CLAUDE.md`
- `/root/claude/references/linux-server-migration.md`

也会自动读 `/root/.claude/projects/-root-claude/memory/MEMORY.md`（新版单横杠目录；如果有的话，要先 scp；Pi = `/home/bwicarus/.claude/projects/-home-bwicarus-claude/memory/`）。

## 常见操作快速参考

> 下列命令是 VPS 视角（`/root/...`、root 直跑 systemctl）。**这些服务现在只在 Pi 跑**：Pi 上把 `/root` 换 `/home/bwicarus`，systemctl 前加 `sudo`（bwicarus 是 NOPASSWD sudo）。

```bash
# 查 Tailscale IP（iPad 端点要用）
tailscale ip -4

# 查 cmd_server API key
cat /root/claude/state/qa-server-data/cmd_server_key.txt

# 查 Daily 任务最近状态
cat /root/claude/state/last_run.json | python3 -m json.tool

# 查所有 systemd 服务健康
for s in xvfb-99 anki-headless obsidian-sync qa-server bwicarus-daily.timer webapp tailscaled; do
  echo "$s: $(systemctl is-active $s)"
done

# 触发完整 daily
systemctl start bwicarus-daily

# 切 AI 后端
source /etc/profile.d/claude.sh
python3 /root/claude/scripts/service_switch.py switch claude   # 或 gpt
python3 /root/claude/scripts/service_switch.py status

# 重启 Anki
systemctl restart anki-headless

# 看控制面板触发日志
tail -f /root/claude/state/logs/webapp_trigger.log

# 看 Anki 启动日志
journalctl -u anki-headless -n 30 --no-pager
```

## Windows 端何时关掉

按用户原策略"服务器端确定稳定再切"：
1. 服务器侧观察 1-2 周（Daily 自动跑 + iPad 操作 + 控制面板触发都稳定）
2. 关掉 Windows 计划任务「Obsidian Anki 每日状态更新」（避免凌晨双跑 AnkiWeb sync 冲突）
3. iPad 快捷指令切到 Tailscale 服务器 IP（见 `references/ipad-switch-to-server.md`）
4. 关掉 bwicarus-client.exe
5. Windows 端可以下机 / 关机

## 永久删除 Windows 端前的 checklist（2026-05-15）

服务器侧已经独立 + 跨机器同步 OK 的：

| 维度 | 服务器侧已有 | Windows 删了之后 |
|---|---|---|
| 代码（git tracked） | `/root/claude/` git clone | ✅ 不丢，GitHub 是 source of truth |
| Vault | `/root/obsidian/` (obsidian-headless sync) | ✅ Obsidian Sync 云端是权威 |
| Anki collection | `/root/.local/share/Anki2/User 1/collection.anki2` | ✅ AnkiWeb 是权威 |
| Claude / Codex CLI token | `/root/.claude/.credentials.json` + `/root/.codex/auth.json` | ✅ 已 scp，独立的（独立 device 记录） |
| AnkiConnect plugin | `/root/.local/share/Anki2/addons21/2055492159/` | ✅ git clone 自 FooSoft repo |
| Memory（auto memory） | `/root/.claude/projects/-root-claude/memory/`（新版单横杠目录） | ✅ 删 Windows 前手动 `scp -r` 同步过来即可 |
| systemd 服务 + Daily timer | 跑着 | ✅ |
| 控制面板 + qa-server | 跑着 | ✅ |

**删 Windows 前必须补**的：

| # | gap | 怎么修 |
|---|---|---|
| 1 | 服务器**不能 git push** | 服务器生成 SSH key（`/root/.ssh/id_ed25519`）+ 用户去 GitHub Settings/Keys 加为 **Deploy Key**（勾 "Allow write access"）+ `git remote set-url origin git@github.com:...`；改完后服务器侧 `git push` 直接走 SSH，不用 token |
| 2 | 服务器**只信 Windows 的 SSH key** | 用户如果还能 SSH 进服务器（用任何客户端、密码、或其他 key）就 OK；不放心可以 `ssh-copy-id` 多加几个 pubkey 到 `/root/.ssh/authorized_keys` |
| 3 | Windows 端 Anki / Obsidian 未同步的本地改动 | Windows 关机前手动 sync 一次（确保 AnkiWeb / Obsidian Sync 拿到所有最新数据） |
| 4 | Windows 客户端 `config.json` 的 GUI 偏好 | 服务器 `state/server-config.json` 是独立的；打开 `/control/` 「设置」面板检查所有开关跟 Windows 一致 |

可以**丢**的（不影响项目）：
- Windows 客户端 `bwicarus-client.exe` 本身（用户可以从 `/profile/` 重新下载）
- Windows 上的 Tailscale 设备记录（tailscale admin 后台会看到 offline，不影响功能）
- Windows 上的 SSH client config / git credential helper（其他设备能 SSH 就行）
- Windows Claude Code session jsonl（聊天回顾用，跟项目继续无关）

## 服务器侧 git push 配置流程（一次性，2026-05-15 完成）

```bash
# 1. 服务器生成 SSH key
ssh root@bwicarus.space
ssh-keygen -t ed25519 -C "bwicarus-server-deploy" -f /root/.ssh/id_ed25519 -N "" -q

# 2. 加 github.com 到 known_hosts（避免首次 ssh 弹 prompt）
ssh-keyscan -H github.com >> /root/.ssh/known_hosts
chmod 600 /root/.ssh/known_hosts /root/.ssh/id_ed25519

# 3. 输出 pubkey
cat /root/.ssh/id_ed25519.pub
# 复制这一行

# 4. 用户在浏览器：
# https://github.com/<owner>/<repo>/settings/keys/new
# Title: bwicarus-server
# Key: 粘 pubkey
# ✅ Allow write access
# 点 Add deploy key

# 5. 改 git remote 从 HTTPS 到 SSH
cd /root/claude
git remote set-url origin git@github.com:bwicarus/obsidian-claudecode-skills.git

# 6. 测试
git push --dry-run origin main
# 应该输出 "Everything up-to-date"
```
