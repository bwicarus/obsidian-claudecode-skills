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
# webapp /dashboard/ 走 _serve_user：读 data/users/<u>/dashboard/ →
# fallback data/dashboard_template/。所以 deploy 目标必须是 users 私有目录，
# 不是 data/dashboard（那目录没人读，曾导致仪表盘一直显示旧快照）。
WEBAPP_DASHBOARD = Path(os.environ.get(
    "WEBAPP_DASHBOARD_DIR", "/root/webapp/data/users/bwicarus/dashboard"
))
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
    print(f"▶ {name} {start.strftime('%H:%M:%S')}...", flush=True)

    # 步骤开始时立刻写 running 状态，让前端 last_run.json 轮询能看到"正在跑"
    STEPS.append(
        {
            "name": name,
            "status": "running",
            "rc": None,
            "error": None,
            "started_at": start.isoformat(timespec="seconds"),
            "ended_at": None,
            "duration_s": None,
        }
    )
    write_run("running")

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
    # 更新刚刚 append 的 running 记录
    STEPS[-1].update(
        {
            "status": status,
            "rc": rc,
            "error": err,
            "ended_at": end.isoformat(timespec="seconds"),
            "duration_s": duration,
        }
    )
    write_run("running")
    suffix = f" {err}" if err else ""
    print(f"  {'✓' if status == 'ok' else '✗'} {status} ({duration}s){suffix}", flush=True)
    return status == "ok"


def run_py(script: str, args: list[str] | None = None) -> int:
    args = args or []
    r = subprocess.run(
        [PYTHON, str(PROJECT_DIR / "scripts" / script)] + args,
        cwd=str(PROJECT_DIR),
    )
    return r.returncode


def run_kg_link_mastery() -> int:
    """对 knowledge_graph/*.json 每个 KG：
    1. AI 关联（增量，只跑最近 7 天动过的笔记）
    2. mastery + state + 反向传递（无 AI）
    3. KG 准确性审计（C 启发式 + D 单调性 + B AI 抽样 20 节点）
    """
    kg_dir = PROJECT_DIR / "knowledge_graph"
    if not kg_dir.exists():
        print("  无 knowledge_graph 目录，跳过"); return 0
    kg_files = [f for f in sorted(kg_dir.glob("*.json")) if not f.name.endswith(".bak.json")]
    if not kg_files:
        print("  无 KG 文件，跳过"); return 0
    rc = 0
    for kg in kg_files:
        print(f"  KG: {kg.name}")
        # 1) AI 关联增量（--since-days 7：只跑近 7 天动过的笔记）
        r1 = run_py("kg/link_with_ai.py", [
            "--kg", str(kg), "--model", "sonnet", "--effort", "medium",
            "--workers", "4", "--since-days", "7", "--in-place",
        ])
        if r1 != 0:
            print(f"    AI 关联失败 (rc={r1})，仍跑后续")
        # 2) mastery + state
        r2 = run_py("kg/link_and_mastery.py", ["--kg", str(kg), "--in-place"])
        if r2 != 0:
            print(f"    link_and_mastery 失败 (rc={r2})"); rc = r2
        # 3) KG 准确性审计
        r3 = run_py("kg/audit_kg.py", [
            "--kg", str(kg), "--ai-sample-size", "20",
            "--model", "sonnet", "--effort", "medium", "--workers", "4",
        ])
        if r3 != 0:
            print(f"    audit_kg 失败 (rc={r3})")
    return rc


def server_cfg() -> dict:
    p = PROJECT_DIR / "state" / "server-config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cfg_int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _refresh_step(cfg_key: str, task: str, build_args) -> int:
    """通用：读 server-config[cfg_key]，未启用跳过；启用则
    refresh_weak_cards.py --task <task> --apply + 各自参数。"""
    c = server_cfg().get(cfg_key) or {}
    if not c.get("enabled"):
        print(f"  {cfg_key} 未启用，跳过")
        return 0
    args = ["--task", task, "--apply",
            "--limit", str(_cfg_int(c.get("limit"), 5)),
            "--cooldown-days", str(_cfg_int(c.get("cooldown_days"), 30))]
    args += build_args(c)
    return run_py("refresh_weak_cards.py", args)


def step_weak() -> int:
    return _refresh_step(
        "weak_card_refresh", "weak", lambda c:
        ["--min-lapses", str(_cfg_int(c.get("min_lapses"), 3)),
         "--escalate-lapses", str(_cfg_int(c.get("escalate_lapses"), 2))]
        + (["--apply-escalation"] if c.get("auto_escalate") else []))


def step_antimodel() -> int:
    return _refresh_step(
        "card_antimodel", "antimodel", lambda c:
        ["--min-stability-days", str(_cfg_int(c.get("min_stability_days"), 60)),
         "--min-reps", str(_cfg_int(c.get("min_reps"), 5))])


def step_quality() -> int:
    return _refresh_step(
        "card_quality", "quality", lambda c:
        ["--max-back-len", str(_cfg_int(c.get("max_back_len"), 280)),
         "--hard-again-ratio", str(c.get("hard_again_ratio") or "0.4"),
         "--min-reviews", str(_cfg_int(c.get("min_reviews"), 4)),
         "--sample-per-run", str(_cfg_int(c.get("sample_per_run"), 3))]
        + ([] if c.get("relative_threshold", True) else ["--abs-threshold"])
        + (["--apply-escalation"] if c.get("auto_split") else []))


def run_smoke_tests() -> int:
    """跑 tests/ 下的 smoke tests。任何一个 fail → 返回非零 → step 标记 failed。

    daily timer 在已知坏环境（路由名打错、部署没同步、关键脚本崩了）下继续
    跑后续 6 步 ankibase modifying 操作风险高，所以 main() 会在这步 fail
    时 early-return 不动 Anki。
    """
    tests_dir = PROJECT_DIR / "tests"
    if not tests_dir.exists():
        return 0       # 测试目录不在，跳过（向后兼容）
    r = subprocess.run(
        [PYTHON, "-m", "unittest", "discover", "tests"],
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

    # Step -1: smoke tests 守门。fail 立即 abort，不动 Anki / vault / dashboard。
    if not step("smoke tests", run_smoke_tests):
        write_run("failed")
        print(
            "✗ smoke tests 失败，abort daily（不跑后续 ankibase modifying 步骤）",
            flush=True,
        )
        return 1

    step("确保 AnkiConnect", lambda: 0 if ensure_anki() else -1)
    # 读 Anki 数据前先从 AnkiWeb 拉最新（手机 AnkiDroid 等其它设备的复习记录），
    # 否则 anki_status / review_priority 算的是本地陈旧数据。
    # sync 失败不阻断（离线 / AnkiWeb 临时挂时仍按本地数据继续，总比不跑好）。
    step("AnkiWeb 同步（拉最新）", sync_ankiweb)
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
    step("薄弱卡 AI 改写", step_weak)
    step("已掌握卡换问法", step_antimodel)
    step("卡片质量体检", step_quality)
    step("重建必复习牌组", lambda: run_py("build_review_deck.py"))
    step("清理孤儿", lambda: run_py("cleanup_orphans.py", ["--apply"]))
    # 知识图谱：先 AI 关联（精准）+ 再算 mastery / state（每个 KG 文件一次）
    step("KG 关联+掌握度", run_kg_link_mastery)
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
