"""Claude Code 实时额度查询（共享模块）。

详细文档：references/claude-code-quota-api.md
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

_CACHE: dict = {"data": None, "at": 0.0}


def _credentials_path() -> Path:
    """找 .credentials.json：先用 $HOME/.claude，回退 /root/.claude（root cron 跑时常用）。"""
    cands = [
        Path.home() / ".claude" / ".credentials.json",
        Path("/root/.claude/.credentials.json"),
    ]
    for c in cands:
        if c.exists():
            return c
    raise FileNotFoundError(f"找不到 .credentials.json: {cands}")


def _read_token() -> str:
    """每次现读 credentials.json，让 Claude Code 自动刷新 accessToken 生效。"""
    p = _credentials_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    return data["claudeAiOauth"]["accessToken"]


def fetch_quota(timeout: int = 10, cache_ttl: int = 5) -> dict:
    """查实时配额。零 token 消耗，但有 cache_ttl 秒缓存防高频。"""
    if _CACHE["data"] and time.time() - _CACHE["at"] < cache_ttl:
        return _CACHE["data"]
    token = _read_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.0.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    _CACHE.update(data=data, at=time.time())
    return data


def util_5h() -> float:
    """5h 窗口当前 utilization (0-100)；查询失败返回 100（保守拒绝）。"""
    try:
        q = fetch_quota()
        v = (q.get("five_hour") or {}).get("utilization")
        return float(v) if v is not None else 0.0
    except Exception:
        return 100.0


def util_7d_sonnet() -> float:
    try:
        q = fetch_quota()
        v = (q.get("seven_day_sonnet") or {}).get("utilization")
        return float(v) if v is not None else 0.0
    except Exception:
        return 100.0


def util_7d() -> float:
    try:
        q = fetch_quota()
        v = (q.get("seven_day") or {}).get("utilization")
        return float(v) if v is not None else 0.0
    except Exception:
        return 100.0


def can_run_more(target_5h_util: float = 80.0, target_7d_util: float = 90.0) -> tuple[bool, str]:
    """返回 (是否可以继续, 原因描述)。任一指标超 target 即停。"""
    try:
        q = fetch_quota()
    except Exception as ex:
        return False, f"quota 查询失败: {ex}"
    fh = (q.get("five_hour") or {}).get("utilization") or 0.0
    sd = (q.get("seven_day") or {}).get("utilization") or 0.0
    ss = (q.get("seven_day_sonnet") or {}).get("utilization") or 0.0
    if fh >= target_5h_util:
        return False, f"5h 窗口已达 {fh:.1f}% >= {target_5h_util:.1f}%"
    if sd >= target_7d_util:
        return False, f"7d 总窗口已达 {sd:.1f}% >= {target_7d_util:.1f}%"
    if ss >= target_7d_util:
        return False, f"7d Sonnet 窗口已达 {ss:.1f}% >= {target_7d_util:.1f}%"
    return True, f"OK (5h={fh:.0f}% 7d={sd:.0f}% sonnet={ss:.0f}%)"


if __name__ == "__main__":
    # 命令行模式：打印当前 quota
    import sys
    q = fetch_quota(cache_ttl=0)
    print(json.dumps(q, ensure_ascii=False, indent=2))
    ok, reason = can_run_more()
    print(f"\nbudget: {'OK ✓' if ok else 'BLOCKED ✗'} - {reason}", file=sys.stderr)
