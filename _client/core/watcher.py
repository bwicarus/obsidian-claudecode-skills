"""vault 文件变化监听 + 防抖 + 冷却 + 互斥触发。

设计：
  防抖（debounce_sec）：最后一次变化后这段时间内"安静"才会触发。
                       默认 90 秒，给"打字思考停顿"足够余量。
  冷却（cooldown_sec）：上次触发后这段时间内即使再有变化也跳过。
                       默认 600 秒（10 分钟），避免 register 还在跑就又叠一轮。
  互斥（busy 标志）  ：GUI 端跑 register 时调 set_busy(True) → 期间所有触发跳过。
                       跑完 set_busy(False)。

触发时机：
  - 文件变化 → handler 累积 + 重置 timer
  - timer 到点 → _fire(paths)
      busy?       → 跳过，调 on_skip("上轮还在跑")
      cooldown 中? → 跳过，调 on_skip("冷却剩余 Ns")
      其余        → 调 on_burst(paths)，更新 last_fired_at
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


SKIP_DIR_NAMES = {".git", ".obsidian", "__pycache__", "node_modules", ".trash"}


class _Handler(FileSystemEventHandler):
    """累积事件 + 防抖。把 fire 的时机交给 VaultWatcher.fire()。"""

    def __init__(self, fire_callback: Callable[[list[str]], None], debounce_sec: float):
        self.fire_callback = fire_callback
        self.debounce_sec = debounce_sec
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    def _is_relevant(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in (".md", ".markdown"):
            return False
        for part in p.parts:
            if part in SKIP_DIR_NAMES:
                return False
        return True

    def on_any_event(self, event):
        if event.is_directory:
            return
        if not self._is_relevant(event.src_path):
            return
        if event.event_type not in ("created", "modified", "moved"):
            return
        with self._lock:
            self._pending.add(event.src_path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_sec, self._maybe_fire)
            self._timer.daemon = True
            self._timer.start()

    def _maybe_fire(self) -> None:
        with self._lock:
            paths = sorted(self._pending)
            self._pending.clear()
            self._timer = None
        if not paths:
            return
        try:
            self.fire_callback(paths)
        except Exception:
            import traceback
            traceback.print_exc()


class VaultWatcher:
    """启停式包装。GUI 端在跑 register 期间要 set_busy(True/False)。"""

    def __init__(
        self,
        vault_path: str,
        on_burst: Callable[[list[str]], None],
        on_skip: Callable[[str], None] | None = None,
        debounce_sec: float = 90.0,
        cooldown_sec: float = 600.0,
    ):
        self.vault_path = Path(vault_path)
        self._on_burst = on_burst
        self._on_skip = on_skip or (lambda _msg: None)
        self.debounce_sec = float(debounce_sec)
        self.cooldown_sec = float(cooldown_sec)

        self._last_fired_at: float = 0.0
        self._busy: bool = False
        self._state_lock = threading.Lock()
        self._observer: Observer | None = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def set_busy(self, busy: bool) -> None:
        with self._state_lock:
            self._busy = bool(busy)

    def status(self) -> dict:
        with self._state_lock:
            now = time.time()
            cooldown_remaining = 0.0
            if self._last_fired_at:
                cooldown_remaining = max(0.0, self.cooldown_sec - (now - self._last_fired_at))
            return {
                "running": self.is_running,
                "busy": self._busy,
                "last_fired_at": self._last_fired_at,
                "cooldown_remaining": cooldown_remaining,
                "debounce_sec": self.debounce_sec,
                "cooldown_sec": self.cooldown_sec,
            }

    # ── handler 调到这里 ──
    def _fire(self, paths: list[str]) -> None:
        with self._state_lock:
            now = time.time()
            if self._busy:
                self._on_skip(f"上一轮任务还在跑，跳过 {len(paths)} 个变化")
                return
            if self._last_fired_at and now - self._last_fired_at < self.cooldown_sec:
                rem = int(self.cooldown_sec - (now - self._last_fired_at))
                self._on_skip(f"冷却中（剩余 {rem}s），跳过 {len(paths)} 个变化")
                return
            self._last_fired_at = now
        try:
            self._on_burst(paths)
        except Exception as e:
            self._on_skip(f"触发回调失败：{e}")

    # ── 外部 API ──
    def start(self) -> tuple[bool, str]:
        if self.is_running:
            return True, f"已在监听 {self.vault_path}"
        if not self.vault_path.exists() or not self.vault_path.is_dir():
            return False, f"vault 路径无效：{self.vault_path}"
        try:
            handler = _Handler(self._fire, self.debounce_sec)
            obs = Observer()
            obs.schedule(handler, str(self.vault_path), recursive=True)
            obs.daemon = True
            obs.start()
            self._observer = obs
            return True, (
                f"开始监听 {self.vault_path}（防抖 {int(self.debounce_sec)}s · "
                f"冷却 {int(self.cooldown_sec)}s）"
            )
        except Exception as e:
            return False, f"启动 watcher 失败：{e}"

    def stop(self) -> tuple[bool, str]:
        if not self._observer:
            return True, "watcher 未启动"
        try:
            self._observer.stop()
            self._observer.join(timeout=3)
        except Exception:
            pass
        self._observer = None
        return True, "已停止 watcher"
