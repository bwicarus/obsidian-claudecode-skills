#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bw_scheduler — 自建定时任务调度（2026-09-14 用户设计：比官方更自由、直接接通知）。

任务目录：%LOCALAPPDATA%\\BWReader\\scheduled-tasks\\<id>\\flow.json（+ runs.jsonl、memory.md）。
调度只管「什么时候」：`tick()` 由语音核心运行器每 30 秒调一次，到期就把 bw_flow_runner.py 起成
独立子进程（不阻塞语音）；下次时间按 schedule 算好写回 registry.json。

schedule 形状：
  {"type":"daily","time":"12:00"}                      每天
  {"type":"weekly","days":["MO","WE"],"time":"09:00"}  每周几
  {"type":"hourly","intervalHours":6}                  每 N 小时
  {"type":"once","at":"2026-09-15T10:00"}              一次性（跑完自动 disabled）
  {"type":"manual"}                                    只手动跑
官方 automation.toml 的 rrule 可用 `from_rrule()` 转过来。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
TASKS_ROOT = LOCALAPPDATA / "BWReader" / "scheduled-tasks"
REGISTRY = TASKS_ROOT / "registry.json"
RUNNER = LOCALAPPDATA / "BWReader" / "bw_flow_runner.py"
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _python() -> str:
    exe = sys.executable or "python"
    return str(Path(exe).with_name("python.exe")) if exe.lower().endswith("pythonw.exe") else exe


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")[:2]
    return int(h), int(m)


def next_run(schedule: dict, after: _dt.datetime | None = None) -> _dt.datetime | None:
    after = after or _now()
    t = (schedule or {}).get("type")
    if t == "daily":
        h, m = _parse_hm(schedule.get("time", "09:00"))
        cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
        return cand if cand > after else cand + _dt.timedelta(days=1)
    if t == "weekly":
        h, m = _parse_hm(schedule.get("time", "09:00"))
        days = [d for d in (schedule.get("days") or []) if d in WEEKDAYS] or WEEKDAYS
        for delta in range(0, 8):
            cand = (after + _dt.timedelta(days=delta)).replace(hour=h, minute=m, second=0, microsecond=0)
            if WEEKDAYS[cand.weekday()] in days and cand > after:
                return cand
        return None
    if t == "hourly":
        n = max(1, int(schedule.get("intervalHours") or 1))
        base = after.replace(minute=int(schedule.get("minute") or 0), second=0, microsecond=0)
        cand = base
        while cand <= after:
            cand += _dt.timedelta(hours=n)
        return cand
    if t == "once":
        at = _dt.datetime.fromisoformat(schedule["at"])
        if at.tzinfo is None:
            at = at.astimezone()
        return at if at > after else None
    return None   # manual


def from_rrule(rrule: str) -> dict:
    """官方 automation.toml 的 RRULE → 我们的 schedule。"""
    body = rrule.split(":", 1)[1] if rrule.upper().startswith("RRULE:") else rrule
    parts = dict(kv.split("=", 1) for kv in body.split(";") if "=" in kv)
    hour = int(parts.get("BYHOUR", "9")); minute = int(parts.get("BYMINUTE", "0"))
    time_s = "%02d:%02d" % (hour, minute)
    freq = parts.get("FREQ", "DAILY").upper()
    if parts.get("COUNT") == "1":
        return {"type": "manual", "note": "原 rrule COUNT=1，一次性任务已过期"}
    if freq == "WEEKLY":
        days = [d for d in parts.get("BYDAY", "").split(",") if d in WEEKDAYS]
        if len(days) == 7 or not days:
            return {"type": "daily", "time": time_s}
        return {"type": "weekly", "days": days, "time": time_s}
    if freq == "HOURLY":
        return {"type": "hourly", "intervalHours": int(parts.get("INTERVAL", "1")), "minute": minute}
    return {"type": "daily", "time": time_s}


# ---------- registry ----------
def load_registry() -> dict:
    try:
        d = json.loads(REGISTRY.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("tasks"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"tasks": {}}


def save_registry(reg: dict) -> None:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY)


def task_dirs() -> list[Path]:
    if not TASKS_ROOT.exists():
        return []
    return sorted(p for p in TASKS_ROOT.iterdir() if p.is_dir() and (p / "flow.json").exists())


def read_flow(task_id: str) -> dict:
    return json.loads((TASKS_ROOT / task_id / "flow.json").read_text(encoding="utf-8"))


def upsert(task_id: str, flow: dict, enabled: bool = True) -> dict:
    if not SAFE_ID.match(task_id):
        raise ValueError("任务 id 只能是字母数字-_，且以字母数字开头")
    sys.path.insert(0, str(RUNNER.parent))
    import bw_flow_runner  # noqa: WPS433
    errs = bw_flow_runner.lint(flow)
    if errs:
        raise ValueError("flow 不合规：" + "; ".join(errs))
    d = TASKS_ROOT / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "flow.json").write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
    reg = load_registry()
    rec = reg["tasks"].setdefault(task_id, {})
    rec["enabled"] = bool(enabled)
    nxt = next_run(flow.get("schedule") or {})
    rec["nextRunAt"] = nxt.isoformat(timespec="seconds") if nxt else None
    rec["updatedAt"] = _now().isoformat(timespec="seconds")
    save_registry(reg)
    return describe(task_id)


