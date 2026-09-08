"""readerpc_gate — 后台计划任务的总闸：ReaderPC 没在跑就别占用户的机器。

用户 2026-09-08：「我希望 pc 服务器软件退出后真的能把所有相关功能都停下，不然不使用时
会像昨天一样导致电脑卡顿」。ReaderPC 自己的退出流程是「退出即全停」（托管服务、预处理
worker、语音链一并终止），但 **Windows 计划任务不归它管** —— 关掉 ReaderPC 之后，
每 15 分钟的 KJ 同步、每晚的词典刷新照样会被系统拉起来。昨天凌晨的卡顿就是这么来的：
人在用电脑，一个计划任务在后台跑了半小时的 AI 调用。

判据两条，任一成立就不跑：
  ① 用户主动退出过（readerpc-user-exit.json，且标记晚于本次开机）——
     这与看门狗用的是同一个标记，语义一致：他关掉了，就别替他开回来；
  ② 心跳过期（readerpc-server.status.json 超过 max_stale_seconds 没更新）——
     覆盖崩溃、被杀、从未启动这些没有退出标记的情形。

⚠ 判不出来时**默认放行**。守卫的作用是省资源，不是制造"任务神秘不执行"；
   状态文件读不到就当 ReaderPC 在跑，宁可多跑一次也不要静默停摆。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: 心跳超过这么久没更新就认为 ReaderPC 不在（它每 10 秒刷一次，5 分钟是很宽的余量）
DEFAULT_MAX_STALE_SECONDS = 300
STATUS_NAME = "readerpc-server.status.json"
EXIT_MARKER_NAME = "readerpc-user-exit.json"


def readerpc_root() -> Path:
    """ReaderPC 的本地数据根（%LOCALAPPDATA%\\BWReader）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "BWReader"


def _boot_time() -> float | None:
    """本次开机时刻（epoch 秒）。拿不到返回 None —— 那就不用开机时间去判退出标记的新旧。"""
    try:
        import ctypes
        return time.time() - (ctypes.windll.kernel32.GetTickCount64() / 1000.0)
    except Exception:
        return None


def status_age_seconds(root: Path | None = None) -> float | None:
    """心跳文件距今多少秒。文件不存在或读不出返回 None。"""
    path = (root or readerpc_root()) / STATUS_NAME
    try:
        updated = json.loads(path.read_text(encoding="utf-8")).get("updatedAtEpochMs")
        if isinstance(updated, (int, float)) and updated > 0:
            return max(0.0, time.time() - updated / 1000.0)
    except (OSError, ValueError, TypeError):
        pass
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def user_exited(root: Path | None = None) -> bool:
    """用户在本次开机内主动退出过 ReaderPC。

    与看门狗同一个标记、同一条判废规则：关机时系统也会写标记，若标记早于本次开机
    就当它过期 —— 否则一个陈旧标记会让后台任务永久停摆。
    """
    path = (root or readerpc_root()) / EXIT_MARKER_NAME
    try:
        written = path.stat().st_mtime
    except OSError:
        return False
    boot = _boot_time()
    if boot is not None and written < boot:
        return False
    return True


def readerpc_active(
    root: Path | None = None,
    max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
) -> tuple[bool, str]:
    """(要不要跑, 原因)。判不出来一律放行。"""
    root = root or readerpc_root()
    if user_exited(root):
        return False, "用户已主动退出 ReaderPC（本次开机内）"
    age = status_age_seconds(root)
    if age is None:
        return True, "没有心跳文件，按在跑处理（宁可多跑一次也不静默停摆）"
    if age > max_stale_seconds:
        return False, "ReaderPC 心跳已停 %.0f 秒" % age
    return True, "ReaderPC 在跑（心跳 %.0f 秒前）" % age


def exit_if_readerpc_idle(task_name: str, max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS) -> None:
    """给计划任务用的一行守卫：不该跑就**出声**并以 0 退出（0 = 正常跳过，不是失败）。"""
    active, reason = readerpc_active(max_stale_seconds=max_stale_seconds)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if active:
        return
    print("[%s] %s 跳过：%s" % (stamp, task_name, reason), flush=True)
    raise SystemExit(0)


def _main() -> int:
    """`python readerpc_gate.py --check` → 0 = 该跑，10 = 该跳过（给 .cmd 用 errorlevel 判）。

    跳过用 10 而不是 1：1 会与"脚本自己出错"混在一起，让计划任务的失败记录失去意义。
    """
    import sys
    stale = DEFAULT_MAX_STALE_SECONDS
    for index, arg in enumerate(sys.argv):
        if arg == "--max-stale" and index + 1 < len(sys.argv):
            try:
                stale = float(sys.argv[index + 1])
            except ValueError:
                pass
    active, reason = readerpc_active(max_stale_seconds=stale)
    print("[%s] readerpc-gate: %s → %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), reason, "run" if active else "skip"), flush=True)
    return 0 if active else 10


if __name__ == "__main__":
    raise SystemExit(_main())
