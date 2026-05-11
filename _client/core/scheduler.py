"""每日定时调度器 — 后台 daemon 线程，到点触发回调。

使用模式：
  sched = DailyScheduler(get_cfg, on_trigger=lambda: gui._run_register())
  sched.start()   # 立即跑后台线程
  sched.stop()

cfg 字段（顶层）：
  scheduled_register: {
    "enabled":    bool,    # 总开关
    "time":       "04:00", # HH:MM 24h
    "wake_anki":  bool,    # 触发前是否启动 Anki（让 AnkiConnect 可用）
  }

防重入：
  - 单实例（每个 DailyScheduler 一个线程）
  - 同一天不重复触发（_last_run_date 跟踪）
  - 错过的时间点不补跑（启动时 _last_run_date 设为 today）
"""
from __future__ import annotations

import datetime
import threading
import time
from typing import Callable


class DailyScheduler:
    def __init__(
        self,
        get_cfg: Callable[[], dict],
        on_trigger: Callable[[], None],
        on_log: Callable[[str], None] | None = None,
    ):
        self.get_cfg = get_cfg
        self.on_trigger = on_trigger
        self.on_log = on_log or (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 启动时初始化为 today，避免启动瞬间补跑过去几小时的时间点
        self._last_run_date: datetime.date | None = datetime.date.today()
        self._next_status: str = "未启动"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> str:
        return self._next_status

    def start(self) -> tuple[bool, str]:
        if self.is_running:
            return True, "scheduler 已在运行"
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daily-sched")
        self._thread.start()
        return True, "scheduler 已启动"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running:
            return True, "scheduler 未运行"
        self._stop.set()
        self._thread.join(timeout=3)
        self._thread = None
        self._next_status = "未启动"
        return True, "scheduler 已停止"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self.on_log(f"✗ scheduler tick 异常：{e}")
            # 间隔 30s 检查一次（粒度足够，CPU 几乎无开销）
            for _ in range(30):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def _tick(self) -> None:
        cfg = self.get_cfg() or {}
        sched = cfg.get("scheduled_register") or {}
        if not sched.get("enabled"):
            self._next_status = "未启用"
            return
        target = (sched.get("time") or "04:00").strip()
        try:
            h, m = (int(x) for x in target.split(":"))
        except ValueError:
            self._next_status = f"时间格式错误：{target}"
            return

        now = datetime.datetime.now()
        today = now.date()
        target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target_dt < now:
            # 今天的目标时间已过 → 下次触发是明天
            next_dt = target_dt + datetime.timedelta(days=1)
        else:
            next_dt = target_dt

        # 状态信息：下次触发距离当前还有多久
        delta = next_dt - now
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        self._next_status = f"已启用 · 下次 {next_dt.strftime('%m-%d %H:%M')}（{hours}h{mins:02d}m 后）"

        # 到点触发（容差：在目标时间的 30 秒以内）
        if (now >= target_dt and (now - target_dt).total_seconds() < 60
                and self._last_run_date != today):
            self._last_run_date = today
            self.on_log(f"⏰ 触发每日定时登记笔记（{target}）")
            try:
                self.on_trigger()
            except Exception as e:
                self.on_log(f"✗ 触发失败：{e}")
