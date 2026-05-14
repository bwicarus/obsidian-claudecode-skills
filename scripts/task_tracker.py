"""
task_tracker.py — 任务进度追踪模块

各脚本通过 `with track("任务名"):` 上下文管理器报告活跃任务。
状态写入 state/active_tasks.json：
    {
      "tasks":     [...],   # 进行中
      "completed": [...]    # 最近完成（保留 COMPLETION_DISPLAY_S 秒供监视窗口显示）
    }
由 任务监视.exe 半透明窗口实时显示。

任务监视器会自动清理 PID 已不存在的孤儿条目（脚本崩溃后残留）和过期的完成条目。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "active_tasks.json"

# 当 stdout 被重定向到非 TTY（例如 control 面板用 subprocess.Popen 把脚本输出导向 log 文件）时，
# 把 Handle 的 update / log / set_summary 同步 print 出来，方便从日志看运行过程。
# 交互式 TTY（用户 ssh 直接跑）下保持安静，仍由进度窗口 / 监视窗口展示。
try:
    _MIRROR_TO_STDOUT = not sys.stdout.isatty()
except (ValueError, AttributeError):
    _MIRROR_TO_STDOUT = False


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return True


COMPLETION_DISPLAY_S = 60  # 任务完成后保留多久供监视窗口显示


def cleanup_stale() -> list[dict]:
    """剔除 PID 已不存在的孤儿条目并持久化，返回清理后的活跃列表。"""
    tasks = [t for t in _load() if _is_pid_alive(int(t.get("pid", 0) or 0))]
    _save(tasks)
    return tasks


def _load_full() -> dict:
    if not STATE_FILE.exists():
        return {"tasks": [], "completed": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "tasks":     data.get("tasks", []),
            "completed": data.get("completed", []),
        }
    except (json.JSONDecodeError, OSError):
        return {"tasks": [], "completed": []}


def _load() -> list[dict]:
    """活跃任务（向后兼容）。"""
    return _load_full()["tasks"]


def _load_completed() -> list[dict]:
    return _load_full()["completed"]


def _save(tasks: list[dict], completed: list[dict] | None = None) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if completed is None:
        completed = _load_completed()
    # 清理过期的完成条目
    cutoff = int(time.time()) - COMPLETION_DISPLAY_S
    completed = [c for c in completed if c.get("completed_at", 0) >= cutoff]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"tasks": tasks, "completed": completed},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def start(name: str, detail: str = "") -> str:
    task_id = uuid.uuid4().hex
    entry = {
        "id":         task_id,
        "name":       name,
        "detail":     detail,
        "started_at": int(time.time()),
        "pid":        os.getpid(),
    }
    tasks = _load()
    tasks.append(entry)
    _save(tasks)
    return task_id


def end(task_id: str, summary: str | None = None) -> None:
    """任务结束：从活跃列表移到完成列表，附时长和可选汇总。
    完成条目在 COMPLETION_DISPLAY_S 秒后由 _save 自动清理。"""
    full = _load_full()
    tasks = full["tasks"]
    completed = full["completed"]
    finished = next((t for t in tasks if t.get("id") == task_id), None)
    remaining = [t for t in tasks if t.get("id") != task_id]
    if finished:
        finished["completed_at"] = int(time.time())
        finished["duration_s"] = finished["completed_at"] - int(finished.get("started_at", finished["completed_at"]))
        if summary:
            finished["summary"] = summary
        completed.append(finished)
    _save(remaining, completed)


def set_summary(task_id: str, summary: str) -> None:
    """运行中的任务可以预设结束汇总文本。"""
    tasks = _load()
    for t in tasks:
        if t.get("id") == task_id:
            t["summary"] = summary
            break
    _save(tasks)


def update(task_id: str, detail: str) -> None:
    tasks = _load()
    for t in tasks:
        if t.get("id") == task_id:
            t["detail"] = detail
            break
    _save(tasks)


def set_progress(task_id: str, current: int, total: int) -> None:
    tasks = _load()
    for t in tasks:
        if t.get("id") == task_id:
            t["progress"] = {"current": int(current), "total": int(total)}
            break
    _save(tasks)


MAX_LOG_ENTRIES = 5


def log(task_id: str, message: str) -> None:
    tasks = _load()
    for t in tasks:
        if t.get("id") == task_id:
            entries = t.get("log", [])
            entries.append(message)
            t["log"] = entries[-MAX_LOG_ENTRIES:]
            break
    _save(tasks)


@contextmanager
def track(name: str, detail: str = ""):
    """用法：
        with track("登记新笔记") as h:
            h.set_progress(0, 5)
            for i in range(5):
                h.update(f"第 {i+1} 步")
                ...
                h.log(f"✓ 第 {i+1} 步完成")
                h.set_progress(i + 1, 5)
            h.set_summary("处理 5 篇")  # 可选：完成后展示的汇总
    """
    tid = start(name, detail)

    class Handle:
        def update(self, msg: str) -> None:
            update(tid, msg)
            if _MIRROR_TO_STDOUT:
                print(f"▶ {msg}", flush=True)

        def set_progress(self, current: int, total: int) -> None:
            set_progress(tid, current, total)

        def log(self, msg: str) -> None:
            log(tid, msg)
            if _MIRROR_TO_STDOUT:
                print(msg, flush=True)

        def set_summary(self, summary: str) -> None:
            set_summary(tid, summary)
            if _MIRROR_TO_STDOUT:
                print(f"✓ {summary}", flush=True)

    try:
        yield Handle()
    finally:
        end(tid)


if __name__ == "__main__":
    # 手动 CLI：清理孤儿后列出当前活跃任务
    import sys
    tasks = cleanup_stale()
    if not tasks:
        print("（无活跃任务）")
    else:
        now = time.time()
        for t in tasks:
            elapsed = int(now - t["started_at"])
            print(f"  [{elapsed}s] {t['name']} (pid={t['pid']}) {t.get('detail','')}")
    sys.exit(0)
