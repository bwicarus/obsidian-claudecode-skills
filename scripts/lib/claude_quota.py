"""Claude Code 实时额度查询（共享模块）。

详细文档：references/claude-code-quota-api.md
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta
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


# ─────────────────────────────────────────────────────────────────────────
# 时间感知激进模式：用 5h 滑动窗口性质，保证 target_time 时 util ≈ 0
#
# 关键洞察：Claude API 的 5h 窗口是"过去 5 小时所有请求"的累积，
# 不是 fixed reset。所以 target_time 时 util = 在 (target_time - 5h, target_time)
# 这个区间内发的所有请求。
#
# 推论：只要在 target_time - 5h 之前停跑 AI，target_time 时 5h util 自然 = 0。
# 加个 buffer（默认 30 分钟）防止网络延迟 / 跨时区误差。
# ─────────────────────────────────────────────────────────────────────────

def time_to_safe_cutoff(target_hour: int = 9, target_min: int = 0,
                        buffer_min: int = 30,
                        now: datetime | None = None) -> tuple[bool, str, float]:
    """返回 (能否继续跑, 原因描述, 距离 cutoff 的分钟数)。
    cutoff = target_time - 5h - buffer_min。"""
    if now is None:
        now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_min,
                         second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    safe_cutoff = target - timedelta(hours=5, minutes=buffer_min)
    delta_min = (safe_cutoff - now).total_seconds() / 60
    if delta_min <= 0:
        return (False,
                f"已过安全截止（{safe_cutoff.strftime('%H:%M')} = "
                f"早 {target_hour}:{target_min:02d} - 5h - {buffer_min}min buffer）",
                delta_min)
    return (True,
            f"距截止 {safe_cutoff.strftime('%H:%M')} 还有 {delta_min:.0f} 分钟",
            delta_min)


def can_run_aggressive(target_hour: int = 9, target_min: int = 0,
                       buffer_min: int = 30,
                       target_7d_util: float = 88.0,
                       now: datetime | None = None) -> tuple[bool, str]:
    """激进模式综合判定：时间到 cutoff + 7d 窗口未爆。
    5h 阈值不用——只要在 cutoff 前，5h 涨多少都没事（target_time 时自然过期）。"""
    ok_time, reason_time, _ = time_to_safe_cutoff(target_hour, target_min, buffer_min, now)
    if not ok_time:
        return False, reason_time
    try:
        q = fetch_quota()
    except Exception as ex:
        return False, f"quota 查询失败: {ex}"
    sd = (q.get("seven_day") or {}).get("utilization") or 0.0
    ss = (q.get("seven_day_sonnet") or {}).get("utilization") or 0.0
    if sd >= target_7d_util:
        return False, f"7d 总窗口 {sd:.1f}% >= {target_7d_util:.1f}%"
    if ss >= target_7d_util:
        return False, f"7d Sonnet {ss:.1f}% >= {target_7d_util:.1f}%"
    return True, f"OK 7d={sd:.0f}% sonnet={ss:.0f}% · {reason_time}"


if __name__ == "__main__":
    # 命令行模式：打印当前 quota
    import sys
    q = fetch_quota(cache_ttl=0)
    print(json.dumps(q, ensure_ascii=False, indent=2))
    ok, reason = can_run_more()
    print(f"\nbudget: {'OK ✓' if ok else 'BLOCKED ✗'} - {reason}", file=sys.stderr)
