from __future__ import annotations

from dataclasses import dataclass
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
    CONTEXT_DELIVERY_SNAPSHOT,
    ShortcutBrokerError,
    WindowsShortcutBroker,
    WindowsProcessRunner,
    load_direct_config,
    read_direct_status,
    set_direct_config_enabled,
    start_direct_service,
    stop_direct_service,
)
from control_plane import (
    ControlPaths,
    SubprocessExactCommandRunner,
    inspect_bootstrap_task,
    remove_bootstrap_task,
)
from readerpc_services import (
    PRODUCT_NAME,
    PcOcrServiceController,
    ReaderPCPaths,
    ReaderPCServiceError,
    format_pc_progress,
    read_codex_voice_activity,
    read_reader_context_status,
    write_disabled_reader_context_snapshot as write_offline_snapshot,
    write_recovering_reader_context_snapshot as write_recovering_snapshot,
    write_readerpc_status,
)
from voice_history_sidebar_sync import (
    CodexAppServerHistoryClient,
    CaptureBoundHistorySynchronizer,
    history_worker_lease,
    monitor_capture_history,
)


APP_VERSION = "0.1.33"
PREFERENCES_CONTRACT = "readerpc-server-config/1"
CODEX_VOICE_KEEPALIVE_CONTRACT = "reader-codex-voice-keepalive/1"
# 桥接模式旗标的独立意图文件(C# 启动时读取;keepalive/config/runtime-status 都是
# exact 合同不能加键)。bridge-only = C# 完全不装载 keepalive 链(不拉 Codex、不发
# 任何 F24)+ 语音动作(start/codex-voice-set)拒绝,上下文/快照/工具照常。
SERVICE_MODE_CONTRACT = "readerpc-service-mode/1"
SERVICE_MODE_FULL = "full"
SERVICE_MODE_BRIDGE_ONLY = "bridge-only"
POLL_INTERVAL_MS = 2_500
STATUS_PUBLISH_INTERVAL_SECONDS = 10.0
PC_RESTART_BACKOFF_SECONDS = 30.0
VOICE_RESTART_BACKOFF_SECONDS = 30.0
VOICE_START_TIMEOUT_SECONDS = 8.0
VOICE_START_POLL_SECONDS = 0.1
SHORTCUT_BROKER_READY_SECONDS = 2.0
SHORTCUT_BROKER_READY_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class ReaderPCHistoryStatus:
    """The independent service and Codex Voice gates for history sync."""

    service_online: bool
    capture_active: bool
    capture_generation: int | None = None


def write_disabled_reader_context_snapshot(
    bridge_paths: BridgePaths,
    **kwargs: Any,
) -> None:
    """Compatibility wrapper around the shared ReaderPC lifecycle writer."""

    write_offline_snapshot(bridge_paths.root, **kwargs)


def write_recovering_reader_context_snapshot(
    bridge_paths: BridgePaths,
    **kwargs: Any,
) -> None:
    """Publish a non-actionable snapshot while the enabled service recovers."""

    write_recovering_snapshot(bridge_paths.root, **kwargs)


def prepare_readerpc_shortcut_broker() -> WindowsShortcutBroker | None:
    """Retire the old logon bootstrap and own F24 for this server lifetime."""

    bridge_paths = BridgePaths.discover()
    control_paths = ControlPaths.discover()
    runner = SubprocessExactCommandRunner()
    inspection = inspect_bootstrap_task(
        bridge_paths,
        control_paths,
        runner,
    )
    if inspection.exists:
        if not inspection.owned:
            raise BridgeError(
                "同名后台引导器不属于 Reader；拒绝删除或共用。"
            )
        remove_bootstrap_task(
            bridge_paths,
            control_paths,
            runner,
        )
    # A Direct generation left by the retired logon bootstrap cannot be
    # adopted: it has no ReaderPC owner PID. Replace it before this process
    # starts its own bounded generation.
    stop_readerpc_voice(
        bridge_paths,
        WindowsProcessRunner(),
        disable_configuration=False,
    )
    broker = WindowsShortcutBroker()
    try:
        broker.start()
    except ShortcutBrokerError:
        # /End is bounded but the retired process can still be unwinding. Wait
        # for its pipe to disappear, then make this ReaderPC process the owner.
        broker.close()
        deadline = time.monotonic() + SHORTCUT_BROKER_READY_SECONDS
        while time.monotonic() < deadline:
            if not WindowsShortcutBroker.probe_available():
                replacement = WindowsShortcutBroker()
                replacement.start()
                return replacement
            time.sleep(SHORTCUT_BROKER_READY_POLL_SECONDS)
        raise
    return broker


