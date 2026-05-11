"""
任务监视.py — 半透明置顶悬浮窗 + 系统托盘图标

读取 state/active_tasks.json 实时显示活跃任务及已用时长。
任务由各脚本通过 task_tracker.track(...) 上下文管理器报告。

托盘菜单：
    显示窗口 / 隐藏窗口 — 切换悬浮窗可见
    退出
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

PROJECT     = Path(r"C:\claude")
STATE_FILE  = PROJECT / "state" / "active_tasks.json"
SCRIPTS_DIR = PROJECT / "scripts"
PYTHON      = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
PYTHONW     = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\pythonw.exe"
POLL_MS     = 500
AUTO_HIDE_AFTER_DONE_S = 3      # 所有任务结束 N 秒后自动隐藏

AI_BACKENDS = ("claude", "gpt")

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
_AI_SETTINGS  = Path(_LOCALAPPDATA) / "截图问答" / "settings.json"


# ── 状态读取 ─────────────────────────────────────────────────────────────────

def _is_pid_alive(pid: int) -> bool:
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
        return True  # 出错时保守认为还活着


def load_active_tasks() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        tasks = data.get("tasks", [])
    except (json.JSONDecodeError, OSError):
        return []
    # 过滤掉死进程留下的孤儿条目
    return [t for t in tasks if _is_pid_alive(t.get("pid", 0))]


COMPLETION_DISPLAY_S = 60  # 与 task_tracker 保持一致


def load_completed_tasks() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        rows = data.get("completed", []) or []
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = time.time() - COMPLETION_DISPLAY_S
    return [r for r in rows if r.get("completed_at", 0) >= cutoff]


def fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    return f"{seconds // 3600}时{(seconds % 3600) // 60:02d}分"


# ── 悬浮窗 ───────────────────────────────────────────────────────────────────

class FloatingWindow:
    BG          = "#1a1a1a"
    FG          = "#e8e8e8"
    DOT_ACTIVE  = "#60a5fa"
    DOT_IDLE    = "#666"
    ALPHA       = 0.85

    WIDTH        = 320
    BAR_LENGTH   = 16
    BAR_FILLED   = "▰"
    BAR_EMPTY    = "▱"
    LOG_VISIBLE  = 3
    DIM          = "#888"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("任务监视")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.ALPHA)
        self.root.configure(bg=self.BG)

        # 初始位置：屏幕右下角
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.WIDTH}x80+{sw - self.WIDTH - 20}+{sh - 200}")

        self.frame = tk.Frame(self.root, bg=self.BG, padx=12, pady=10)
        self.frame.pack(fill="both", expand=True)

        self.label = tk.Label(
            self.frame, text="（无活跃任务）", bg=self.BG, fg=self.FG,
            font=("Consolas", 10), justify="left", anchor="w",
        )
        self.label.pack(fill="both", expand=True)

        # 拖动支持
        for w in (self.root, self.frame, self.label):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)

        # 默认隐藏
        self.visible_user_pref = False
        self.root.withdraw()

        self._drag_x = 0
        self._drag_y = 0
        self._last_active_ts = 0.0

    def _drag_start(self, ev):
        self._drag_x = ev.x_root - self.root.winfo_x()
        self._drag_y = ev.y_root - self.root.winfo_y()

    def _drag_motion(self, ev):
        x = ev.x_root - self._drag_x
        y = ev.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def show(self):
        self.visible_user_pref = True
        self.root.deiconify()

    def hide(self):
        self.visible_user_pref = False
        self.root.withdraw()

    def is_shown(self) -> bool:
        return self.root.state() == "normal"

    def _format_task(self, t: dict, now: float) -> list[str]:
        lines: list[str] = []
        elapsed = int(now - t.get("started_at", now))
        lines.append(f"● {t['name']}  {fmt_elapsed(elapsed)}")

        progress = t.get("progress")
        if progress and progress.get("total", 0) > 0:
            cur = max(0, min(progress["current"], progress["total"]))
            total = progress["total"]
            ratio = cur / total
            filled = int(round(ratio * self.BAR_LENGTH))
            bar = self.BAR_FILLED * filled + self.BAR_EMPTY * (self.BAR_LENGTH - filled)
            pct = int(ratio * 100)
            lines.append(f"  {bar}  {cur}/{total}  {pct}%")

        detail = t.get("detail", "")
        if detail:
            lines.append(f"  {detail}")

        log_entries = t.get("log", []) or []
        if log_entries:
            visible = log_entries[-self.LOG_VISIBLE:]
            lines.append("  " + "─" * (self.BAR_LENGTH + 4))
            for entry in visible:
                lines.append(f"  {entry}")
        return lines

    def _format_completed(self, t: dict, now: float) -> list[str]:
        """完成的任务：✓ 标记 + 时长 + 汇总 + 最近日志条目。"""
        lines: list[str] = []
        duration = int(t.get("duration_s", 0))
        ago = int(now - t.get("completed_at", now))
        lines.append(f"✓ {t['name']}  共 {fmt_elapsed(duration)}  ({ago}s 前完成)")

        progress = t.get("progress")
        if progress and progress.get("total", 0) > 0:
            cur, total = progress["current"], progress["total"]
            lines.append(f"  完成 {cur}/{total}")

        summary = t.get("summary", "") or t.get("detail", "")
        if summary:
            lines.append(f"  {summary}")

        log_entries = t.get("log", []) or []
        if log_entries:
            visible = log_entries[-self.LOG_VISIBLE:]
            lines.append("  " + "─" * (self.BAR_LENGTH + 4))
            for entry in visible:
                lines.append(f"  {entry}")
        return lines

    def refresh(self, reschedule: bool = True):
        tasks     = load_active_tasks()
        completed = load_completed_tasks()
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
            self.label.config(text=text, fg=self.FG)
            height = max(60, 18 * total_lines + 20)
            self.root.geometry(f"{self.WIDTH}x{height}")
            if not self.is_shown():
                self.root.deiconify()
        else:
            # 所有任务结束 N 秒后，如果用户没主动开过窗，则隐藏
            if (now - self._last_active_ts) > AUTO_HIDE_AFTER_DONE_S and not self.visible_user_pref:
                self.root.withdraw()
            else:
                self.label.config(text="（无活跃任务）", fg="#888")

        if reschedule:
            self.root.after(POLL_MS, self.refresh)


# ── 托盘图标 ─────────────────────────────────────────────────────────────────

def make_icon_image(active: bool = False) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (96, 165, 250, 255) if active else (140, 140, 140, 255)
    d.ellipse((10, 10, 54, 54), fill=color)
    return img


def run_service_command(*args: str) -> str:
    script = SCRIPTS_DIR / "service_switch.py"
    cmd = [PYTHON, str(script), *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            cmd,
            cwd=str(PROJECT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=20,
        )
        text = (r.stdout + "\n" + r.stderr).strip()
        return text or f"退出码 {r.returncode}"
    except Exception as exc:
        return f"服务命令失败：{exc}"


def parse_backend(status_text: str) -> str:
    for line in status_text.splitlines():
        if line.startswith("backend="):
            backend = line.split("=", 1)[1].strip().lower()
            if backend in AI_BACKENDS:
                return backend
    return "unknown"


def load_saved_backend() -> str:
    try:
        data = json.loads(_AI_SETTINGS.read_text(encoding="utf-8"))
        if "codex" in data.get("backend", ""):
            return "gpt"
        return "claude"
    except Exception:
        return "claude"


class TrayApp:
    def __init__(self):
        self.window = FloatingWindow()
        self._stop = threading.Event()
        self._manual_message_until = 0.0
        # backend 由 settings.json 决定；服务未运行时 autostart_worker 会拉起
        self._backend = load_saved_backend()
        status = run_service_command("status")
        self._needs_autostart = "not listening" in status

        self.icon = pystray.Icon(
            "task_monitor",
            icon=make_icon_image(False),
            title="任务监视",
            menu=pystray.Menu(
                pystray.MenuItem("显示/隐藏窗口", self._toggle, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("AI: Claude", self._switch_claude, checked=self._is_claude, radio=True),
                pystray.MenuItem("AI: GPT",    self._switch_gpt,    checked=self._is_gpt,    radio=True),
                pystray.MenuItem("服务状态", self._show_service_status),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("登记新卡片/笔记", self._register_new_note),
                pystray.MenuItem("上传网页", self._upload_website),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

    def _is_claude(self, *_):
        return self._backend == "claude"

    def _is_gpt(self, *_):
        return self._backend == "gpt"

    def _toggle(self, *_):
        def flip():
            if self.window.is_shown():
                self.window.hide()
            else:
                self.window.show()
        self.window.root.after(0, flip)

    def _set_backend_from_status(self, status_text: str):
        backend = parse_backend(status_text)
        if backend in AI_BACKENDS:
            self._backend = backend
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _set_window_message(self, text: str):
        def update():
            self._manual_message_until = time.time() + 20
            self.window.visible_user_pref = True
            self.window.label.config(text=text, fg=self.window.FG)
            self.window.root.geometry("420x180")
            self.window.root.deiconify()

        self.window.root.after(0, update)

    def _run_service_async(self, label: str, *args: str):
        def worker():
            self._set_window_message(f"{label}...")
            result = run_service_command(*args)
            self._set_backend_from_status(result)
            self._set_window_message(f"{label}\n\n{result}")

        threading.Thread(target=worker, daemon=True).start()

    def _switch_claude(self, *_):
        self._run_service_async("切换 AI: Claude", "switch", "claude")

    def _switch_gpt(self, *_):
        self._run_service_async("切换 AI: GPT", "switch", "gpt")

    def _show_service_status(self, *_):
        self._run_service_async("服务状态", "status")

    def _run_launcher_async(self, label: str, py_name: str):
        def worker():
            script = PROJECT / "launchers" / py_name
            self._set_window_message(f"{label}...\n\nAI 后端：{self._backend}")
            if not script.exists():
                self._set_window_message(f"{label}\n\n未找到：{script}")
                return
            try:
                # 用 pythonw（无控制台）后台执行；进度通过 task_tracker 显示在悬浮窗
                subprocess.Popen(
                    [PYTHONW, str(script)],
                    cwd=str(PROJECT),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self._set_window_message(f"{label}\n\n已启动")
            except Exception as exc:
                self._set_window_message(f"{label}\n\n启动失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _register_new_note(self, *_):
        self._run_launcher_async("登记新卡片/笔记", "登记新笔记.py")

    def _upload_website(self, *_):
        self._run_launcher_async("上传网页", "上传网页.py")

    def _quit(self, *_):
        def shutdown():
            try:
                run_service_command("stop-all")
            finally:
                self._stop.set()
                self.icon.stop()
                try:
                    self.window.root.after(0, self.window.root.destroy)
                except Exception:
                    pass

        self._set_window_message("正在退出...\n\n停止后台服务、截图问答和 relay 隧道。")
        threading.Thread(target=shutdown, daemon=True).start()

    def _icon_updater(self):
        """背景线程：根据是否有活跃任务切换托盘图标颜色。"""
        last_state = None
        last_backend_check = 0.0
        while not self._stop.is_set():
            try:
                has_active = bool(load_active_tasks())
                if has_active != last_state:
                    self.icon.icon = make_icon_image(has_active)
                    last_state = has_active
                if time.time() - last_backend_check > 5:
                    last_backend_check = time.time()
                    self._set_backend_from_status(run_service_command("status"))
            except Exception:
                pass
            time.sleep(POLL_MS / 1000)

    def _window_refresher(self):
        if time.time() < self._manual_message_until:
            self.window.root.after(POLL_MS, self._window_refresher)
            return
        self.window.refresh(reschedule=False)
        self.window.root.after(POLL_MS, self._window_refresher)

    def _autostart_worker(self):
        time.sleep(1.0)
        if not self._needs_autostart:
            return
        label = f"自动启动 {self._backend.upper()} 服务"
        self._set_window_message(f"{label}...")
        result = run_service_command("switch", self._backend)
        self._set_backend_from_status(result)
        self._set_window_message(f"{label}\n\n{result}")

    def run(self):
        # tkinter 必须在主线程；托盘和图标更新放后台线程
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self._icon_updater, daemon=True).start()
        threading.Thread(target=self._autostart_worker, daemon=True).start()
        self.window.root.after(POLL_MS, self._window_refresher)
        self.window.root.mainloop()


def main():
    lock_file = None
    if os.name == "nt":
        try:
            import msvcrt
            lock_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BwicarusTaskMonitor.lock"
            lock_file = open(lock_path, "a+b")
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
    TrayApp().run()


if __name__ == "__main__":
    main()
