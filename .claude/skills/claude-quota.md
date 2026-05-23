# Skill: claude-quota

查询 Claude Code 订阅的实时配额使用率（5 小时窗口 / 7 天窗口 / Sonnet 子窗口 / extra credits）。**零 token 消耗**——是元数据查询，可以随便调。

## 触发方式

用户输入 `/claude-quota` 或问"我的 Claude 额度还剩多少"、"还能跑多久"等类似问题。

## 执行（最快路径）

直接 bash 一行出结果：

```bash
TOKEN=$(python3 -c "import json; print(json.load(open('/root/.claude/.credentials.json'))['claudeAiOauth']['accessToken'])")
curl -sS https://api.anthropic.com/api/oauth/usage \
  -H "Authorization: Bearer $TOKEN" \
  -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'User-Agent: claude-cli/2.0.0' | python3 -m json.tool
```

如果 credentials.json 路径不一样：用 `~/.claude/.credentials.json` 通用展开（root 用户在 `/root/.claude/.credentials.json`，普通用户在 `$HOME/.claude/.credentials.json`）。

## 响应解读

返回 JSON，重要字段：

- `five_hour.utilization` — 5 小时窗口使用率（0-100%）
- `seven_day.utilization` — 7 天总窗口使用率
- `seven_day_sonnet.utilization` — Sonnet 模型单独的 7 天窗口
- `extra_usage.used_credits` — 超出基础额度后用掉的付费 credits（USD）
- `*.resets_at` — 各窗口的下次重置 ISO8601 UTC 时间

向用户汇报时挑前 3 个最重要的展示，并标注离重置还有多久（按当前时间算 delta）。

## 红线（防撞限流）

- 任一窗口 ≥ 90% → 强烈建议用户停下批量任务等重置
- 70-90% → 提醒"剩余不多注意分配"
- < 70% → 正常使用

## 完整文档

[`references/claude-code-quota-api.md`](../../references/claude-code-quota-api.md) — 端点细节、所有配套端点、Python 代理模板、前端 chip 模板、注意事项

## 注意

- token 等同账号完整凭证，**不要把 accessToken 输出到日志或前端**
- 缓存 30s 起步，别高频轮询（虽然 0 token 但仍是外网请求）
- 这是**非公开端点**，Anthropic 可能下线/改字段——遇到 401/403/404 优雅降级
