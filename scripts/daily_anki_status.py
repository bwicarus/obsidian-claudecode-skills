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


def _quota_snapshot() -> dict | None:
    """查 5h / 7d / sonnet / opus utilization。失败返 None（不阻断 step）。"""
    try:
        sys.path.insert(0, str(PROJECT_DIR / "scripts"))
        from lib.claude_quota import fetch_quota
        q = fetch_quota(cache_ttl=2)
        return {
            "five_hour":        (q.get("five_hour")        or {}).get("utilization", 0) or 0,
            "seven_day":        (q.get("seven_day")        or {}).get("utilization", 0) or 0,
            "seven_day_sonnet": (q.get("seven_day_sonnet") or {}).get("utilization", 0) or 0,
            "seven_day_opus":   (q.get("seven_day_opus")   or {}).get("utilization", 0) or 0,
        }
    except Exception:
        return None


def _append_quota_log(step_name: str, before: dict | None, after: dict | None,
                       duration_s: int) -> None:
    """每个 step 跑完追加一条日志到 state/quota_log.json。"""
    if before is None or after is None:
        return
    delta = {k: round(after[k] - before[k], 2) for k in before}
    # 只对有实际 AI 调用的 step 记（delta 任一字段 ≥ 0.5%）；其它写一行轻量
    has_consumption = any(abs(v) >= 0.5 for v in delta.values())
    log_file = PROJECT_DIR / "state" / "quota_log.json"
    log = {"entries": []}
    if log_file.exists():
        try: log = json.loads(log_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError): pass
    entries = log.setdefault("entries", [])
    entries.append({
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "step": step_name,
        "duration_s": duration_s,
        "before": before, "after": after, "delta": delta,
        "ai_intensive": has_consumption,
    })
    # 保留最近 500 条
    if len(entries) > 500:
        log["entries"] = entries[-500:]
    try:
        log_file.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def step(name: str, func) -> bool:
    start = datetime.datetime.now().astimezone()
    print(f"▶ {name} {start.strftime('%H:%M:%S')}...", flush=True)
    quota_before = _quota_snapshot()

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
    quota_after = _quota_snapshot()
    _append_quota_log(name, quota_before, quota_after, duration)
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
    # 打印 quota delta（如果有显著消耗）
    quota_msg = ""
    if quota_before and quota_after:
        delta_sonnet = quota_after["seven_day_sonnet"] - quota_before["seven_day_sonnet"]
        delta_opus = quota_after["seven_day_opus"] - quota_before["seven_day_opus"]
        if abs(delta_sonnet) >= 0.5 or abs(delta_opus) >= 0.5:
            quota_msg = f"  [quota Δsonnet {delta_sonnet:+.1f}% Δopus {delta_opus:+.1f}%]"
    print(f"  {'✓' if status == 'ok' else '✗'} {status} ({duration}s){quota_msg}{suffix}", flush=True)
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
    import json as _json
    kg_dir = PROJECT_DIR / "knowledge_graph"
    if not kg_dir.exists():
        print("  无 knowledge_graph 目录，跳过"); return 0
    kg_files = [f for f in sorted(kg_dir.glob("*.json")) if not f.name.endswith(".bak.json")]
    if not kg_files:
        print("  无 KG 文件，跳过"); return 0
    # 兜底：消化 register 留下的 pending_kg_sync.json（没成功关联或被 bug 跳过的笔记）
    pending_file = PROJECT_DIR / "state" / "pending_kg_sync.json"
    pending_paths: list = []
    if pending_file.exists():
        try:
            pending_paths = _json.loads(pending_file.read_text(encoding="utf-8")) or []
        except Exception as ex:
            print(f"  读 pending_kg_sync.json 失败：{ex}")
    if pending_paths:
        # touch 这些笔记的 mtime，让 link_with_ai --since-days 7 把它们重新纳入
        import time as _time
        now = _time.time()
        touched = 0
        for p in pending_paths:
            try:
                Path(p).touch(exist_ok=True)
                touched += 1
            except Exception:
                pass
        print(f"  消化 pending_kg_sync.json：touch {touched}/{len(pending_paths)} 篇笔记 mtime（强制纳入本次同步）")
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
        # 3) KG 准确性审计（深度模式：含 PDF 内容验证 + safe ops 自动 apply）
        # 按书开关 + 增量(只审变动节点):server-config["kg_audit"] 控制(控制面板可改)。
        #   enabled  全局总开关(默认 True)；books[<book>] 每本书开关(未列出用 default，默认 True)；
        #   incremental 只审新增/改过的节点(默认 True)，根治"每晚全图重审"的浪费。
        kac = server_cfg().get("kg_audit") or {}
        book = kg.stem
        audit_on = bool(kac.get("enabled", True)) and \
            bool((kac.get("books") or {}).get(book, kac.get("default", True)))
        if not audit_on:
            print(f"    审查未启用(book={book})，跳过")
        else:
            audit_args = [
                "--kg", str(kg), "--ai-sample-size", "20",
                "--model", "sonnet", "--effort", "medium", "--workers", "4",
                "--deep", "--auto-apply-safe",
                "--budget-loop",
                "--target-hour", "9", "--target-min", "0", "--buffer-min", "30",
                # 2026-05-26 调低：原 88（昨晚一夜 7d 涨 +7%，过高）→ 60（更保守）
                "--budget-target-7d", "60",
                "--budget-max-batches", "30",
                # 2026-06-17：can_run_aggressive 不看 5h 窗口，7d 周低点时单夜能把 5h 烧到 100%。
                # 加 5h 天花板 70%：单夜不独占整个 5h 窗口、不锁死白天用量。
                "--budget-5h-cap", "70",
            ]
            if kac.get("incremental", True):
                audit_args.append("--incremental")   # 只审变动/新增节点
            r3 = run_py("kg/audit_kg.py", audit_args)
            if r3 != 0:
                print(f"    audit_kg 失败 (rc={r3})")
        # 4) 滚动重扫 PDF（夜间深度扫描书本，每晚 30 页轮转）
        r4 = run_py("kg/rescan_rolling.py", [
            "--kg", str(kg),
            "--pages-per-night", "30",
            "--workers", "4", "--model", "sonnet", "--effort", "medium",
            "--target-hour", "9", "--target-min", "0", "--buffer-min", "30",
            "--auto-apply-safe",
        ])
        if r4 != 0:
            print(f"    rescan_rolling 失败 (rc={r4})")
    # 清空 pending_kg_sync.json（已经被本次 link_with_ai 通过 touch 触发）
    if pending_paths and pending_file.exists() and rc == 0:
        try:
            pending_file.write_text("[]", encoding="utf-8")
            print(f"  已清空 pending_kg_sync.json（消化 {len(pending_paths)} 篇）")
        except Exception as ex:
            print(f"  清空 pending_kg_sync.json 失败：{ex}")
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
    # 总开关:控制面板「凌晨定时」→「启用每日凌晨任务」。关掉则 timer 照常触发但本脚本立即空跑退出
    # (不碰 Anki / vault / dashboard / AI)。默认 True(缺字段=开,保持原行为)。
    if not server_cfg().get("daily", {}).get("enabled", True):
        print("⏸ daily 总开关已关闭(server-config daily.enabled=false),跳过本次运行。", flush=True)
        write_run("skipped")
        return 0
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
    step("登记新笔记", lambda: run_py("register_notes.py", ["--no-update-kg"]))
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
    # 概念网三步已分离到 concept-graph.timer(scripts/concept_graph_daily.py)——
    # 不碰 Anki,不该被 daily 的 Anki 顾虑连坐(用户拍板,2026-07-19)
    # 知识图谱：先 AI 关联（精准）+ 再算 mastery / state（每个 KG 文件一次）
    step("KG 关联+掌握度", run_kg_link_mastery)
    # 领域词典(从 KG/目录/查词长出来)→ 融合权重反向学习(词典金标准)→ 跨语言概念归一(AI 判词义)
    step("通用语停用词", lambda: run_py("build_auto_stopwords.py", ["--write", "--show", "0"]))
    step("领域词典", lambda: run_py("attention_profile.py", ["--domain-dict"]))
    step("融合权重学习", lambda: run_py("attention_profile.py", ["--fit"]))
    step("跨语言概念归一", lambda: run_py("attention_profile.py", ["--concepts"]))
    # 学习近况:扫行为信号(Anki 连错/自测低分)→ 生成困难档案 → AI 后台补怀疑&建议 → 消解转态
    step("学习近况", lambda: run_py("learning_situations.py", ["--daily"]))
    # 错误模式元画像:归纳跨知识点的系统性弱点(证明弱/定义混/术语对应不清)
    step("错误模式元画像", lambda: run_py("error_meta_profile.py", ["--gen"]))
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