def readerpc_history_sync_enabled(paths: BridgePaths) -> bool:
    """Enable Reader chat return while this server owns snapshot mode."""

    config = load_direct_config(paths)
    return bool(
        config is not None
        and config.get("localOptIn") is True
        and config.get("contextDeliveryMode") == CONTEXT_DELIVERY_SNAPSHOT
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_preferences(path: Path) -> dict[str, object]:
    defaults: dict[str, object] = {
        "keepPcPreprocessingOnline": True,
        "serviceMode": SERVICE_MODE_FULL,
    }
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
    # serviceMode 缺省容忍(旧偏好文件没有它)→ full;非法值也回 full,
    # 不 bump contract:旧版启动器只读自己认识的键,天然兼容。
    mode = value.get("serviceMode")
    return {
        "keepPcPreprocessingOnline": value["keepPcPreprocessingOnline"],
        "serviceMode": (
            mode
            if mode in (SERVICE_MODE_FULL, SERVICE_MODE_BRIDGE_ONLY)
            else SERVICE_MODE_FULL
        ),
    }


def save_preferences(
    path: Path,
    *,
    keep_pc_online: bool,
    service_mode: str = SERVICE_MODE_FULL,
) -> None:
    if service_mode not in (SERVICE_MODE_FULL, SERVICE_MODE_BRIDGE_ONLY):
        raise ReaderPCServiceError(f"未知服务模式 {service_mode}")
    _atomic_json(
        path,
        {
            "contract": PREFERENCES_CONTRACT,
            "keepPcPreprocessingOnline": bool(keep_pc_online),
            "serviceMode": service_mode,
        },
    )


def set_readerpc_service_mode(bridge_paths: BridgePaths, mode: str) -> None:
    """写 C# 启动时读取的模式意图文件。改模式必须随后重启直连服务才生效。"""

    if mode not in (SERVICE_MODE_FULL, SERVICE_MODE_BRIDGE_ONLY):
        raise ReaderPCServiceError(f"未知服务模式 {mode}")
    _atomic_json(
        bridge_paths.runtime_status.parent / "readerpc-service-mode.json",
        {"contract": SERVICE_MODE_CONTRACT, "mode": mode},
    )


def set_codex_voice_keep_active(
    bridge_paths: BridgePaths,
    enabled: bool,
) -> None:
    """Publish the ephemeral ReaderPC service intent consumed by Direct."""

    _atomic_json(
        bridge_paths.runtime_status.parent / "codex-voice-keepalive.json",
        {
            "contract": CODEX_VOICE_KEEPALIVE_CONTRACT,
            "enabled": bool(enabled),
        },
    )


def read_codex_voice_keep_active(
    bridge_paths: BridgePaths,
) -> bool | None:
    path = bridge_paths.runtime_status.parent / "codex-voice-keepalive.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"contract", "enabled"}
        or value.get("contract") != CODEX_VOICE_KEEPALIVE_CONTRACT
        or not isinstance(value.get("enabled"), bool)
    ):
        return None
    return value["enabled"]


