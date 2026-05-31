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
| `User-Agent` | `claude-cli/<ver>`，如 `claude-cli/2.0.0`（端点不校验具体版本号，填任意版本都行；本机实装是 2.1.158，仓库 lib 里沿用 2.0.0 占位）|

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
  "five_hour":        {"utilization": 5.0,  "resets_at": "2026-05-31T09:50:00.442035+00:00"},
  "seven_day":        {"utilization": 57.0, "resets_at": "2026-06-01T14:00:00.442059+00:00"},
  "seven_day_sonnet": {"utilization": 53.0, "resets_at": "2026-06-01T14:00:00.442070+00:00"},
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
| `resets_at` | 该窗口下次重置的 ISO8601 UTC 时间（实测带微秒 + `+00:00` offset，不是 `Z` 结尾，如 `2026-05-31T09:50:00.442035+00:00`；标准 ISO8601 解析器都能吃，别按 `Z` 结尾硬匹配）|
| 没用过的模型/计划字段 | 返回 `null` |
| `extra_usage` | 超出基础额度后的付费 credits 状态 |

---

## 配套端点（同样的 Bearer auth）

| 端点 | 用途 |
|---|---|
| `/api/oauth/usage` | **实时配额**（本文主角）|
| `/api/oauth/profile` | 账号信息，**顶层是 `account` / `organization` / `application` 三个嵌套 dict**（不是平铺）。`account.has_claude_max` / `account.has_claude_pro`；`organization.rate_limit_tier` / `organization.subscription_status` / `organization.organization_type` / `organization.billing_type` / `organization.subscription_created_at` |
| `/api/oauth/account/settings` | 账户设置 |
| `/api/claude_code/policy_limits` | 组织级策略上限（个人订阅看不到啥）|

⚠️ `/api/rate-limits` 是 404，**别用**。

---

## 端点发现方法

```bash
# 版本无关：直接 strings 掉 which claude 解析出的真实 binary
strings "$(readlink -f "$(which claude)")" \
  | grep -oE '/api/[a-z_/-]+' | sort -u
```

新版 Claude Code 走 **native installer**，binary 是原生 ELF，路径在 `~/.local/share/claude/versions/<ver>`（本机为 `2.1.158`，**无 `.exe` 扩展**），不是 npm 包。旧的 npm 安装才在 `$(npm root -g)/@anthropic-ai/claude-code/bin/` 下（且 `.exe` 后缀只对 Windows 成立）。

binary 把所有 endpoint 路径以明文字符串嵌着，挨个 curl 试就知道哪个真实存在哪个 404。

---

## 注意事项

1. **非公开端点** — Anthropic 可能改路径/字段/收紧权限。遇到 401/403/404 优雅降级，不要当主路径依赖
2. **不要把 accessToken 透给前端** — 服务端代理即可。token 等同于你 Claude Code 账号的完整凭证
3. **加缓存** — 这种数据 30s 一次足够，避免高频打外网被限流
4. **每次现读 `credentials.json`** — 不要缓存到内存，会错过 Claude Code 自动刷新
5. **`per-call usage` 是另一回事** — `claude -p --output-format json` 的响应里有 `total_cost_usd`、`usage.{input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens}`、`modelUsage` 按模型分项。那是**本次调用花了多少**，跟 `/oauth/usage` 的**累计 utilization** 互补——可以一起记账

---

## 本仓库已封装：`scripts/lib/claude_quota.py`

**生产代码直接 `import lib.claude_quota` 即可，下面「集成模式」一节的示例只是教学参考，不要照着重写一遍轮子。**

共享模块封装了取 token（先 `$HOME/.claude`，回退 `/root/.claude`）+ 查询 + 缓存 + 各种 gate 判定：

| 函数 | 作用 |
|---|---|
| `fetch_quota(timeout=10, cache_ttl=5)` | 查 `/oauth/usage`，5s 内存缓存防高频；返回原始 dict |
| `util_5h()` / `util_7d()` / `util_7d_sonnet()` | 取对应窗口 utilization（0-100）；**查询失败返回 100**（保守拒绝）|
| `can_run_more(target_5h_util=80.0, target_7d_util=90.0)` | `(可否继续, 原因)`；任一窗口超阈值即停 |
| `time_to_safe_cutoff(target_hour=9, target_min=0, buffer_min=30, now=None)` | 时间感知截止：因 5h 是滑动窗口，只要在 `target - 5h - buffer` 前停跑，target 时 5h util 自然≈0 |
| `can_run_aggressive(target_hour=9, ..., target_7d_util=88.0)` | 激进模式：cutoff 前不看 5h，只看 7d 窗口未爆 |

调用方：`_server_deploy/control.py`（`/control/api/quota-now` 路由）、`scripts/daily_anki_status.py`、`scripts/kg/audit_kg.py`、`scripts/kg/rescan_rolling.py`。`python -m scripts.lib.claude_quota`（`__main__`）会打印当前 quota + `can_run_more` 判定。

---

## 集成模式

> 以下为**参考实现**（说明原理），生产请直接 `import lib.claude_quota`（见上节）。

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
