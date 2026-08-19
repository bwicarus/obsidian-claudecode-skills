# Pi SSH 客户端 Snippets

iPad / Mac 等 SSH 客户端的 Snippets 侧栏建议存这几条。**所有命令假设你已经
`ssh bwicarus@100.101.15.57` 进 Pi**（或者通过 Tailscale hostname
`bwicarus.taile44d0c.ts.net`）。

## 🔁 日常重连 Claude Code 会话

```bash
tmux new -A -s claude
```

`-A` = 没会话就建、有就 attach。一条命令通杀首次和续接，**这是最常用的**。

进 tmux 后如果是空 shell，跑：

```bash
cd ~/claude && claude
```

`claude` 命令已经默认开启 Remote Control（你 `/config` 改的全局开关），iPad
Claude.ai App 的 Code 标签会看到这个会话。

## 🚀 接续已有 session（仅首次切到 Pi 时用）

```bash
cd ~/claude && claude --resume <session-id>
```

session ID 从 `claude --resume` 不带参数时的交互列表里挑，或者直接指明。

## 📊 看 Pi 服务状态

```bash
```bash
systemctl status webapp voice-rt qa-server anki-headless obsidian-sync xvfb-99 nginx \
  mcp-server bwicarus-daily.timer concept-graph.timer --no-pager -l | head -60
```

`voice-rt`（:8767 语音 realtime 中继）是 `deploy_reader.sh` 的健康门禁之一，别漏看。
```

输出每个 unit 的 active / inactive / failed，含最近几行 journal。

## 🕒 看上次 daily 跑得怎么样

```bash
cat ~/claude/state/last_run.json | python3 -m json.tool | head -40
```

15 步全 ok 的话 `"status": "ok"` 在顶部。

## ▶️ 手动触发完整 daily

```bash
sudo systemctl start bwicarus-daily.service && sudo journalctl -u bwicarus-daily.service -f
```

`Ctrl+C` 只停 tail，daily 在后台继续跑完。

## 🚨 重启某个服务

```bash
sudo systemctl restart qa-server
```

换 `webapp` / `anki-headless` / `obsidian-sync` / `nginx` / `xvfb-99` 之一。

## 🔄 git pull 最新代码 + 重启 webapp

```bash
## 🔄 git pull 最新代码 + 部署

⚠ `app.py` / `control.py` / `_server_deploy/templates/*.html` 里的 7 个阅读器模板都在**部署清单内**，
必须走原子部署，不要手工 cp：

```bash
cd ~/claude && git pull && bash scripts/deploy_reader.sh
```

清单外文件（`templates/control.html`、`insights.py`、`fitness*.py`、`qa_server.py` …）才手工 cp：

```bash
cp ~/webapp/templates/control.html ~/deploy-backups/manual/control.html.$(date +%s)
cp _server_deploy/templates/control.html ~/webapp/templates/ && sudo systemctl restart webapp.service
```

判类：`python3 scripts/reader_deploy_manifest.py | cut -f1 | grep -F '<你改的文件>'`，
完整规则见 [`deployment-workflow.md`](deployment-workflow.md)。nginx 改动不在这条里。
```

部署 control 面板 / nav.js 改动一条龙。nginx 配置改动不在这条里（要 reload nginx 单独执行）。

## 🌐 验证 HTTPS 入口可达

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://bwicarus.taile44d0c.ts.net/control/
```

预期 `302`（未登录重定向到 /login）。在 Pi 自己上跑因为 MagicDNS 解析到 100.101.15.57。

## 🔐 看 Tailscale 证书剩多久

```bash
echo | openssl s_client -connect 127.0.0.1:443 -servername bwicarus.taile44d0c.ts.net 2>/dev/null | openssl x509 -noout -dates
```

90 天有效，Tailscale 会自动续。

## 📦 跑 smoke tests

```bash
cd ~/claude && /usr/bin/python3 -m unittest discover tests -v 2>&1 | tail -10
```

预期结尾 `OK`（当前 `tests/` 有 124 个 `test_*.py`、约 1300 个用例，耗时以实际为准；**别拿固定的用例数当通过标准**，只看有没有 `FAILED`/`ERROR`）。

## 🗂️ 看 vault / Anki 规模

```bash
echo "vault: $(find ~/obsidian -name '*.md' | wc -l) md / $(du -sh ~/obsidian | cut -f1)"
echo "anki:  $(curl -s -d '{"action":"deckNames","version":6}' http://127.0.0.1:8765 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d[\"result\"]),\"decks\")')"
echo "anki:  $(curl -s -d '{"action":"findCards","version":6,"params":{"query":"deck:*"}}' http://127.0.0.1:8765 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d[\"result\"]),\"cards\")')"
```

## 🧠 看跨会话 auto memory

```bash
# 取最新的 memory 目录（不依赖目录名编码）
MEMDIR=$(ls -td ~/.claude/projects/*/memory 2>/dev/null | head -1)
ls "$MEMDIR"
cat "$MEMDIR/MEMORY.md"
```

`MEMORY.md` 是索引，逐个 `.md` 是具体记忆。当前活动目录是
`~/.claude/projects/-home-bwicarus-claude/memory/`（前导单个连字符，目录名编码自
`/home/bwicarus/claude`）；旧的 `home-bwicarus--claude/`（双连字符）已冻结，里面是
2026-05-15 的旧 feedback 条目，别照那个看。

## ⚙️ iPad 触发 URL 模板

控制面板 → 设置 → 「iPad 远程触发」section 里有所有 URL（含 API key）的复制按钮。
也可以直接命令行打：

```bash
echo "key: $(cat ~/claude/state/qa-server-data/cmd_server_key.txt)"
TS=bwicarus.taile44d0c.ts.net
echo "QA 浏览器:    http://$TS:9091/"
echo "/qa 注入:     http://$TS:9090/qa?key=$(cat ~/claude/state/qa-server-data/cmd_server_key.txt)"
echo "register:    http://$TS:9090/run/register?key=$(cat ~/claude/state/qa-server-data/cmd_server_key.txt)"
echo "daily:       http://$TS:9090/run/daily?key=$(cat ~/claude/state/qa-server-data/cmd_server_key.txt)"
echo "ankiweb-sync: http://$TS:9090/run/ankiweb-sync?key=$(cat ~/claude/state/qa-server-data/cmd_server_key.txt)"
```

## 🆘 紧急：tmux session 撞名

`duplicate session: claude` → 已有同名 session：

```bash
tmux attach -t claude        # 直接进
# 或者杀掉重建（只在确认旧 session 没东西时）
tmux kill-session -t claude
```
