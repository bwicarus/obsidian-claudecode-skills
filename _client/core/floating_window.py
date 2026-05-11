"""半透明置顶悬浮窗 — 复刻原 launchers/任务监视.py 的 FloatingWindow，
作为 customtkinter 主窗口的子 Toplevel（同进程内，不再独立 launcher.exe）。

数据来源：state/active_tasks.json，路径由 config 提供。
显示活跃任务进度条 + 最近完成任务，所有任务结束 N 秒后自动隐藏。

cfg.floating:
  position:      "auto" | "top-left" | "top-right" | "bottom-left" | "bottom-right" | "custom"
  custom_x:      int    # position == "custom" 时的 X
  custom_y:      int    # 同上
  click_through: bool   # 鼠标点击穿透（不阻挡下层窗口）
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from typing import Callable

from task_state import (  # type: ignore
    load_active_tasks, load_completed_tasks, fmt_elapsed,
)


BG          = "#1a1a1a"
FG          = "#e8e8e8"
DIM         = "#888"
WIDTH       = 320
ALPHA       = 0.85
POLL_MS     = 500
LOG_VISIBLE = 3
BAR_LENGTH  = 16
BAR_FILLED  = "▰"
BAR_EMPTY   = "▱"
AUTO_HIDE_AFTER_DONE_S = 3


class FloatingWindow:
    def __init__(
        self,
        master: tk.Misc,
        state_file_getter: Callable[[], str | None],
        get_cfg: Callable[[], dict] | None = None,
        on_save_position: Callable[[int, int], None] | None = None,
    ):
        self._get_state_file = state_file_getter
        self._get_cfg = get_cfg or (lambda: {})
        self._on_save_position = on_save_position or (lambda _x, _y: None)

        self.win = tk.Toplevel(master)
        self.win.title("任务监视")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", ALPHA)
        self.win.configure(bg=BG)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        # 应用 cfg 决定的位置
        self.apply_cfg_position()

        self.frame = tk.Frame(self.win, bg=BG, padx=12, pady=10)
        self.frame.pack(fill="both", expand=True)
        self.label = tk.Label(
            self.frame, text="（无活跃任务）", bg=BG, fg=DIM,
            font=("Consolas", 10), justify="left", anchor="w",
        )
        self.label.pack(fill="both", expand=True)

        # 拖动支持
        for w in (self.win, self.frame, self.label):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.visible_user_pref = False
        self._drag_x = 0
        self._drag_y = 0
        self._dragging = False
        self._last_active_ts = 0.0
        self._poll_alive = True

        self.win.withdraw()
        self.win.after(POLL_MS, self._poll)

        # 应用鼠标穿透（如 cfg 配置）
        cfg = (self._get_cfg() or {}).get("floating") or {}
        if cfg.get("click_through"):
            # 必须等窗口实例化后再设置 EX style
            self.win.after(50, lambda: self.set_click_through(True))

    # ── 位置 / 穿透 ───────────────────────────────────────
    def apply_cfg_position(self) -> None:
        cfg = (self._get_cfg() or {}).get("floating") or {}
        pos = (cfg.get("position") or "auto").lower()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        if pos == "top-left":
            x, y = 20, 20
        elif pos == "top-right":
            x, y = sw - WIDTH - 20, 20
        elif pos == "bottom-left":
            x, y = 20, sh - 200
        elif pos == "bottom-right":
            x, y = sw - WIDTH - 20, sh - 200
        elif pos == "custom":
            try:
                x = int(cfg.get("custom_x", sw - WIDTH - 20))
                y = int(cfg.get("custom_y", sh - 200))
            except (TypeError, ValueError):
                x, y = sw - WIDTH - 20, sh - 200
        else:   # auto / 默认右下
            x, y = sw - WIDTH - 20, sh - 200
        self.win.geometry(f"{WIDTH}x80+{x}+{y}")

    def set_click_through(self, enabled: bool) -> None:
        """Windows 下设置鼠标穿透：WS_EX_LAYERED + WS_EX_TRANSPARENT。
        穿透状态下窗口不接收任何鼠标事件 → 拖动也失效。需要先关穿透才能拖。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            self.win.update_idletasks()
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enabled:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    # ── 行为 ─────────────────────────────────
    def show(self):
        self.visible_user_pref = True
        self.win.deiconify()
        try:
            self.win.lift()
        except Exception:
            pass

    def hide(self):
        self.visible_user_pref = False
        self.win.withdraw()

    def toggle(self):
        if self.is_shown():
            self.hide()
        else:
            self.show()

    def is_shown(self) -> bool:
        try:
            return self.win.state() == "normal"
        except tk.TclError:
            return False

    def destroy(self) -> None:
        self._poll_alive = False
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── 拖动 ─────────────────────────────────
    def _drag_start(self, ev):
        self._drag_x = ev.x_root - self.win.winfo_x()
        self._drag_y = ev.y_root - self.win.winfo_y()
        self._dragging = True

    def _drag_motion(self, ev):
        x = ev.x_root - self._drag_x
        y = ev.y_root - self._drag_y
        self.win.geometry(f"+{x}+{y}")

    def _drag_end(self, _ev):
        if not self._dragging:
            return
        self._dragging = False
        try:
            x, y = self.win.winfo_x(), self.win.winfo_y()
            self._on_save_position(x, y)
        except Exception:
            pass

    # ── 渲染 ─────────────────────────────────
    def _format_task(self, t: dict, now: float) -> list[str]:
        lines: list[str] = []
        elapsed = int(now - t.get("started_at", now))
        lines.append(f"● {t['name']}  {fmt_elapsed(elapsed)}")
        progress = t.get("progress")
        if progress and progress.get("total", 0) > 0:
            cur = max(0, min(progress["current"], progress["total"]))
            total = progress["total"]
            ratio = cur / total
            filled = int(round(ratio * BAR_LENGTH))
            bar = BAR_FILLED * filled + BAR_EMPTY * (BAR_LENGTH - filled)
            pct = int(ratio * 100)
            lines.append(f"  {bar}  {cur}/{total}  {pct}%")
        detail = t.get("detail", "")
        if detail:
            lines.append(f"  {detail}")
        log_entries = t.get("log", []) or []
        if log_entries:
            visible = log_entries[-LOG_VISIBLE:]
            lines.append("  " + "─" * (BAR_LENGTH + 4))
            for entry in visible:
                lines.append(f"  {entry}")
        return lines

    def _format_completed(self, t: dict, now: float) -> list[str]:
        lines: list[str] = []
        duration = int(t.get("duration_s", 0))
        ago = int(now - t.get("completed_at", now))
        lines.append(f"✓ {t['name']}  共 {fmt_elapsed(duration)}  ({ago}s 前完成)")
        progress = t.get("progress")
        if progress and progress.get("total", 0) > 0:
            lines.append(f"  完成 {progress['current']}/{progress['total']}")
        summary = t.get("summary", "") or t.get("detail", "")
        if summary:
            lines.append(f"  {summary}")
        log_entries = t.get("log", []) or []
        if log_entries:
            visible = log_entries[-LOG_VISIBLE:]
            lines.append("  " + "─" * (BAR_LENGTH + 4))
            for entry in visible:
                lines.append(f"  {entry}")
        return lines

    def _poll(self) -> None:
        if not self._poll_alive:
            return
        try:
            self._refresh()
        except Exception:
            pass
        try:
            self.win.after(POLL_MS, self._poll)
        except tk.TclError:
            self._poll_alive = False

    def _refresh(self) -> None:
        path = self._get_state_file()
        if not path:
            self.label.config(text="（未配置 state/active_tasks.json 路径）", fg=DIM)
            return

        tasks     = load_active_tasks(path)
        completed = load_completed_tasks(path)
        now       = time.time()

        if tasks or completed:
            if tasks:
                self._last_active_ts = now
            blocks: list[list[str]] = []
            for t in tasks:
                blocks.append(self._format_task(t, now))
            for t in completed:
                blocks.append(self._format_completed(t, now))
            text = "\n".join("\n".join(b) for b in blocks)
            total_lines = sum(len(b) for b in blocks) + max(0, len(blocks) - 1)
            self.label.config(text=text, fg=FG)
            height = max(60, 18 * total_lines + 20)
            self.win.geometry(f"{WIDTH}x{height}")
            if not self.is_shown():
                self.win.deiconify()
        else:
            if (now - self._last_active_ts) > AUTO_HIDE_AFTER_DONE_S and not self.visible_user_pref:
                self.win.withdraw()
            else:
                self.label.config(text="（无活跃任务）", fg=DIM)
