"""
anki_sync_refresh.py — 周期触发：sync AnkiWeb，若复习数据有变化则刷新+部署仪表盘。

动机：用户在手机 AnkiDroid 等设备上的复习记录，只有服务器 sync 才能拉下来。
本脚本由 systemd timer 周期跑（默认每 15 分钟）：
  1. AnkiConnect sync（拉最新复习数据）
  2. 变化检测：今日复习数(getNumCardsReviewedToday) + 日期，与上次比较
  3. 有变化才跑轻量仪表盘刷新：anki_status --write-record → review_priority
     --write-record → export_dashboard → 部署到 webapp dashboard 目录

只读 Anki 卡片状态 + 写 records/dashboard，**不改 Anki 牌组**（不 build_review_deck、
不改写卡片），与手机并发复习安全。不写 frontmatter，避免每 15 分钟 churn vault 笔记。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

PROJECT = config.PROJECT_DIR
PYTHON = config.PYTHON if Path(config.PYTHON).exists() else sys.executable
ANKI_URL = config.ANKI_CONNECT_URL
STATE = PROJECT / "state" / "anki_sync_refresh_state.json"
WEBAPP_DASHBOARD = Path(os.environ.get(
    "WEBAPP_DASHBOARD_DIR", "/home/bwicarus/webapp/data/users/bwicarus/dashboard"))


def anki(action: str, params: dict | None = None, timeout: int = 120):
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(ANKI_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res["result"]


def run_py(script: str, args: list[str] | None = None) -> int:
    return subprocess.run(
        [PYTHON, str(PROJECT / "scripts" / script), *(args or [])],
        cwd=str(PROJECT),
    ).returncode


def main() -> int:
    # 1. 拉最新复习数据
    try:
        anki("sync")
    except Exception as e:
        print(f"AnkiWeb sync 失败：{e}（跳过本轮）", flush=True)
        return 1

    # 2. 变化检测
    today = datetime.date.today().isoformat()
    try:
        n = anki("getNumCardsReviewedToday")
    except Exception:
        n = -1
    last = {}
    if STATE.exists():
        try:
            last = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if last.get("date") == today and last.get("reviewed") == n:
        print(f"复习数据无变化（{today} reviewed={n}），跳过仪表盘刷新", flush=True)
        return 0

    print(f"复习数据变化 {last.get('reviewed')}→{n}（{today}），刷新仪表盘…", flush=True)

    # 3. 轻量刷新（不写 frontmatter，不动牌组）
    run_py("anki_status.py", ["--all", "--write-record", "--wait-seconds", "30"])
    run_py("review_priority.py", ["--write-record"])
    rc = run_py("export_dashboard.py")
    if rc != 0:
        print("export_dashboard 失败，不部署、不更新状态（下轮重试）", flush=True)
        return 1

    # 4. 部署到 webapp dashboard 目录
    WEBAPP_DASHBOARD.mkdir(parents=True, exist_ok=True)
    for fn in ("dashboard.json", "index.html"):
        src = PROJECT / "dashboard" / fn
        if src.exists():
            shutil.copy(src, WEBAPP_DASHBOARD / fn)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"date": today, "reviewed": n,
         "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")},
        ensure_ascii=False), encoding="utf-8")
    print("✓ 仪表盘已刷新并部署", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