def enable_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    bridge_only: bool = False,
) -> int:
    """Start the Direct generation owned by this ReaderPC process."""

    previous = load_direct_config(bridge_paths)
    if previous is None:
        message = (
            "现有电脑语音配置无效；拒绝静默覆盖。"
            if bridge_paths.direct_config.exists()
            else "尚未完成电脑语音配置，请先打开详细设置。"
        )
        try:
            write_recovering_reader_context_snapshot(bridge_paths)
        except Exception as exc:
            raise ReaderPCServiceError(
                f"{message} 同时无法撤销旧实时快照：{exc}"
            ) from exc
        raise ReaderPCServiceError(message)

    try:
        # 模式文件必须先于 start:C# 只在启动时读它。桥接模式下 keepalive 写 False
        # 只为磁盘状态一致(C# 传 null 路径根本不读它),真正的"不碰语音"由模式文件保证。
        set_readerpc_service_mode(
            bridge_paths,
            SERVICE_MODE_BRIDGE_ONLY if bridge_only else SERVICE_MODE_FULL,
        )
        set_codex_voice_keep_active(bridge_paths, not bridge_only)
        set_direct_config_enabled(
            bridge_paths,
            True,
            context_delivery_mode=CONTEXT_DELIVERY_SNAPSHOT,
        )
        return start_readerpc_voice(
            bridge_paths,
            process_runner,
            owner_pid=os.getpid(),
        )
    except Exception as exc:
        try:
            set_codex_voice_keep_active(bridge_paths, False)
        except Exception:
            pass
        try:
            write_recovering_reader_context_snapshot(bridge_paths)
        except Exception as rollback_exc:
            raise ReaderPCServiceError(
                f"电脑语音启动失败：{exc}；同时无法撤销旧实时快照："
                f"{rollback_exc}"
            ) from exc
        raise


def stop_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    disable_configuration: bool = True,
    terminate_service: bool = True,
) -> None:
    """Revoke ReaderPC intent and stop its exact Direct generation."""

    failures: list[str] = []
    lifecycle_writer = (
        write_disabled_reader_context_snapshot
        if disable_configuration
        else write_recovering_reader_context_snapshot
    )
    try:
        set_codex_voice_keep_active(bridge_paths, False)
    except Exception as exc:
        failures.append(f"撤销 ReaderPC 运行意图：{exc}")
    if disable_configuration:
        try:
            set_direct_config_enabled(bridge_paths, False)
        except Exception as exc:
            failures.append(f"关闭派生的电脑语音配置：{exc}")
    try:
        lifecycle_writer(bridge_paths)
    except Exception as exc:
        failures.append(f"撤销旧实时快照：{exc}")
    if terminate_service:
        try:
            status = read_direct_status(bridge_paths, process_runner)
            if status.service_online or bridge_paths.service_record.exists():
                stop_direct_service(bridge_paths, process_runner)
        except Exception as exc:
            failures.append(f"停止电脑语音服务：{exc}")
    try:
        lifecycle_writer(bridge_paths)
    except Exception as exc:
        failures.append(f"确认实时快照已撤销：{exc}")
    if failures:
        raise ReaderPCServiceError("；".join(failures))


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


def stop_readerpc_services(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    pc_ocr: PcOcrServiceController,
) -> None:
    """Stop every service owned by ReaderPC without touching Codex MCP clients."""

    failures: list[str] = []
    try:
        pc_ocr.stop()
    except Exception as exc:
        failures.append(f"PC 预处理：{exc}")

    try:
        stop_readerpc_voice(
            bridge_paths,
            process_runner,
            disable_configuration=True,
            terminate_service=False,
        )
    except Exception as exc:
        failures.append(f"电脑语音与上下文直连：{exc}")

    if failures:
        raise ReaderPCServiceError("；".join(failures))


