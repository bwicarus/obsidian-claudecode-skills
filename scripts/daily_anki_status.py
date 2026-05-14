#!/usr/bin/env python3
"""daily_anki_status.py — Linux 版每日 Anki 状态更新编排。

等价主项目 daily_anki_status.ps1，跑：
  0 确保 AnkiConnect 可用（必要时 systemctl restart anki-headless）
  1 register_notes.py
  2 anki_status.py --all
  3 review_priority.py
  4 build_review_deck.py
  5 cleanup_orphans.py --apply
  6 export_dashboard.py
  7 部署 dashboard 到 /root/webapp/data/dashboard/（本机直接 cp，不需要 SCP）
  8 AnkiConnect sync (AnkiWeb)

进度写到 state/last_run.json（每步实时更新）。
通过 systemd timer 凌晨 4:00 自动触发，或者 webapp /control/ 按钮触发。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/root/claude"))
PYTHON = os.environ.get("APP_PYTHON", "/usr/bin/python3")
ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
WEBAPP_DASHBOARD = Path("/root/webapp/data/dashboard")
LAST_RUN = PROJECT_DIR / "state" / "last_run.json"
BACKUP_DIR = PROJECT_DIR / "state" / "backup"

RUN_START = datetime.datetime.now().astimezone()
STEPS: list[dict] = []


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_run(status: str = "running") -> None:
    LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(
        json.dumps(
            {
                "kind": "daily_anki_status",
                "started_at": RUN_START.isoformat(timespec="seconds"),
                "updated_at": now_iso(),
                "status": status,
                "steps": STEPS,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def ankiconnect(action: str, params: dict | None = None, timeout: int = 10) -> dict:
    body = json.dumps({"action": action, "version": 6, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(
        ANKI_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ensure_anki() -> bool:
    """ping AnkiConnect；不通则 systemctl restart anki-headless 重试，最多 3 次。"""
    for i in range(3):
        try:
            r = ankiconnect("version", timeout=5)
            if r.get("error") is None:
                return True
        except Exception:
            pass
        print(f"  AnkiConnect 不可达（尝试 {i + 1}/3），重启 anki-headless...")
        subprocess.run(["systemctl", "restart", "anki-headless"], check=False)
        time.sleep(20)
    return False


def step(name: str, func) -> bool:
    start = datetime.datetime.now().astimezone()
    print(f"▶ {name} {start.strftime('%H:%M:%S')}...")
    rc = 0
    err: str | None = None
    try:
        ret = func()
        if isinstance(ret, int):
            rc = ret
    except Exception as e:
        rc = -1
        err = str(e)
    end = datetime.datetime.now().astimezone()
    duration = int((end - start).total_seconds())
    status = "ok" if rc == 0 and not err else "failed"
    STEPS.append(
        {
            "name": name,
            "status": status,
            "rc": rc,
            "error": err,
            "started_at": start.isoformat(timespec="seconds"),
            "ended_at": end.isoformat(timespec="seconds"),
            "duration_s": duration,
        }
    )
    write_run("running")
    suffix = f" {err}" if err else ""
    print(f"  {'✓' if status == 'ok' else '✗'} {status} ({duration}s){suffix}")
    return status == "ok"


def run_py(script: str, args: list[str] | None = None) -> int:
    args = args or []
    r = subprocess.run(
        [PYTHON, str(PROJECT_DIR / "scripts" / script)] + args,
        cwd=str(PROJECT_DIR),
    )
    return r.returncode


def deploy_dashboard() -> int:
    WEBAPP_DASHBOARD.mkdir(parents=True, exist_ok=True)
    for fn in ["dashboard.json", "index.html"]:
        src = PROJECT_DIR / "dashboard" / fn
        if src.exists():
            shutil.copy(src, WEBAPP_DASHBOARD / fn)
    return 0


def sync_ankiweb() -> int:
    r = ankiconnect("sync", timeout=120)
    return 0 if r.get("error") is None else -1


def rotate_backups() -> None:
    """每日备份 note-states.json 和 anki/records，保留 7 天。"""
    today = datetime.date.today().strftime("%Y%m%d")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    note_states = PROJECT_DIR / "state" / "note-states.json"
    if note_states.exists():
        shutil.copy(note_states, BACKUP_DIR / f"note-states-{today}.json")

    records = PROJECT_DIR / "anki" / "records"
    archive = BACKUP_DIR / f"anki-records-{today}.zip"
    if records.exists() and not archive.exists():
        shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=records)

    # 滚动保留 7 天
    for pattern in ["note-states-*.json", "anki-records-*.zip"]:
        files = sorted(BACKUP_DIR.glob(pattern), reverse=True)
        for f in files[7:]:
            f.unlink()


def main() -> int:
    print(f"=== Linux daily 编排开始 {RUN_START.strftime('%Y-%m-%d %H:%M:%S')} ===")
    write_run("running")
    rotate_backups()

    step("确保 AnkiConnect", lambda: 0 if ensure_anki() else -1)
    step("登记新笔记", lambda: run_py("register_notes.py"))
    step(
        "更新 Anki 状态",
        lambda: run_py(
            "anki_status.py",
            ["--all", "--write-frontmatter", "--write-record", "--wait-seconds", "60"],
        ),
    )
    step(
        "计算复习优先级",
        lambda: run_py("review_priority.py", ["--write-frontmatter", "--write-record"]),
    )
    step("重建必复习牌组", lambda: run_py("build_review_deck.py"))
    step("清理孤儿", lambda: run_py("cleanup_orphans.py", ["--apply"]))
    step("导出仪表板", lambda: run_py("export_dashboard.py"))
    step("部署仪表板", deploy_dashboard)
    step("AnkiWeb 同步", sync_ankiweb)

    failed = [s for s in STEPS if s["status"] != "ok"]
    final = "failed" if failed else "ok"
    write_run(final)
    print(f"{'✓' if final == 'ok' else '✗'} 完成 {now_iso()} (失败 {len(failed)} 步)")
    return 0 if final == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
