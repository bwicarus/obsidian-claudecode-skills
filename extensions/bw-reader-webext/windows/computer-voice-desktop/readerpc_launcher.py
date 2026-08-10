from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageDraw
import pystray

from bridge_core import (
    BridgeError,
    BridgePaths,
    WindowsProcessRunner,
    disable_and_stop_direct_service,
    load_direct_config,
    read_direct_status,
    set_direct_config_enabled,
    start_direct_service,
)
from readerpc_services import (
    PRODUCT_NAME,
    PcOcrServiceController,
    ReaderPCPaths,
    ReaderPCServiceError,
    format_pc_progress,
    read_reader_context_status,
    write_readerpc_status,
)


APP_VERSION = "0.1.3"
PREFERENCES_CONTRACT = "readerpc-server-config/1"
POLL_INTERVAL_MS = 2_500
STATUS_PUBLISH_INTERVAL_SECONDS = 10.0
PC_RESTART_BACKOFF_SECONDS = 30.0


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_preferences(path: Path) -> dict[str, bool]:
    defaults = {"keepPcPreprocessingOnline": True}
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return defaults
    if (
        not isinstance(value, dict)
        or value.get("contract") != PREFERENCES_CONTRACT
        or not isinstance(value.get("keepPcPreprocessingOnline"), bool)
    ):
        return defaults
    return {
        "keepPcPreprocessingOnline": value["keepPcPreprocessingOnline"],
    }


def save_preferences(path: Path, *, keep_pc_online: bool) -> None:
    _atomic_json(
        path,
        {
            "contract": PREFERENCES_CONTRACT,
            "keepPcPreprocessingOnline": bool(keep_pc_online),
        },
    )


def _tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=14, fill="#0A84FF")
    draw.rectangle((17, 16, 25, 48), fill="white")
    draw.rectangle((29, 16, 37, 48), fill="white")
    draw.rectangle((41, 16, 49, 48), fill="white")
    draw.ellipse((19, 20, 23, 24), fill="#0A84FF")
    draw.ellipse((31, 32, 35, 36), fill="#0A84FF")
    draw.ellipse((43, 40, 47, 44), fill="#0A84FF")
    return image