def start_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    owner_pid: int | None = None,
    timeout_seconds: float = VOICE_START_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Start the direct service and require a matching fresh runtime heartbeat."""

    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ReaderPCServiceError("电脑语音启动确认时限无效。")
    current = read_direct_status(bridge_paths, process_runner)
    if not current.service_online and bridge_paths.service_record.exists():
        # A faulted/stale owned generation cannot be healed by starting a
        # second listener.  Stop only the PID authenticated by the strict
        # service record, then start one replacement generation.
        stop_direct_service(bridge_paths, process_runner)
    pid = start_direct_service(
        bridge_paths,
        process_runner,
        readerpc_owner_pid=owner_pid,
    )
    deadline = clock() + timeout_seconds
    last_reason = "runtime-status-offline-or-stale"
    while True:
        status = read_direct_status(bridge_paths, process_runner)
        last_reason = status.reason or last_reason
        if status.service_online and status.pid == pid:
            return pid
        if process_runner.executable_for_pid(pid) is None:
            stop_direct_service(bridge_paths, process_runner)
            raise ReaderPCServiceError(
                "电脑语音进程启动后立即退出"
                f"（{last_reason}）。电脑语音组件与当前配置可能不兼容。"
            )
        if clock() >= deadline:
            stop_direct_service(bridge_paths, process_runner)
            raise ReaderPCServiceError(
                "电脑语音进程已启动，但未在限定时间内写出有效状态"
                f"（{last_reason}）。"
            )
        sleeper(VOICE_START_POLL_SECONDS)


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
        self.closing = False
        self.service_lock = threading.Lock()
        self.last_pc_start_attempt = 0.0
        self.last_voice_start_attempt = 0.0
        self.voice_recovery_in_progress = False
        self.voice_start_in_progress = False
        self.voice_stop_in_progress = False
        self.voice_snapshot_offline_marked = False
        self.last_status_publish = 0.0
        self.history_stop_event = threading.Event()
        self.history_synchronizer = CaptureBoundHistorySynchronizer(
            root=self.bridge_paths.root,
            structured_history_client=CodexAppServerHistoryClient(),
        )
        self.history_thread = threading.Thread(
            target=self._run_history_sync,
            name="readerpc-voice-history",
            daemon=True,
        )
        preferences = load_preferences(self.readerpc_paths.preferences_file)
        self.keep_pc_online = tk.BooleanVar(
            value=preferences["keepPcPreprocessingOnline"]
        )
        self.bridge_only = tk.BooleanVar(
            value=preferences["serviceMode"] == SERVICE_MODE_BRIDGE_ONLY
        )

        root.title(PRODUCT_NAME)
        root.geometry("620x500")
        root.minsize(560, 450)
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        root.bind("<Unmap>", self._on_unmap, add="+")

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
                "最小化后驻留托盘；关闭时停止全部相关服务。"
            ),
            foreground="#596579",
            wraplength=570,
        ).pack(anchor="w", pady=(4, 14))

        self.voice_status, self.voice_detail, self.voice_button = self._service_row(
            outer,
            "电脑语音",
            "随 ReaderPC 服务器启动、保活并在服务器关闭时停止",
            None,
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
            "文字、分词与公式 · quality-first-v2",
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
            text="打开音频与连接配置",
            command=self.open_legacy_voice_settings,
        ).pack(side="right")
        mode_row = ttk.Frame(outer)
        mode_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            mode_row,
            text="仅桥接模式：不接管语音（上下文/快照/工具照常，语音留在本机）",
            variable=self.bridge_only,
            command=self.on_bridge_only_changed,
        ).pack(side="left")

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
                pystray.MenuItem("退出并停止全部服务", self._tray_exit),
            ),
        )
        threading.Thread(target=self._run_tray, name="readerpc-tray", daemon=True).start()
        self.history_thread.start()
        root.after(100, self._drain_events)
        root.after(250, self.refresh)
        root.after(600, self._ensure_pc_online)
        root.after(800, self._ensure_voice_online)

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

    def _run_history_sync(self) -> None:
        """Keep capture-bound voice turns beside ReaderPC's owned service."""

        with history_worker_lease(self.bridge_paths.root) as owned:
            if not owned:
                return
            monitor_capture_history(
                stop_event=self.history_stop_event,
                status_provider=self._history_status,
                enabled_provider=lambda: readerpc_history_sync_enabled(
                    self.bridge_paths
                ),
                synchronizer=self.history_synchronizer,
            )

    def _tray_show(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.show_window)

    def _tray_start_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(True))

    def _tray_stop_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(False))

    def _tray_exit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.request_exit)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_window(self) -> None:
        self.root.withdraw()

    def _on_unmap(self, event=None) -> None:
        if self.closed or self.closing:
            return
        if event is not None and getattr(event, "widget", self.root) is not self.root:
            return
        self.root.after(0, self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        if self.closed or self.closing:
            return
        try:
            if self.root.state() == "iconic":
                self.hide_window()
        except tk.TclError:
            pass

    def request_exit(self) -> None:
        if self.closed or self.closing:
            return
        self.closing = True
        self.busy = True
        self.show_window()
        self._set_buttons_enabled(False)
        self.footer.configure(
            text="正在停止 PC 预处理、电脑语音与 Reader 上下文直连…",
            foreground="#596579",
        )

        def worker() -> None:
            try:
                with self.service_lock:
                    stop_readerpc_services(
                        self.bridge_paths,
                        self.process_runner,
                        self.pc_ocr,
                    )
            except Exception as exc:
                self.events.put(("shutdown-error", exc))
            else:
                self.events.put(("shutdown-success", None))

        threading.Thread(
            target=worker,
            name="readerpc-shutdown",
            daemon=False,
        ).start()

    def _finish_exit(self) -> None:
        self.closed = True
        history_stop_event = getattr(self, "history_stop_event", None)
        if history_stop_event is not None:
            history_stop_event.set()
        history_thread = getattr(self, "history_thread", None)
        if history_thread is not None and history_thread.is_alive():
            history_thread.join(timeout=3)
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
        if self.busy or self.closing:
            return
        self.busy = True
        self.footer.configure(text=label)
        self._set_buttons_enabled(False)

        def worker() -> None:
            try:
                with self.service_lock:
                    if self.closing:
                        raise ReaderPCServiceError("ReaderPC 正在退出。")
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
                if kind == "shutdown-success":
                    self._finish_exit()
                    return
                if kind == "shutdown-error":
                    self.closing = False
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.show_window()
                    detail = f"无法安全退出：{value}"
                    self.footer.configure(text=detail, foreground="#c62828")
                    messagebox.showerror(PRODUCT_NAME, detail)
                    continue
                if self.closing and kind in {"task-error", "task-success"}:
                    continue
                if kind == "task-error":
                    self.voice_recovery_in_progress = False
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.footer.configure(text=f"操作失败：{value}", foreground="#c62828")
                    self.refresh()
                elif kind == "task-success":
                    self.voice_recovery_in_progress = False
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

    def _history_status(self) -> ReaderPCHistoryStatus:
        voice = self._voice_status()
        codex_voice = read_codex_voice_activity()
        return ReaderPCHistoryStatus(
            service_online=voice.service_online is True,
            capture_active=codex_voice.active is True,
            capture_generation=codex_voice.generation,
        )

    def _bridge_only_enabled(self) -> bool:
        """偏好里的桥接模式;部分构造(测试/早期启动)时按完整模式处理。"""
        var = getattr(self, "bridge_only", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def toggle_voice(self) -> None:
        current = self._voice_status()
        if not current.service_online:
            self._start_voice_task()

    def _start_voice_task(self) -> None:
        if self.busy or self.closing:
            return
        self.last_voice_start_attempt = time.monotonic()
        self.voice_recovery_in_progress = True
        self.voice_start_in_progress = True

        bridge_only = self._bridge_only_enabled()

        def start() -> int:
            try:
                pid = enable_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    bridge_only=bridge_only,
                )
                self.voice_snapshot_offline_marked = False
                return pid
            finally:
                self.voice_start_in_progress = False

        self._run_task(
            "正在恢复桥接与实时快照服务…"
            if bridge_only
            else "正在恢复电脑语音与实时快照服务…",
            start,
            "桥接与实时快照已恢复（语音未接管）。"
            if bridge_only
            else "电脑语音与实时快照服务已恢复；正在确认 Codex 语音。",
        )

    def _stop_voice_task(self) -> None:
        if self.busy or self.closing or self.voice_stop_in_progress:
            return
        self.voice_stop_in_progress = True

        def stop() -> None:
            try:
                stop_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    disable_configuration=True,
                )
                self.voice_snapshot_offline_marked = True
            finally:
                self.voice_stop_in_progress = False

        self._run_task(
            "ReaderPC 正在停止电脑语音与实时快照…",
            stop,
            "电脑语音与实时快照已停止。",
        )

    def toggle_pc(self) -> None:
        self._set_pc_running(not self.pc_ocr.status().running)

    def _set_pc_running(self, running: bool) -> None:
        self.keep_pc_online.set(running)
        save_preferences(
            self.readerpc_paths.preferences_file,
            keep_pc_online=running,
            service_mode=(
                SERVICE_MODE_BRIDGE_ONLY
                if self._bridge_only_enabled()
                else SERVICE_MODE_FULL
            ),
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

    def on_bridge_only_changed(self) -> None:
        """切模式 = 存偏好 + 停当前直连代际 + 按新模式重启(C# 只在启动时读模式文件)。"""
        bridge_only = bool(self.bridge_only.get())
        save_preferences(
            self.readerpc_paths.preferences_file,
            keep_pc_online=bool(self.keep_pc_online.get()),
            service_mode=(
                SERVICE_MODE_BRIDGE_ONLY if bridge_only else SERVICE_MODE_FULL
            ),
        )
        if self.busy or self.closing:
            return

        def switch() -> int:
            try:
                stop_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    disable_configuration=False,
                )
            except Exception:
                pass   # 旧代际可能本就不在;重启路径自身会再校验
            self.voice_start_in_progress = True
            try:
                pid = enable_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    bridge_only=bridge_only,
                )
                self.voice_snapshot_offline_marked = False
                return pid
            finally:
                self.voice_start_in_progress = False

        self.last_voice_start_attempt = time.monotonic()
        self._run_task(
            "正在切换到仅桥接模式…" if bridge_only else "正在切换到完整模式…",
            switch,
            "已切到仅桥接模式：语音未接管，上下文与工具照常。"
            if bridge_only
            else "已切回完整模式：电脑语音恢复接管。",
        )

    def _ensure_pc_online(self) -> None:
        if self.closed or self.closing:
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

    def _reconcile_service_mode_intent(self) -> None:
        """App 经桥请求换模式(C# 只写意图文件);ReaderPC 是唯一的服务生命周期
        所有者,在这里收敛:文件模式 ≠ 当前模式 → 同步 UI/偏好并按新模式重启。
        重启后文件==已应用模式,循环自然静止。"""
        try:
            value = json.loads(
                (self.bridge_paths.runtime_status.parent
                 / "readerpc-service-mode.json").read_text("utf-8")
            )
        except (OSError, ValueError, TypeError):
            return   # TypeError:部分构造(测试 Mock 路径)——按无意图处理
        if not (isinstance(value, dict)
                and value.get("contract") == SERVICE_MODE_CONTRACT):
            return
        mode = value.get("mode")
        if mode not in (SERVICE_MODE_FULL, SERVICE_MODE_BRIDGE_ONLY):
            return
        wanted = mode == SERVICE_MODE_BRIDGE_ONLY
        if wanted == self._bridge_only_enabled():
            return
        if self.busy or self.closing or self.voice_start_in_progress:
            return
        var = getattr(self, "bridge_only", None)
        if var is None:
            return
        var.set(wanted)
        self.on_bridge_only_changed()

    def _ensure_voice_online(self) -> None:
        if self.closed or self.closing:
            return
        try:
            self._reconcile_service_mode_intent()
            # Do not race the known start transaction: it owns config, process
            # and the first fresh runtime heartbeat. Other busy work (for
            # example PC preprocessing) must not leave a stale ready snapshot.
            if not self.voice_start_in_progress:
                status = self._voice_status()
                if status.service_online:
                    self.voice_snapshot_offline_marked = False
                else:
                    if not self.voice_snapshot_offline_marked:
                        write_recovering_reader_context_snapshot(
                            self.bridge_paths
                        )
                        self.voice_snapshot_offline_marked = True
                    if (
                        not self.busy
                        and time.monotonic() - self.last_voice_start_attempt
                        >= VOICE_RESTART_BACKOFF_SECONDS
                    ):
                        self._start_voice_task()
        except (BridgeError, ReaderPCServiceError, OSError, ValueError) as exc:
            self.footer.configure(
                text=f"电脑语音离线状态确认失败：{exc}",
                foreground="#c62828",
            )
        finally:
            self.root.after(5_000, self._ensure_voice_online)

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
            codex_voice = read_codex_voice_activity()
            pc = self.pc_ocr.status()
            context = read_reader_context_status(
                self.bridge_paths.root / "runtime" / "reader-context-snapshot.json"
            )
            bridge_only = self._bridge_only_enabled()
            if not voice.service_online:
                voice_label = (
                    "正在恢复"
                    if self.voice_recovery_in_progress
                    else "离线 · 等待重试"
                )
                voice_color = "#b26a00"
            elif bridge_only:
                # 桥接模式:直连在线而 Codex 语音未接管是**正常态**,标绿不标黄。
                voice_label = "桥接模式 · 语音未接管"
                voice_color = "#167347"
            elif codex_voice.active is True:
                voice_label = "在线 · Codex 语音工作中"
                voice_color = "#167347"
            else:
                voice_label = (
                    "直连在线 · 等待 Codex 语音"
                    if codex_voice.status == "available"
                    else "直连在线 · 无法确认 Codex 语音"
                )
                voice_color = "#b26a00"
            self.voice_status.configure(text=voice_label, foreground=voice_color)
            self.voice_detail.configure(
                text=(
                    f"Reader 已连接 · PID {voice.pid}"
                    if voice.reader_connected
                    else f"等待 Reader 连接 · {voice.reason}"
                )
            )
            if self.voice_button is not None:
                self.voice_button.configure(text="立即重试")

            if not voice.service_online:
                context_label = "等待电脑语音服务恢复"
                context_color = "#b26a00"
                context_detail = "服务恢复后会自动重新接收 Reader 快照。"
            else:
                context_label = context.state_label
                context_color = "#167347" if context.fresh else "#6b7280"
                context_detail = (
                    f"{context.kind or '内容'} · {context.title}"
                    if context.title
                    else "打开 Reader App 或启用扩展后会在这里显示。"
                )
            self.context_status.configure(
                text=context_label,
                foreground=context_color,
            )
            self.context_detail.configure(text=context_detail)

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
                        "intentEnabled": not bridge_only,
                        "codexVoiceStatus": codex_voice.status,
                        "codexVoiceActive": codex_voice.active,
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
    # ReaderPC is the sole lifecycle owner. Retire the old logon bootstrap,
    # replace any ownerless Direct generation, and hold the one F24 broker for
    # this process lifetime.
    broker = prepare_readerpc_shortcut_broker()
    window: ReaderPCWindow | None = None
    try:
        root = tk.Tk()
        window = ReaderPCWindow(root)
        root.mainloop()
    finally:
        if window is not None and not window.closed:
            stop_readerpc_services(
                window.bridge_paths,
                window.process_runner,
                window.pc_ocr,
            )
        if broker is not None:
            broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
