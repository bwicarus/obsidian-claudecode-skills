"""读取主项目的 state/active_tasks.json — 任务监视悬浮窗的数据源。

主项目里 task_tracker.py 的 track(...) 上下文管理器写这个 JSON：
  - tasks[]: 当前活跃任务（含 progress / log / detail / pid）
  - completed[]: 最近 N 秒内完成的任务（带 duration / summary）

客户端只读，不写。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

COMPLETION_DISPLAY_S = 60   # 完成任务保留显示时长


def _is_pid_alive_windows(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return True   # 出错保守认为活着，避免误隐


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_active_tasks(state_file: str | Path) -> list[dict]:
    data = _read_state(Path(state_file))
    tasks = data.get("tasks", []) or []
    return [t for t in tasks if _is_pid_alive_windows(int(t.get("pid", 0)))]


def load_completed_tasks(state_file: str | Path) -> list[dict]:
    data = _read_state(Path(state_file))
    rows = data.get("completed", []) or []
    cutoff = time.time() - COMPLETION_DISPLAY_S
    return [r for r in rows if r.get("completed_at", 0) >= cutoff]


def fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    return f"{seconds // 3600}时{(seconds % 3600) // 60:02d}分"
