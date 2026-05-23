# Claude Code 实时额度查询 API

Anthropic 在 OAuth 上暴露了 Claude Code 订阅的实时配额使用率端点。**未公开文档，**从 `claude` binary 的字符串里挖出来。

**关键性质**：纯元数据查询，**零 token 消耗**——可以任意频率轮询不会扣额度。

---

## 端点

```
GET https://api.anthropic.com/api/oauth/usage
```

## 认证

用 Claude Code 本地保存的 **OAuth accessToken**，不是 API key。

Token 存在 `~/.claude/.credentials.json`（Linux/server 上一般是这个文件，权限 `600`；macOS 上有时也在 Keychain）：

```json
{
  "claudeAiOauth": {
    "accessToken":  "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt":    1234567890123,
    "scopes":       [...],
    "subscriptionType": "max",
    "rateLimitTier": "default_claude_max_20x"
  }
}
```

Claude Code 自己会用 `refreshToken` 自动续期 `accessToken`——**每次现读 `credentials.json` 拿最新的 `accessToken` 就行，别自己管刷新逻辑**。

## 必需 Header

三个 header 缺一不可（缺 `anthropic-beta` 或 `User-Agent` 可能被拒）：

| Header | 值 |
|---|---|
| `Authorization` | `Bearer <accessToken>` |
| `anthropic-beta` | `oauth-2025-04-20` |
| `User-Agent` | `claude-cli/2.0.0` |

## 完整 curl 示例

```bash
TOKEN=$(python3 -c "import json; print(json.load(open('/root/.claude/.credentials.json'))['claudeAiOauth']['accessToken'])")

curl -sS https://api.anthropic.com/api/oauth/usage \
  -H "Authorization: Bearer $TOKEN" \
  -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'User-Agent: claude-cli/2.0.0'
```

## 响应格式

```json
{
  "five_hour":        {"utilization": 5.0,  "resets_at": "2026-05-23T17:30:00Z"},
  "seven_day":        {"utilization": 57.0, "resets_at": "2026-05-25T13:59:59Z"},
  "seven_day_sonnet": {"utilization": 53.0, "resets_at": "2026-05-25T14:00:00Z"},
  "seven_day_opus":   null,
  "seven_day_oauth_apps": null,
  "seven_day_cowork":     null,
  "seven_day_omelette":   {"utilization": 0.0, "resets_at": null},
  "tangelo": null,
  "iguana_necktie": null,
  "omelette_promotional": null,
  "extra_usage": {
    "is_enabled": true,
    "monthly_limit": null,
    "used_credits": 5086.0,
    "utilization": null,
    "currency": "USD",
    "disabled_reason": null
  }
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `utilization` | 0-100 百分比 |
| `resets_at` | 该窗口下次重置的 ISO8601 UTC 时间 |
| 没用过的模型/计划字段 | 返回 `null` |
| `extra_usage` | 超出基础额度后的付费 credits 状态 |

---

## 配套端点（同样的 Bearer auth）

| 端点 | 用途 |
|---|---|
| `/api/oauth/usage` | **实时配额**（本文主角）|
| `/api/oauth/profile` | 账号信息：`has_claude_max` / `has_claude_pro` / `rate_limit_tier` / `subscription_status` |
| `/api/oauth/account/settings` | 账户设置 |
| `/api/claude_code/policy_limits` | 组织级策略上限（个人订阅看不到啥）|

⚠️ `/api/rate-limits` 是 404，**别用**。

---

## 端点发现方法

```bash
# claude binary 位置：which claude 链接的目标，通常在 npm root 全局下
strings $(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe \
  | grep -oE '/api/[a-z_/-]+' | sort -u
```

binary 把所有 endpoint 路径以明文字符串嵌着，挨个 curl 试就知道哪个真实存在哪个 404。

---

## 注意事项

1. **非公开端点** — Anthropic 可能改路径/字段/收紧权限。遇到 401/403/404 优雅降级，不要当主路径依赖
2. **不要把 accessToken 透给前端** — 服务端代理即可。token 等同于你 Claude Code 账号的完整凭证
3. **加缓存** — 这种数据 30s 一次足够，避免高频打外网被限流
4. **每次现读 `credentials.json`** — 不要缓存到内存，会错过 Claude Code 自动刷新
5. **`per-call usage` 是另一回事** — `claude -p --output-format json` 的响应里有 `total_cost_usd`、`usage.{input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens}`、`modelUsage` 按模型分项。那是**本次调用花了多少**，跟 `/oauth/usage` 的**累计 utilization** 互补——可以一起记账

---

## 集成模式

### 服务端 Python 代理（带缓存）

```python
import json, time, urllib.request
from pathlib import Path

_CACHE = {"data": None, "at": 0}

def get_claude_usage(ttl: int = 30) -> dict:
    if _CACHE["data"] and time.time() - _CACHE["at"] < ttl:
        return _CACHE["data"]
    cred = Path("~/.claude/.credentials.json").expanduser()
    token = json.loads(cred.read_text())["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.0.0",
        })
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    _CACHE.update(data=data, at=time.time())
    return data
```

### 前端展示（仪表盘 chip）

```js
async function refreshClaudeQuota() {
  const d = await (await fetch("/your/proxy/claude-quota")).json();
  const fh = d.five_hour?.utilization        ?? 0;
  const sd = d.seven_day?.utilization        ?? 0;
  const ss = d.seven_day_sonnet?.utilization ?? 0;
  const max = Math.max(fh, sd, ss);
  el.textContent = `5h:${fh}% · 7d:${sd}% · S:${ss}%`;
  el.style.color = max >= 90 ? "#dc2626" :
                   max >= 70 ? "#f59e0b" : "#10b981";
}
setInterval(refreshClaudeQuota, 60_000);
refreshClaudeQuota();
```

### 批量任务的 gate（防撞限流）

跑批量 Claude 调用前先查：

```python
usage = get_claude_usage()
if (usage.get("five_hour") or {}).get("utilization", 0) >= 90:
    raise RuntimeError("5h 额度 ≥ 90%，跳过批量任务避免撞限流")
```
