"""Google API 配额本地计数器。

谁用:任何调 Google API 的脚本(YouTube Data / Vision / Translate 等)。
做法:每次调用前 log_usage(service, units, action),累计到 SQLite。

YouTube Data API 重置:每天 Pacific Time 午夜(无夏令时变 PST,夏令时变 PDT)。
对应北京时间约 16:00(PDT 夏令时)或 17:00(PST 冬令时)。

units 表(常用):
- search.list:           100
- videos.list:           1
- channels.list:         1
- captions.list:         50
- captions.download:     200
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get(
    "GOOGLE_QUOTA_DB",
    "/home/bwicarus/claude/state/google_api_quota.db",
))

# 各 service 的每日免费配额(0 / None = 无固定日限,只计量不算百分比)
DAILY_LIMITS = {
    "youtube": 10_000,      # search.list 每次 100 units
    "vision":  1_000,       # 我们 OCR 用的
    "gemini":  None,        # 付费(prepay)按 token 计费、无固定日限;助手侧 units 记的是 token 数(只累计用量,真余额去 AI Studio billing 控制台看)
    "stt":     None,        # Cloud Speech-to-Text 无固定免费日限(按 Free Trial 赠金计费)
}

# 重置时区(PT)
PT_RESET_HOUR_UTC = 8   # PDT (UTC-7) 午夜 = 07:00 UTC;PST (UTC-8) 午夜 = 08:00 UTC
                         # 取 8 安全(PST 准 / PDT 早 1 小时)


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("""
        CREATE TABLE IF NOT EXISTS quota_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            units INTEGER NOT NULL,
            action TEXT,
            note TEXT,
            ts_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_svc_ts ON quota_log(service, ts_utc)")
    return db


def log_usage(service: str, units: int, action: str = "", note: str = "") -> None:
    """记录一次 API 调用的 quota 消耗。"""
    db = _db()
    db.execute(
        "INSERT INTO quota_log (service, units, action, note) VALUES (?, ?, ?, ?)",
        (service.lower(), int(units), action, note),
    )
    db.commit()
    db.close()


def _current_quota_window_start_utc() -> datetime:
    """当前配额窗口的起点(上一次 PT 午夜的 UTC 时刻)。"""
    now = datetime.now(timezone.utc)
    today_reset = now.replace(hour=PT_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now < today_reset:
        return today_reset - timedelta(days=1)
    return today_reset


def report(service: str = "youtube") -> dict:
    """返回 service 当前配额窗口的使用情况。"""
    db = _db()
    window_start = _current_quota_window_start_utc()
    iso = window_start.strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(
        "SELECT COALESCE(SUM(units),0), COUNT(*) FROM quota_log "
        "WHERE service=? AND ts_utc >= ?",
        (service.lower(), iso),
    ).fetchone()
    used, calls = int(rows[0]), int(rows[1])
    limit = DAILY_LIMITS.get(service.lower(), 0) or 0   # None/缺省都归一成 0 = 无固定日限
    remaining = max(0, limit - used) if limit else None
    next_reset = window_start + timedelta(days=1)
    secs = (next_reset - datetime.now(timezone.utc)).total_seconds()
    # 最近 5 条
    last = db.execute(
        "SELECT ts_utc, units, action FROM quota_log "
        "WHERE service=? AND ts_utc >= ? ORDER BY ts_utc DESC LIMIT 5",
        (service.lower(), iso),
    ).fetchall()
    db.close()
    return {
        "service": service.lower(),
        "window_start_utc": iso,
        "next_reset_utc": next_reset.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds_to_reset": int(secs),
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "calls": calls,
        "recent": [{"ts": r[0], "units": r[1], "action": r[2]} for r in last],
    }


# Gemini 单价(USD / 1M tokens),(输入, 输出)。付费档,粗略;改价直接改这表。
# 助手日志 note 里带 model=,按这表逐条算钱;未知模型回退 flash 价。
GEMINI_PRICES = {
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro":        (1.25, 10.00),   # ≤200k 上下文档;超大上下文更贵,这里不细分
    "gemini-2.0-flash":      (0.10, 0.40),
}
_GEM_DEFAULT_PRICE = (0.30, 2.50)

def _parse_note(note: str) -> dict:
    """从 note 'model=X in=N out=M status=...' 解析。"""
    d = {}
    for tok in (note or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1); d[k] = v
    return d

def gemini_cost(days: int = 0) -> dict:
    """累计 Gemini 估算花费(USD),**按每条记录的 model 单价分别算输入/输出**。
    days>0 只统计近 days 天;0=全部历史。返回 {usd, in_tokens, out_tokens, calls, by_model}。"""
    db = _db()
    if days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.execute("SELECT note FROM quota_log WHERE service='gemini' AND ts_utc>=?", (since,)).fetchall()
    else:
        rows = db.execute("SELECT note FROM quota_log WHERE service='gemini'").fetchall()
    db.close()
    usd = 0.0; tin = tout = calls = 0; by_model = {}; free_tok = 0; free_calls = 0
    for (note,) in rows:
        d = _parse_note(note)
        ti = int(d.get("in", 0) or 0); to = int(d.get("out", 0) or 0)
        if ti == 0 and to == 0:   # 旧记录/失败行没 token → 跳过(不计费)
            continue
        if d.get("tier") == "free":   # 免费档:不花钱,只累计用量(成本只算付费档)
            free_tok += ti + to; free_calls += 1
            continue
        mdl = d.get("model", "") or "gemini-2.5-flash"
        pin, pout = GEMINI_PRICES.get(mdl, _GEM_DEFAULT_PRICE)
        c = ti / 1e6 * pin + to / 1e6 * pout
        usd += c; tin += ti; tout += to; calls += 1
        m = by_model.setdefault(mdl, {"usd": 0.0, "in": 0, "out": 0, "calls": 0})
        m["usd"] += c; m["in"] += ti; m["out"] += to; m["calls"] += 1
    for m in by_model.values():
        m["usd"] = round(m["usd"], 4)
    return {"usd": round(usd, 4), "in_tokens": tin, "out_tokens": tout, "calls": calls,
            "by_model": by_model, "free_tokens": free_tok, "free_calls": free_calls}


def fmt_secs(s: int) -> str:
    if s < 0: return "已重置(刷一下脚本)"
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"


def _cli():
    """命令行:python google_api_quota.py [service]"""
    service = sys.argv[1] if len(sys.argv) > 1 else "youtube"
    r = report(service)
    print(f"=== {r['service'].upper()} Data API 配额 ===")
    print(f"  窗口起点(UTC): {r['window_start_utc']}")
    print(f"  下次重置(UTC): {r['next_reset_utc']} (约 {fmt_secs(r['seconds_to_reset'])} 后)")
    if r['limit']:
        pct = r['used'] / r['limit'] * 100
        print(f"  已用: {r['used']:,} units / 限额 {r['limit']:,}  ({pct:.1f}%)")
        print(f"  剩余: {r['remaining']:,} units")
    else:
        print(f"  已用: {r['used']:,} units / 限额 无固定日限")
    print(f"  调用次数: {r['calls']}")
    if r["recent"]:
        print(f"  最近 5 条:")
        for x in r["recent"]:
            print(f"    [{x['ts']}] +{x['units']:>4} {x['action']}")


if __name__ == "__main__":
    _cli()
