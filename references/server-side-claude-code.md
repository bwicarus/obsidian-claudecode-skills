# 在服务器侧继续这个项目（Claude Code Linux）

2026-05-14 起整套工作流已迁到 `bwicarus.space` 服务器。这份指南教你（或下一个 session 的 Claude Code）**从 Windows 切换到服务器**继续协作。

## 服务器端已就绪的一切

- 项目代码：`/root/claude/` （git clone 自 GitHub）
- Vault：`/root/obsidian/` （obsidian-headless 持续同步）
- Anki：`/opt/anki-venv/` + `/root/.local/share/Anki2/User 1/` + AnkiConnect :8765
- Claude CLI：`/usr/bin/claude` v2.1.141+（已登录，token 在 `/root/.claude/.credentials.json`）
- Codex CLI：`/usr/bin/codex` v0.130+（已登录，token 在 `/root/.codex/auth.json`）
- Python 3.10 / Node 22.13.1 (nvm) + 系统 Node 20.20.2
- `/root/claude/.env` 含所有路径环境变量

## 切换工作流的两种方式

### A. 长任务（推荐）—— tmux + Claude Code

```bash
ssh root@bwicarus.space
tmux new -s claude       # 或者 tmux attach -t claude 复用已有
cd /root/claude
claude                   # 进入交互模式
# 工作中如果要离开：Ctrl+B 然后 D（脱离 tmux，会话保留）
# 回来：ssh + tmux attach -t claude
```

好处：
- 断网 / 退出 SSH，Claude Code 会话保留在服务器
- 下次回来直接接着用
- 不依赖你 Windows 在线

### B. 一次性查询 —— 单次 prompt

```bash
ssh root@bwicarus.space 'cd /root/claude && claude -p "项目当前状态"'
```

好处：单次问答，不需要建立持久会话。

## 跟 Windows 上的 Claude Code 切换的注意点

| 维度 | Windows 端（你现在）| 服务器端（迁过去后） |
|---|---|---|
| 工作目录 | `C:\claude\` | `/root/claude/` |
| Claude Code 版本 | Opus 4.7 1M context | 默认（Sonnet 4.6）—— 要 Opus 加 `--model claude-opus-4-7` |
| 内存 / 上下文 | 完整保留（这个对话窗）| 全新会话 |
| 操作 Windows | 直接 | **无法**（除非反向 ssh 回 Windows） |
| 操作服务器 | ssh + 132ms RTT | **本地直接** bash / file 编辑（即时） |
| Memory（auto memory） | `~/.claude/projects/C--claude/memory/` (Windows 私有) | `/root/.claude/projects/root--claude/memory/`（服务器侧；用户切换时手动 scp 同步） |

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
    root@bwicarus.space:/root/.claude/projects/root--claude/

# 反向：服务器 → Windows（如果服务器侧也产生了新 memory）
scp -r root@bwicarus.space:/root/.claude/projects/root--claude/memory/ \
    ~/.claude/projects/C--claude/
```

注意：服务器侧的 project name 取决于 cwd（`/root/claude` → `root--claude`）。

## 服务器侧 Claude Code 该知道的关键事实

下次会话在服务器跑时，先让它**读这两个文件**就能恢复完整理解：
- `/root/claude/CLAUDE.md`
- `/root/claude/references/linux-server-migration.md`

也会自动读 `/root/.claude/projects/root--claude/memory/MEMORY.md`（如果有的话，要先 scp）。

## 常见操作快速参考

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