class ReaderPCWindow:
    def __init__(
        self,
        root: tk.Tk,
        *,
        bridge_paths: BridgePaths | None = None,
        process_runner: WindowsProcessRunner | None = None,
        pc_ocr: PcOcrServiceController | None = None,
        readerpc_paths: ReaderPCPaths | None = None,
    ) -> None:
        self.root = root
        self.bridge_paths = bridge_paths or BridgePaths.discover()
        self.process_runner = process_runner or WindowsProcessRunner()
        self.pc_ocr = pc_ocr or PcOcrServiceController()
        self.readerpc_paths = readerpc_paths or ReaderPCPaths.discover()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.closed = False
        self.last_pc_start_attempt = 0.0
        self.last_status_publish = 0.0
        preferences = load_preferences(self.readerpc_paths.preferences_file)
        self.keep_pc_online = tk.BooleanVar(
            value=preferences["keepPcPreprocessingOnline"]
        )

        root.title(PRODUCT_NAME)
        root.geometry("620x500")
        root.minsize(560, 450)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("ReaderPC.Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("ReaderPC.Heading.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=PRODUCT_NAME, style="ReaderPC.Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "统一显示电脑语音、Reader 上下文与 PC 预处理。"
                "关闭窗口后仍驻留托盘；空闲不会加载 OCR 模型。"
            ),
            foreground="#596579",
            wraplength=570,
        ).pack(anchor="w", pady=(4, 14))

        self.voice_status, self.voice_detail, self.voice_button = self._service_row(
            outer,
            "电脑语音",
            "Windows 直连语音服务",
            self.toggle_voice,
        )
        self.context_status, self.context_detail, _ = self._service_row(
            outer,
            "Reader 上下文",
            "网页与 App 的实时快照通道",
            None,
        )
        self.pc_status, self.pc_detail, self.pc_button = self._service_row(
            outer,
            "PC 预处理",
            "文字、分词与公式 · quality-first-v1",
            self.toggle_pc,
        )

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(8, 2))
        ttk.Checkbutton(
            options,
            text="ReaderPC 运行期间保持 PC 预处理在线",
            variable=self.keep_pc_online,
            command=self.on_keep_pc_changed,
        ).pack(side="left")
        ttk.Button(
            options,
            text="打开语音详细设置",
            command=self.open_legacy_voice_settings,
        ).pack(side="right")

        ttk.Separator(outer).pack(fill="x", pady=(12, 10))
        self.footer = ttk.Label(
            outer,
            text="正在读取本机状态…",
            foreground="#596579",
            wraplength=570,
        )
        self.footer.pack(anchor="w")

        self.tray = pystray.Icon(
            "ReaderPCServer",
            _tray_image(),
            PRODUCT_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("显示主窗口", self._tray_show, default=True),
                pystray.MenuItem("启动 PC 预处理", self._tray_start_pc),
                pystray.MenuItem("停止 PC 预处理", self._tray_stop_pc),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出界面（服务继续）", self._tray_exit),
            ),
        )
        threading.Thread(target=self._run_tray, name="readerpc-tray", daemon=True).start()
        root.after(100, self._drain_events)
        root.after(250, self.refresh)
        root.after(600, self._ensure_pc_online)

    def _service_row(
        self,
        parent: ttk.Frame,
        title: str,
        subtitle: str,
        command: Callable[[], None] | None,
    ) -> tuple[ttk.Label, ttk.Label, ttk.Button | None]:
        frame = ttk.LabelFrame(parent, padding=(12, 10))
        frame.pack(fill="x", pady=(0, 9))
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=title, style="ReaderPC.Heading.TLabel").pack(side="left")
        status = ttk.Label(top, text="正在读取", foreground="#596579")
        status.pack(side="left", padx=(12, 0))
        button = None
        if command is not None:
            button = ttk.Button(top, text="…", command=command, width=10)
            button.pack(side="right")
        detail = ttk.Label(frame, text=subtitle, foreground="#6b7280", wraplength=520)
        detail.pack(anchor="w", pady=(5, 0))
        return status, detail, button

    def _run_tray(self) -> None:
        try:
            self.tray.run()
        except Exception as exc:
            self.events.put(("tray-error", str(exc)))

    def _tray_show(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.show_window)

    def _tray_start_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(True))

    def _tray_stop_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(False))

    def _tray_exit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.exit_ui)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_window(self) -> None:
        self.root.withdraw()

    def exit_ui(self) -> None:
        self.closed = True
        try:
            self.tray.stop()
        finally:
            self.root.destroy()

    def _run_task(
        self,
        label: str,
        action: Callable[[], Any],
        success: str,
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.footer.configure(text=label)
        self._set_buttons_enabled(False)

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self.events.put(("task-error", exc))
            else:
                self.events.put(("task-success", (success, result)))

        threading.Thread(target=worker, name="readerpc-action", daemon=True).start()

    def _drain_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "task-error":
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.footer.configure(text=f"操作失败：{value}", foreground="#c62828")
                    self.refresh()
                elif kind == "task-success":
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.footer.configure(text=value[0], foreground="#167347")
                    self.refresh()
                elif kind == "tray-error":
                    self.footer.configure(
                        text=f"托盘不可用，主窗口仍可使用：{value}",
                        foreground="#c62828",
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (self.voice_button, self.pc_button):
            if button is not None:
                button.configure(state=state)

    def _voice_status(self):
        return read_direct_status(self.bridge_paths, self.process_runner)

    def toggle_voice(self) -> None:
        current = self._voice_status()
        if current.service_online:
            self._run_task(
                "正在停用电脑语音服务…",
                lambda: disable_and_stop_direct_service(
                    self.bridge_paths,
                    self.process_runner,
                ),
                "电脑语音已停用。",
            )
            return

        def start() -> int:
            changed = set_direct_config_enabled(self.bridge_paths, True)
            try:
                return start_direct_service(self.bridge_paths, self.process_runner)
            except Exception:
                if changed:
                    set_direct_config_enabled(self.bridge_paths, False)
                raise

        self._run_task("正在启用电脑语音服务…", start, "电脑语音已启用。")

    def toggle_pc(self) -> None:
        self._set_pc_running(not self.pc_ocr.status().running)

    def _set_pc_running(self, running: bool) -> None:
        self.keep_pc_online.set(running)
        save_preferences(
            self.readerpc_paths.preferences_file,
            keep_pc_online=running,
        )
        if running:
            self.last_pc_start_attempt = time.monotonic()
            self._run_task(
                "正在启动 PC 预处理…",
                self.pc_ocr.start,
                "PC 预处理已在线，App 最迟约 20 秒后显示可用。",
            )
        else:
            self._run_task(
                "正在停止 PC 预处理…",
                self.pc_ocr.stop,
                "PC 预处理已停止。",
            )

    def on_keep_pc_changed(self) -> None:
        self._set_pc_running(bool(self.keep_pc_online.get()))

    def _ensure_pc_online(self) -> None:
        if self.closed:
            return
        if (
            self.keep_pc_online.get()
            and not self.busy
            and not self.pc_ocr.status().running
            and time.monotonic() - self.last_pc_start_attempt >= PC_RESTART_BACKOFF_SECONDS
        ):
            self.last_pc_start_attempt = time.monotonic()
            self._run_task(
                "正在让 PC 预处理保持在线…",
                self.pc_ocr.start,
                "PC 预处理已恢复在线。",
            )
        self.root.after(5_000, self._ensure_pc_online)

    def open_legacy_voice_settings(self) -> None:
        executable = self.bridge_paths.desktop_launcher
        if not executable.is_file():
            messagebox.showerror(PRODUCT_NAME, "未找到已安装的电脑语音详细设置。")
            return
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )

    def refresh(self) -> None:
        if self.closed:
            return
        try:
            voice = self._voice_status()
            pc = self.pc_ocr.status()
            context = read_reader_context_status(
                self.bridge_paths.root / "runtime" / "reader-context-snapshot.json"
            )
            if voice.service_online:
                voice_label = "在线 · 通话中" if voice.capture_active else "在线 · 空闲"
                voice_color = "#167347"
                voice_action = "停用"
            elif voice.configuration_enabled:
                voice_label = "正在恢复" if voice.reason else "已启用 · 离线"
                voice_color = "#b26a00"
                voice_action = "重启"
            else:
                voice_label = "已停用"
                voice_color = "#6b7280"
                voice_action = "启用"
            self.voice_status.configure(text=voice_label, foreground=voice_color)
            self.voice_detail.configure(
                text=(
                    f"Reader 已连接 · PID {voice.pid}"
                    if voice.reader_connected
                    else f"等待 Reader 连接 · {voice.reason}"
                )
            )
            self.voice_button.configure(text=voice_action)

            self.context_status.configure(
                text=context.state_label,
                foreground="#167347" if context.fresh else "#6b7280",
            )
            self.context_detail.configure(
                text=(
                    f"{context.kind or '内容'} · {context.title}"
                    if context.title
                    else "打开 Reader App 或启用扩展后会在这里显示。"
                )
            )

            self.pc_status.configure(
                text=pc.state_label,
                foreground="#167347" if pc.running else "#6b7280",
            )
            details = format_pc_progress(pc)
            if pc.error:
                details = pc.error
            elif not details:
                details = (
                    "环境已就绪；空闲时不加载模型和显存。"
                    if pc.source_ready
                    else "未找到签发运行文件或 PC OCR Python 环境。"
                )
            self.pc_detail.configure(text=details)
            self.pc_button.configure(text="停止" if pc.running else "启动")

            now = time.monotonic()
            if now - self.last_status_publish >= STATUS_PUBLISH_INTERVAL_SECONDS:
                write_readerpc_status(
                    self.readerpc_paths.status_file,
                    voice={
                        "online": voice.service_online,
                        "configured": voice.configuration_enabled,
                        "readerConnected": voice.reader_connected,
                        "captureActive": voice.capture_active,
                        "reason": voice.reason,
                    },
                    context=context,
                    pc_ocr=pc,
                )
                self.last_status_publish = now
        except (BridgeError, ReaderPCServiceError, OSError, ValueError) as exc:
            self.footer.configure(text=f"状态读取失败：{exc}", foreground="#c62828")
        self.root.after(POLL_INTERVAL_MS, self.refresh)


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\BWReader.ReaderPC.Server")
        if not handle:
            return False
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True


def self_test_report() -> dict[str, Any]:
    paths = ReaderPCPaths.discover()
    pc_paths = PcOcrServiceController().paths
    checks = {
        "preferences-path-absolute": paths.preferences_file.is_absolute(),
        "status-path-absolute": paths.status_file.is_absolute(),
        "pc-runtime-discovered": pc_paths.project_root is not None,
        "pc-python-present": pc_paths.python_exe.is_file(),
        "voice-install-present": BridgePaths.discover().native_host.is_file(),
    }
    return {
        "contract": "readerpc-server-self-test/1",
        "ok": all(checks.values()),
        "version": APP_VERSION,
        "checks": checks,
        "servicesStarted": False,
        "audioOpened": False,
        "modelsLoaded": False,
        "gpuAllocated": False,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--self-test"]:
        report = self_test_report()
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if arguments:
        return 2
    instance = SingleInstance()
    if not instance.acquire():
        return 0
    root = tk.Tk()
    ReaderPCWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
