# iPad 快捷指令切换到服务器（替代本机 Windows）

把 iPad 拍照 + 截图问答的目标从 Windows 100.x 切到服务器的 100.x。

> ⚠ **本文写于 VPS 时代，命令里的 `bwicarus.space` / `root@` 已过时**：VPS 自 2026-06-10 起暂停（只留公网 webapp，自动化全 disable），当前唯一活跃实例是 **Pi**（`bwicarus.taile44d0c.ts.net`，ssh 别名 `pi`，用户 `bwicarus` 而非 root）。照做前把主机与用户换成 Pi 的；Tailscale IP 也要换成 Pi 的 `100.101.15.57`。

## 前置：Tailscale 授权

服务器装了 Tailscale 但**需要你点击授权 URL** 完成账号绑定：

```
https://login.tailscale.com/a/157fd9bd01d1e9
```
（这个 URL 一次性，过期重新跑 `tailscale up` 拿新 URL）

授权后服务器加入你的 tailnet，自动拿到一个 `100.x.x.x` 的私网 IP。

确认：

```bash
ssh root@bwicarus.space 'tailscale status; tailscale ip -4'
```

应该输出形如 `100.64.x.x` 的 IP（记为 `$SERVER_TS_IP`）。

## iPad 快捷指令改两个地方

打开 iPad 上每个跟"截图问答 / 笔记登记 / 上传网页"相关的快捷指令，找到 HTTP 请求步骤：

### 1. 改 URL 里的 IP

**原来**：`http://<本机 Windows 的 100.x>:9090/qa?key=...`
**改成**：`http://<$SERVER_TS_IP>:9090/qa?key=...`

类似地把 `:9091` 浏览器跳转的 URL 也改：
- 原：`http://<本机 100.x>:9091/`
- 改：`http://<$SERVER_TS_IP>:9091/`

### 2. 改 API key

**服务器侧 cmd_server API key 不一样**（独立生成，跟 Windows 客户端的 key 没关系）。

```bash
ssh root@bwicarus.space 'cat /root/claude/state/qa-server-data/cmd_server_key.txt'
```

复制这个 key 替换快捷指令里 `?key=...` 后面那一串。

## 各类快捷指令对应

| iPad 快捷指令 | 服务器端点 | Method | Body |
|---|---|---|---|
| 截图问答（推图）| `http://$SERVER_TS_IP:9090/qa?key=<KEY>` | POST | `{"image_b64": "<base64 图片>"}` |
| 浏览器看对话页 | `http://$SERVER_TS_IP:9091/` | GET | （iPad Safari 打开）|
| 触发登记新笔记 | `http://$SERVER_TS_IP:9090/run/register?key=<KEY>` | POST | （无 body）|
| 触发完整 daily | `http://$SERVER_TS_IP:9090/run/daily?key=<KEY>` | POST | （无 body）|
| 触发 AnkiWeb 同步 | `http://$SERVER_TS_IP:9090/run/ankiweb-sync?key=<KEY>` | POST | （无 body）|
| 查可用 commands | `http://$SERVER_TS_IP:9090/list?key=<KEY>` | GET | — |

## 测试链路

1. 浏览器打开 `http://$SERVER_TS_IP:9091/` —— 应该看到截图问答的完整对话页（跟你 Windows 上按 ctrl+shift+q 弹出的页面**一模一样**）
2. 跑「截图问答」快捷指令（拍照 + JPEG 转换 + POST）—— 浏览器页面应该自动出现截图
3. 在浏览器输入问题，看 AI 是否回答（服务器用 `claude_cli` 后端）
4. 点「保存到 vault」—— `/root/obsidian/习题/` 或 `/root/obsidian/错题/` 应该有新笔记（obsidian-sync 几秒后会同步到你本机 + iPad Obsidian 客户端）

## 故障排查

| 症状 | 检查 |
|---|---|
| iPad 浏览器连不上 9091 | 服务器 `systemctl status qa-server` 是否 active；`tailscale status` 是否 idle/active |
| POST 9090 返回 403 `forbidden` | API key 错（cmd_server `_auth` 失败固定返回 403，不会返回 401）；重新 `cat /root/claude/state/qa-server-data/cmd_server_key.txt` |
| POST 9090 返回 404 `/qa` | 服务器 cmd_server 没跑 —— `systemctl restart qa-server` |
| 截图注入成功但浏览器看不到 | qa_browser daemon 没拿到新截图 —— `journalctl -u qa-server -n 30` 看 inject 日志 |
| AI 没回答 / 报错 | 服务器 AI CLI 凭据问题，看 `/root/claude/state/logs/ai_calls.log` |
| 保存到 vault 不出现在 iPad | obsidian-sync 同步慢，等几秒；或者 `systemctl status obsidian-sync` |

## 控制面板设置

也可以在 `https://bwicarus.space/control/` 网页里改服务器开关：

- **AnkiConnect 自动重启**（anki.auto_restart）
- **登记后自动上传**（auto_upload_after_register）
- **凌晨任务的 wake_anki / upload_after**
- **iPad 远程截图问答总开关**（qa_remote_daemon）
- **习题/错题子目录**

改完点「保存设置」自动生效（涉及 vault 路径或 qa_remote_daemon 的改动会重启 qa-server）。

## 保留本机 Windows 客户端

按你的策略"服务器端确定稳定再切"，本机 Windows + bwicarus-client.exe **仍在跑**。建议：

1. iPad 先**复制**一个快捷指令（不删原来的），新的指向服务器
2. 跑一周新版，观察服务器 daily / qa 是否稳定
3. 稳定后再删除/禁用本机 Windows 的相关功能（Windows 计划任务 + bwicarus-client.exe）

---

**当前所有相关服务状态**（在 `https://bwicarus.space/control/` 实时看）：

| Service | 用途 |
|---|---|
| `tailscaled` | Tailscale 客户端 |
| `xvfb-99` | Anki 的虚拟显示 |
| `anki-headless` | Anki + AnkiConnect :8765 |
| `obsidian-sync` | vault 持续同步 |
| `qa-server` | iPad 截图问答 daemon :9091 + cmd_server :9090 |
| `webapp` | Flask + 控制面板 |
| `bwicarus-daily.timer` | 每天 01:00 自动 daily（原 04:00，73d8eb6 起提前到用户不用 AI 的空闲时段）|