def delete(task_id: str) -> bool:
    import shutil
    d = TASKS_ROOT / task_id
    reg = load_registry()
    reg["tasks"].pop(task_id, None)
    save_registry(reg)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def describe(task_id: str) -> dict:
    flow = read_flow(task_id)
    rec = load_registry()["tasks"].get(task_id, {})
    runs = last_runs(task_id, 1)
    return {"id": task_id, "name": flow.get("name"), "summary": flow.get("summary"), "schedule": flow.get("schedule"),
            "model": flow.get("model"), "enabled": rec.get("enabled", True), "nextRunAt": rec.get("nextRunAt"),
            "lastRunAt": rec.get("lastRunAt"), "lastStatus": rec.get("lastStatus"), "running": bool(rec.get("runningPid")),
            "steps": [s.get("id") for s in flow.get("steps", [])], "lastSummary": (runs[0].get("summary") if runs else None)}


def list_tasks() -> list[dict]:
    out = []
    for d in task_dirs():
        try:
            out.append(describe(d.name))
        except Exception as exc:  # noqa: BLE001
            out.append({"id": d.name, "error": str(exc)[:200]})
    return out


def last_runs(task_id: str, limit: int = 10) -> list[dict]:
    p = TASKS_ROOT / task_id / "runs.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------- 执行 ----------
def _pid_alive(pid: int) -> bool:
    try:
        # 不用 text=True：tasklist 按系统代码页输出（中文 Windows = cp936），按 utf-8 解会炸
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=10)
        return ('"%d"' % pid).encode("ascii") in (r.stdout or b"")
    except Exception:
        return False


def start_run(task_id: str, reason: str = "scheduled") -> dict:
    """起一个独立子进程跑这条任务；立即返回。结果由 tick 回收（runs.jsonl 最后一行）。"""
    reg = load_registry()
    rec = reg["tasks"].setdefault(task_id, {"enabled": True})
    pid = rec.get("runningPid")
    if pid and _pid_alive(int(pid)):
        return {"ok": False, "reason": "already-running", "pid": pid}
    d = TASKS_ROOT / task_id
    log = open(d / "last-run.log", "w", encoding="utf-8")
    proc = subprocess.Popen([_python(), str(RUNNER), "run", str(d)], stdout=log, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0), cwd=str(d))
    rec["runningPid"] = proc.pid
    rec["lastRunAt"] = _now().isoformat(timespec="seconds")
    rec["lastReason"] = reason
    rec["lastStatus"] = "running"
    flow = read_flow(task_id)
    nxt = next_run(flow.get("schedule") or {})
    rec["nextRunAt"] = nxt.isoformat(timespec="seconds") if nxt else None
    if (flow.get("schedule") or {}).get("type") == "once":
        rec["enabled"] = False
    save_registry(reg)
    return {"ok": True, "pid": proc.pid, "nextRunAt": rec["nextRunAt"]}


def tick() -> list[dict]:
    """每 30 秒一拍：回收结束的运行、起到期的任务。返回本拍发生的事。"""
    events: list[dict] = []
    reg = load_registry()
    changed = False
    now = _now()
    for d in task_dirs():
        tid = d.name
        rec = reg["tasks"].setdefault(tid, {"enabled": True})
        pid = rec.get("runningPid")
        if pid and not _pid_alive(int(pid)):
            runs = last_runs(tid, 1)
            rec["runningPid"] = None
            rec["lastStatus"] = (runs[0].get("status") if runs else "unknown")
            rec["lastSummary"] = (runs[0].get("summary") if runs else None)
            events.append({"task": tid, "event": "finished", "status": rec["lastStatus"], "summary": rec.get("lastSummary")})
            changed = True
        if not rec.get("enabled", True) or rec.get("runningPid"):
            continue
        try:
            flow = read_flow(tid)
        except Exception:
            continue
        nxt_s = rec.get("nextRunAt")
        if not nxt_s:
            nxt = next_run(flow.get("schedule") or {})
            rec["nextRunAt"] = nxt.isoformat(timespec="seconds") if nxt else None
            changed = True
            continue
        try:
            nxt = _dt.datetime.fromisoformat(nxt_s)
        except ValueError:
            rec["nextRunAt"] = None; changed = True; continue
        if nxt <= now:
            save_registry(reg)   # start_run 会重读并写回
            r = start_run(tid, "scheduled")
            events.append({"task": tid, "event": "started", **r})
            reg = load_registry()
            changed = False
    if changed:
        save_registry(reg)
    return events


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="定时任务调度")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run"); r.add_argument("task_id")
    sub.add_parser("tick")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print(json.dumps(list_tasks(), ensure_ascii=False, indent=2))
    elif a.cmd == "run":
        print(json.dumps(start_run(a.task_id, "manual"), ensure_ascii=False))
    else:
        print(json.dumps(tick(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
