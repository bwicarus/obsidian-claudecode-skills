#!/usr/bin/env python3
"""book-ocr.service 健康度自检(systemd timer 每 15 分钟跑一次)。

报警条件:
- service active 但 progress.json > 30 分钟未更新 → OCR 卡死
- service inactive 但进度还没到 100% → 异常退出
- systemd journal 最近 30 分钟内 fail 次数 ≥ 3 → fail-loop 苗头
- progress.json 存在但 sidecar 数 30 分钟内没增长 → I/O 异常

正常 → 写 /tmp/book-ocr-health.json,删 alert
异常 → 写 /tmp/book-ocr-alert.json + log
严重(fail ≥ 5) → 主动 systemctl stop 防烧资源

日志: state/logs/book-ocr-watchdog.log(只 append,小)
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

SIDECAR_DIR = Path("/home/bwicarus/claude/state/mokuro-ocr/4eda8f8cdc69834f")
PROGRESS = SIDECAR_DIR / "progress.json"
HEALTH = Path("/tmp/book-ocr-health.json")
ALERT = Path("/tmp/book-ocr-alert.json")
HISTORY = Path("/tmp/book-ocr-watchdog-history.json")
LOG = Path("/home/bwicarus/claude/state/logs/book-ocr-watchdog.log")


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def sh(cmd: list[str] | str) -> tuple[int, str, str]:
    r = subprocess.run(
        cmd, capture_output=True, text=True, shell=isinstance(cmd, str)
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def count_sidecars() -> int:
    return sum(
        1
        for f in glob.glob(str(SIDECAR_DIR / "p*.json"))
        if re.match(r"p\d+\.json$", Path(f).name)
    )


def main() -> None:
    issues: list[str] = []
    info: dict = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # service 状态
    _, status, _ = sh(["systemctl", "is-active", "book-ocr"])
    info["service_status"] = status

    # progress.json 最近更新时间 + 内容
    if PROGRESS.exists():
        age_min = (time.time() - PROGRESS.stat().st_mtime) / 60
        info["progress_age_min"] = round(age_min, 1)
        try:
            p = json.loads(PROGRESS.read_text(encoding="utf-8"))
            info["completed"] = p.get("completed")
            info["total"] = p.get("total")
            info["percent"] = p.get("percent")
            info["eta_hours"] = p.get("eta_hours")
            info["avg_s_per_page"] = p.get("avg_seconds_per_page")
        except Exception as ex:
            issues.append(f"progress.json 解析失败: {ex}")
        if status == "active" and age_min > 30:
            issues.append(f"progress.json {age_min:.0f}min 未更新, service 可能卡死")
    else:
        if status == "active":
            issues.append("service active 但 progress.json 不存在")

    # sidecar 数(可推断真实进度)
    info["sidecars"] = count_sidecars()

    # journalctl 最近 30 分钟 Failed 次数
    _, out, _ = sh(
        'journalctl -u book-ocr --since "30 min ago" --no-pager 2>/dev/null '
        '| grep -c "Failed with result"'
    )
    try:
        fail_count = int(out)
    except ValueError:
        fail_count = 0
    info["recent_fails"] = fail_count
    if fail_count >= 3:
        issues.append(f"最近 30 分钟 fail {fail_count} 次, fail-loop 苗头")

    # service inactive 但进度未到 100% → 异常退出
    if status == "inactive" and info.get("completed") is not None:
        completed = info.get("completed") or 0
        total = info.get("total") or 1
        if completed < total:
            issues.append(f"service inactive 但 {completed}/{total} 未完成")

    # 与上次对比:sidecar 数 30 分钟内没增长(配合 progress age)
    last = {}
    if HISTORY.exists():
        try:
            last = json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            last = {}
    if last and status == "active":
        prev_sidecars = last.get("sidecars", 0)
        prev_checked = last.get("checked_at_unix", 0)
        gap_min = (time.time() - prev_checked) / 60
        if gap_min >= 14 and info["sidecars"] == prev_sidecars:
            issues.append(
                f"sidecar 数 {gap_min:.0f} 分钟内未增长 ({prev_sidecars})"
            )

    info["issues"] = issues

    HEALTH.write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    info_for_history = {**info, "checked_at_unix": time.time()}
    HISTORY.write_text(
        json.dumps(info_for_history, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if issues:
        ALERT.write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"ALERT: {issues}")
        # 严重: fail-loop ≥ 5 次 → 主动 stop 防烧资源
        if fail_count >= 5:
            log(f"fail_count={fail_count}, 主动 stop book-ocr")
            sh(["systemctl", "stop", "book-ocr"])
    else:
        if ALERT.exists():
            ALERT.unlink()
        log(
            f"OK: {info.get('completed', '?')}/{info.get('total', '?')} "
            f"({info.get('percent', '?')}%) ETA {info.get('eta_hours', '?')}h "
            f"avg {info.get('avg_s_per_page', '?')}s/page"
        )


if __name__ == "__main__":
    main()
