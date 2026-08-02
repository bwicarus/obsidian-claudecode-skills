from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, ContextManager

from bridge_core import (
    BridgeError,
    BridgePaths,
    CaptureEndpoint,
    CONTEXT_DELIVERY_LEGACY,
    CONTEXT_DELIVERY_MODES,
    CONTEXT_DELIVERY_SNAPSHOT,
    DIRECT_WSS_URL,
    DirectStatus,
    FIXED_APP_KIND,
    FIXED_LISTEN_HOST,
    FIXED_LISTEN_PORT,
    FIXED_OUTPUT_SCOPE,
    FIXED_SHORTCUT,
    ProcessRunner,
    RenderEndpoint,
    WindowsShortcutBroker,
    WindowsProcessRunner,
    build_self_test_report,
    disable_and_stop_direct_service,
    enumerate_active_capture_endpoints,
    enumerate_active_render_endpoints,
    legacy_microphone_config_requires_migration,
    load_direct_config,
    read_direct_status,
    run_idle_bootstrap,
    save_enabled_config,
    start_direct_service,
)
from control_plane import (
    ControlPaths,
    ExactCommandRunner,
    ServeInspection,
    SubprocessExactCommandRunner,
    TaskInspection,
    apply_tailscale_serve,
    inspect_bootstrap_task,
    inspect_tailscale_serve,
    install_bootstrap_task,
    remove_bootstrap_task,
    remove_tailscale_serve,
    run_bootstrap_task_if_owned,
)


APP_TITLE = "电脑客户端桥接器"
APP_VERSION = "0.7.0-snapshot-mcp-source"
LEGACY_CAPTURE_OPTION = "不启用自动路由（兼容 /4）"


class BridgeWindow:
    _ACTION_BUTTON_NAMES = (
        "enable_button",
        "start_button",
        "disable_button",
        "refresh_button",
        "control_refresh_button",
        "bootstrap_install_button",
        "bootstrap_remove_button",
        "tailscale_apply_button",
        "tailscale_remove_button",
        "audio_settings_button",
    )

    def __init__(
        self,
        root: tk.Tk,
        *,
        paths: BridgePaths | None = None,
        process_runner: ProcessRunner | None = None,
        control_paths: ControlPaths | None = None,
        control_runner: ExactCommandRunner | None = None,
        temporary_directory_factory: (
            Callable[[], ContextManager[str]] | None
        ) = None,
        render_endpoint_provider: (
            Callable[[], list[RenderEndpoint]] | None
        ) = None,
        capture_endpoint_provider: (
            Callable[[], list[CaptureEndpoint]] | None
        ) = None,
    ) -> None:
        self.root = root
        self.paths = paths or BridgePaths.discover()
        self.process_runner = process_runner or WindowsProcessRunner()
        self.control_paths = control_paths or ControlPaths.discover()
        self.control_runner = (
            control_runner or SubprocessExactCommandRunner()
        )
        self.temporary_directory_factory = (
            temporary_directory_factory
        )
        self.render_endpoint_provider = render_endpoint_provider or (
            lambda: enumerate_active_render_endpoints(
                self.paths.native_host
            )
        )
        self.capture_endpoint_provider = capture_endpoint_provider or (
            lambda: enumerate_active_capture_endpoints(
                self.paths.native_host
            )
        )
        self.render_endpoints: list[RenderEndpoint] = []
        self.capture_endpoints: list[CaptureEndpoint] = []
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False

        root.title(APP_TITLE)
        root.geometry("700x1140")
        root.minsize(640, 960)
        root.configure(bg="#eef2f7")

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure(
            "Heading.TLabel",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "Status.TLabel",
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Primary.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

        outer = ttk.Frame(root, padding=(20, 16, 20, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text=(
                "本窗口启动和刷新只读取状态；不会采音、启动 GPT、"
                "发送快捷键、操作浏览器或修改 Tailscale。"
            ),
            foreground="#44546a",
            wraplength=600,
        ).pack(anchor="w", pady=(3, 12))

        endpoint_frame = self._section(outer, "PWA 固定直连地址")
        ttk.Label(
            endpoint_frame,
            text=(
                "在 Reader/PWA 中使用下列固定 WSS 地址；"
                "可在只读输入框中选择并复制："
            ),
            foreground="#44546a",
            wraplength=630,
        ).pack(anchor="w")
        self.endpoint_value = tk.StringVar(value=DIRECT_WSS_URL)
        self.endpoint_entry = ttk.Entry(
            endpoint_frame,
            textvariable=self.endpoint_value,
            state="readonly",
            width=78,
        )
        self.endpoint_entry.pack(fill="x", pady=(8, 0))

        status_frame = self._section(outer, "直连真实状态")
        self.config_status = ttk.Label(
            status_frame,
            text="○ 配置已启用：读取中",
            style="Status.TLabel",
        )
        self.config_status.pack(anchor="w")
        self.service_status = ttk.Label(
            status_frame,
            text="○ 直连服务在线：读取中",
            style="Status.TLabel",
        )
        self.service_status.pack(anchor="w", pady=(5, 0))
        self.reader_status = ttk.Label(
            status_frame,
            text="○ Reader 已连接：读取中",
            style="Status.TLabel",
        )
        self.reader_status.pack(anchor="w", pady=(5, 0))
        self.error_status = ttk.Label(
            status_frame,
            text="○ 最近失败：无",
            style="Status.TLabel",
        )
        self.error_status.pack(anchor="w", pady=(5, 0))

        config_frame = self._section(outer, "本机配置")
        ttk.Label(
            config_frame,
            text=(
                "这里列出 Windows 当前已有的播放端点；桥接程序本身不会创建"
                "虚拟设备。未安装两根彼此独立、已签名的虚拟音频线缆时，"
                "不要把 Realtek、Steam、Oculus 等现有端点当作 A/B。"
            ),
            foreground="#9a6700",
            wraplength=590,
        ).pack(anchor="w", pady=(0, 8))
        virtual_microphone_row = ttk.Frame(config_frame)
        virtual_microphone_row.pack(fill="x")
        ttk.Label(
            virtual_microphone_row,
            text="虚拟麦克风 A",
            width=14,
        ).pack(
            side="left"
        )
        self.virtual_microphone_combo = ttk.Combobox(
            virtual_microphone_row,
            state="readonly",
            width=49,
        )
        self.virtual_microphone_combo.pack(
            side="left",
            fill="x",
            expand=True,
        )
        ttk.Label(
            config_frame,
            text=(
                "A 的播放端接收 Reader 网页麦克风；"
                "它与下面的 A 录音端是两个独立 MMDevice ID。"
            ),
            foreground="#44546a",
            wraplength=590,
        ).pack(anchor="w", pady=(4, 0))

        virtual_microphone_capture_row = ttk.Frame(config_frame)
        virtual_microphone_capture_row.pack(fill="x", pady=(9, 0))
        ttk.Label(
            virtual_microphone_capture_row,
            text="Codex 麦克风输入",
            width=14,
        ).pack(side="left")
        self.virtual_microphone_capture_combo = ttk.Combobox(
            virtual_microphone_capture_row,
            state="readonly",
            width=49,
        )
        self.virtual_microphone_capture_combo.pack(
            side="left",
            fill="x",
            expand=True,
        )
        ttk.Label(
            config_frame,
            text=(
                "明确选择 A 的录音端（eCapture）。"
                "桥接器不会从 A 的播放端 ID 推导它；"
                "选中后保存 /5 并为 Codex 自动切换输入。"
                "选择“兼容 /4”时，经确认保存且自动音频路由不会启用。"
            ),
            foreground="#44546a",
            wraplength=590,
        ).pack(anchor="w", pady=(4, 0))

        virtual_speaker_row = ttk.Frame(config_frame)
        virtual_speaker_row.pack(fill="x", pady=(9, 0))
        ttk.Label(
            virtual_speaker_row,
            text="虚拟扬声器 B",
            width=14,
        ).pack(side="left")
        self.virtual_speaker_combo = ttk.Combobox(
            virtual_speaker_row,
            state="readonly",
            width=49,
        )
        self.virtual_speaker_combo.pack(
            side="left",
            fill="x",
            expand=True,
        )
        ttk.Label(
            config_frame,
            text=(
                "B 是 Codex 应用的固定输出目标；bridge 不向 B 写入，"
                "只捕获 Codex 进程树。B 的录音侧不接物理扬声器，"
                "因此远端电脑不会从真实扬声器发声。端点存在并不代表"
                " Codex 已路由到 B；/5 会用按应用内部接口临时切换"
                " Codex 输入/输出并在结束后恢复，/4 才需手工设置。"
            ),
            foreground="#44546a",
            wraplength=590,
        ).pack(anchor="w", pady=(4, 0))
        self.audio_route_status = ttk.Label(
            config_frame,
            text="○ Codex 自动音频路由：读取中",
            foreground="#6b7280",
            wraplength=590,
        )
        self.audio_route_status.pack(anchor="w", pady=(7, 0))
        self.audio_settings_button = ttk.Button(
            config_frame,
            text="打开 Windows 音量混合器",
            command=self.on_open_audio_settings,
        )
        self.audio_settings_button.pack(anchor="w", pady=(7, 0))
        self.migration_status = ttk.Label(
            config_frame,
            text="",
            foreground="#9a6700",
            wraplength=590,
        )
        self.migration_status.pack(anchor="w", pady=(7, 0))

        context_mode_frame = ttk.LabelFrame(
            config_frame,
            text="Reader 上下文交付模式",
            padding=(10, 7),
        )
        context_mode_frame.pack(fill="x", pady=(10, 0))
        self.context_delivery_mode = tk.StringVar(
            value=CONTEXT_DELIVERY_SNAPSHOT
        )
        ttk.Radiobutton(
            context_mode_frame,
            text="旧注入（回滚备用，保持 Voice Typist 行为）",
            variable=self.context_delivery_mode,
            value=CONTEXT_DELIVERY_LEGACY,
        ).pack(anchor="w")
        ttk.Radiobutton(
            context_mode_frame,
            text="实验（本次候选）：Windows 本地快照 + 常驻 MCP（不注入正文）",
            variable=self.context_delivery_mode,
            value=CONTEXT_DELIVERY_SNAPSHOT,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            context_mode_frame,
            text=(
                "两条路径互斥。切换只在“保存”或“启动”时写入配置；"
                "旧配置升级后固定落在旧注入模式，不会静默开启实验。"
            ),
            foreground="#44546a",
            wraplength=560,
        ).pack(anchor="w", pady=(5, 0))

        ttk.Label(
            config_frame,
            text=(
                f"只监听 {FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}；"
                f"输出范围固定为 {FIXED_OUTPUT_SCOPE}，无全系统回退。"
                "单用户实验模式固定启用，Reader/PWA/扩展无需配对。"
            ),
            foreground="#245c3b",
            wraplength=590,
        ).pack(anchor="w", pady=(9, 0))

        config_buttons = ttk.Frame(config_frame)
        config_buttons.pack(fill="x", pady=(12, 0))
        self.enable_button = ttk.Button(
            config_buttons,
            text="保存并启用配置",
            style="Primary.TButton",
            command=self.on_enable_config,
        )
        self.enable_button.pack(side="left")
        self.disable_button = ttk.Button(
            config_buttons,
            text="停用并停止",
            command=self.on_disable_config,
        )
        self.disable_button.pack(side="left", padx=(8, 0))

        service_buttons = ttk.Frame(config_frame)
        service_buttons.pack(fill="x", pady=(9, 0))
        self.start_button = ttk.Button(
            service_buttons,
            text="启用并启动直连服务",
            command=self.on_start,
        )
        self.start_button.pack(side="left")
        self.refresh_button = ttk.Button(
            service_buttons,
            text="刷新真实状态",
            command=self.on_refresh,
        )
        self.refresh_button.pack(side="right")

        bootstrap_frame = self._section(
            outer,
            "登录后台引导器与 Tailscale Serve",
        )
        ttk.Label(
            bootstrap_frame,
            text=(
                "所有安装或回滚都必须先点按钮，再在确认框中确认。"
                "状态刷新只查询；不会注册任务、修改 Serve、启动服务、"
                "采音、启动 GPT 或发送快捷键。"
            ),
            foreground="#44546a",
            wraplength=630,
        ).pack(anchor="w")
        self.bootstrap_install_status = ttk.Label(
            bootstrap_frame,
            text="○ 登录后台引导器：尚未查询",
            style="Status.TLabel",
        )
        self.bootstrap_install_status.pack(anchor="w", pady=(9, 0))
        self.tailscale_install_status = ttk.Label(
            bootstrap_frame,
            text="○ Tailscale Serve：尚未查询",
            style="Status.TLabel",
        )
        self.tailscale_install_status.pack(anchor="w", pady=(5, 0))

        task_buttons = ttk.Frame(bootstrap_frame)
        task_buttons.pack(fill="x", pady=(10, 0))
        self.bootstrap_install_button = ttk.Button(
            task_buttons,
            text="安装登录后台引导器",
            command=self.on_install_bootstrap,
        )
        self.bootstrap_install_button.pack(side="left")
        self.bootstrap_remove_button = ttk.Button(
            task_buttons,
            text="回滚登录后台引导器",
            command=self.on_remove_bootstrap,
        )
        self.bootstrap_remove_button.pack(side="left", padx=(8, 0))

        serve_buttons = ttk.Frame(bootstrap_frame)
        serve_buttons.pack(fill="x", pady=(8, 0))
        self.tailscale_apply_button = ttk.Button(
            serve_buttons,
            text="配置路径级 Serve",
            command=self.on_apply_tailscale,
        )
        self.tailscale_apply_button.pack(side="left")
        self.tailscale_remove_button = ttk.Button(
            serve_buttons,
            text="回滚路径级 Serve",
            command=self.on_remove_tailscale,
        )
        self.tailscale_remove_button.pack(side="left", padx=(8, 0))
        self.control_refresh_button = ttk.Button(
            serve_buttons,
            text="只读刷新安装状态",
            command=self.on_control_refresh,
        )
        self.control_refresh_button.pack(side="right")

        target_frame = self._section(outer, "本机固定目标")
        ttk.Label(
            target_frame,
            text=(
                f"应用：{FIXED_APP_KIND}；快捷键：{FIXED_SHORTCUT}。"
                "Reader 不能提交路径、AUMID 或命令；只有电话 START "
                "才可请求后续启动应用、采音和快捷键动作。"
            ),
            foreground="#44546a",
            wraplength=590,
        ).pack(anchor="w")

        self.footer = ttk.Label(
            outer,
            text=f"版本 {APP_VERSION}",
            foreground="#6b7280",
            wraplength=600,
        )
        self.footer.pack(anchor="w", side="bottom", pady=(7, 0))

        self.on_refresh()
        self.root.after(100, self.poll_events)

    def _section(self, parent: ttk.Frame, heading: str) -> ttk.Frame:
        card = ttk.LabelFrame(parent, text=heading, padding=(14, 12))
        card.pack(fill="x", pady=(0, 11))
        return card

    def set_busy(
        self,
        busy: bool,
        footer: str | None = None,
    ) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        # Missing/optional controls are ignored so headless tests and older
        # packaged layouts cannot prevent the requested action from running.
        for name in self._ACTION_BUTTON_NAMES:
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)
        if footer and getattr(self, "footer", None) is not None:
            self.footer.configure(text=footer)

    def selected_virtual_endpoints(
        self,
    ) -> tuple[RenderEndpoint, RenderEndpoint]:
        microphone_index = self.virtual_microphone_combo.current()
        speaker_index = self.virtual_speaker_combo.current()
        if (
            microphone_index < 0
            or microphone_index >= len(self.render_endpoints)
        ):
            raise BridgeError("请明确选择虚拟麦克风 A 的播放端点。")
        if (
            speaker_index < 0
            or speaker_index >= len(self.render_endpoints)
        ):
            raise BridgeError("请明确选择虚拟扬声器 B 的播放端点。")
        virtual_microphone = self.render_endpoints[microphone_index]
        virtual_speaker = self.render_endpoints[speaker_index]
        if virtual_microphone.endpoint_id == virtual_speaker.endpoint_id:
            raise BridgeError("虚拟麦克风 A 与虚拟扬声器 B 不能相同。")
        return virtual_microphone, virtual_speaker

    def selected_virtual_microphone_capture_endpoint(
        self,
    ) -> CaptureEndpoint | None:
        capture_index = self.virtual_microphone_capture_combo.current()
        if capture_index == 0:
            return None
        if (
            capture_index < 1
            or capture_index > len(self.capture_endpoints)
        ):
            raise BridgeError(
                "Codex 虚拟麦克风输入选择已失效，请刷新后重选。"
            )
        return self.capture_endpoints[capture_index - 1]

    def selected_context_delivery_mode(self) -> str:
        selector = getattr(self, "context_delivery_mode", None)
        if selector is None:
            return CONTEXT_DELIVERY_LEGACY
        mode = str(selector.get())
        if mode not in CONTEXT_DELIVERY_MODES:
            raise BridgeError("请选择有效的 Reader 上下文交付模式。")
        return mode

    @staticmethod
    def _restore_combo_selection(
        combo: ttk.Combobox,
        endpoints: list[RenderEndpoint] | list[CaptureEndpoint],
        selected_id: str,
    ) -> None:
        combo.configure(
            values=[item.display_name for item in endpoints]
        )
        index = next(
            (
                item_index
                for item_index, item in enumerate(endpoints)
                if item.endpoint_id == selected_id
            ),
            -1,
        )
        if index >= 0:
            combo.current(index)
        else:
            # Deliberately no first/default fallback.  A missing or stale
            # endpoint must remain visibly unselected.
            combo.set("")

    def _refresh_render_endpoints(
        self,
        virtual_microphone_id: str = "",
        virtual_speaker_id: str = "",
    ) -> None:
        self.render_endpoints = self.render_endpoint_provider()
        self._restore_combo_selection(
            self.virtual_microphone_combo,
            self.render_endpoints,
            virtual_microphone_id,
        )
        self._restore_combo_selection(
            self.virtual_speaker_combo,
            self.render_endpoints,
            virtual_speaker_id,
        )

    def _refresh_capture_endpoints(
        self,
        virtual_microphone_capture_id: str = "",
    ) -> None:
        self.capture_endpoints = self.capture_endpoint_provider()
        self.virtual_microphone_capture_combo.configure(
            values=[
                LEGACY_CAPTURE_OPTION,
                *[
                    item.display_name
                    for item in self.capture_endpoints
                ],
            ]
        )
        if not virtual_microphone_capture_id:
            self.virtual_microphone_capture_combo.current(0)
            return
        index = next(
            (
                item_index
                for item_index, item in enumerate(
                    self.capture_endpoints
                )
                if item.endpoint_id
                    == virtual_microphone_capture_id
            ),
            -1,
        )
        if index >= 0:
            self.virtual_microphone_capture_combo.current(index + 1)
        else:
            # A configured /5 capture endpoint that is currently inactive
            # is not the same as an explicit legacy /4 choice.
            self.virtual_microphone_capture_combo.set("")

    def _render_audio_route_config_status(
        self,
        config: dict[str, Any] | None,
    ) -> None:
        if config and config.get("virtualSpeakerCaptureEndpointId"):
            self.audio_route_status.configure(
                text=(
                    "● 固定 A/B 音频总线：已启用（/6；"
                    "不再追踪 Codex AudioService 进程）"
                ),
                foreground="#167347",
            )
        elif config and config.get(
            "virtualMicrophoneCaptureEndpointId"
        ):
            capture_id = str(
                config["virtualMicrophoneCaptureEndpointId"]
            )
            if any(
                item.endpoint_id == capture_id
                for item in self.capture_endpoints
            ):
                self.audio_route_status.configure(
                    text=(
                        "● 旧版 Codex 自动音频路由：已启用（/5，"
                        "显式 eCapture；通话结束恢复原选择）"
                    ),
                    foreground="#167347",
                )
            else:
                self.audio_route_status.configure(
                    text=(
                        "⚠ Codex 自动音频路由：/5 已配置，但"
                        " eCapture 当前不在 Active 列表；启动将拒绝"
                    ),
                    foreground="#9a6700",
                )
        else:
            self.audio_route_status.configure(
                text=(
                    "○ Codex 自动音频路由：未启用（/4 兼容模式；"
                    "请选择 Codex 麦克风输入后重新保存）"
                ),
                foreground="#9a6700",
            )

    @staticmethod
    def _status_text(
        label: str,
        active: bool,
        detail: str,
    ) -> tuple[str, str]:
        return (
            f"{'●' if active else '○'} {label}：{detail}",
            "#167347" if active else "#6b7280",
        )

    def render_status(self, status: DirectStatus) -> None:
        text, color = self._status_text(
            "配置已启用",
            status.configuration_enabled,
            "是" if status.configuration_enabled else "否",
        )
        self.config_status.configure(text=text, foreground=color)

        text, color = self._status_text(
            "直连服务在线",
            status.service_online,
            (
                f"是（PID {status.pid}）"
                if status.service_online
                else f"否（{status.reason}）"
            ),
        )
        self.service_status.configure(text=text, foreground=color)

        text, color = self._status_text(
            "Reader 已连接",
            status.reader_connected,
            "是" if status.reader_connected else "否",
        )
        self.reader_status.configure(text=text, foreground=color)
        if status.last_error is None:
            self.error_status.configure(
                text="○ 最近失败：无",
                foreground="#6b7280",
            )
        else:
            error = status.last_error
            hresult = (
                f"，HRESULT {error['hresult']}"
                if error["hresult"] is not None
                else ""
            )
            self.error_status.configure(
                text=(
                    "⚠ 最近失败："
                    f"{error['code']} / {error['stage']}{hresult}"
                ),
                foreground="#9a6700",
            )

    def refresh_static(self) -> DirectStatus:
        config = load_direct_config(self.paths)
        selected_microphone_id = (
            str(config.get("virtualMicrophoneRenderEndpointId", ""))
            if config else ""
        )
        selected_speaker_id = (
            str(config.get("virtualSpeakerRenderEndpointId", ""))
            if config else ""
        )
        selected_capture_id = (
            str(
                config.get(
                    "virtualMicrophoneCaptureEndpointId",
                    "",
                )
            )
            if config else ""
        )
        if config:
            self.context_delivery_mode.set(
                str(config["contextDeliveryMode"])
            )
        previous_microphone = ""
        microphone_index = self.virtual_microphone_combo.current()
        if 0 <= microphone_index < len(self.render_endpoints):
            previous_microphone = self.render_endpoints[
                microphone_index
            ].endpoint_id
        previous_speaker = ""
        speaker_index = self.virtual_speaker_combo.current()
        if 0 <= speaker_index < len(self.render_endpoints):
            previous_speaker = self.render_endpoints[
                speaker_index
            ].endpoint_id
        previous_capture = ""
        capture_index = self.virtual_microphone_capture_combo.current()
        if 1 <= capture_index <= len(self.capture_endpoints):
            previous_capture = self.capture_endpoints[
                capture_index - 1
            ].endpoint_id
        self._refresh_render_endpoints(
            selected_microphone_id or previous_microphone,
            selected_speaker_id or previous_speaker,
        )
        self._refresh_capture_endpoints(
            selected_capture_id or previous_capture,
        )
        self._render_audio_route_config_status(config)
        migration_required = legacy_microphone_config_requires_migration(
            self.paths
        )
        self.migration_status.configure(
            text=(
                "⚠ legacy-migration-required："
                "检测到旧 microphoneEndpointId；"
                "该值不会作为 v3 回退。请选择 A/B 并明确保存以迁移。"
                if migration_required else ""
            )
        )
        status = read_direct_status(self.paths, self.process_runner)
        self.render_status(status)
        return status

    def run_task(
        self,
        label: str,
        action: Callable[[], Any],
        success: Callable[[Any], None],
    ) -> None:
        if self.busy:
            return
        self.set_busy(True, label)

        def worker() -> None:
            try:
                result = action()
            except Exception as error:
                self.events.put(("error", error))
            else:
                self.events.put(("success", (success, result)))

        threading.Thread(target=worker, daemon=True).start()

    def poll_events(self) -> None:
        try:
            kind, payload = self.events.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll_events)
            return
        self.set_busy(False)
        if kind == "error":
            self.refresh_static()
            self.footer.configure(text=f"操作失败：{payload}")
            messagebox.showerror(APP_TITLE, str(payload), parent=self.root)
        else:
            callback, result = payload
            callback(result)
        self.root.after(100, self.poll_events)

    def on_enable_config(self) -> None:
        try:
            virtual_microphone, virtual_speaker = (
                self.selected_virtual_endpoints()
            )
            virtual_microphone_capture = (
                self.selected_virtual_microphone_capture_endpoint()
            )
            context_delivery_mode = (
                self.selected_context_delivery_mode()
            )
        except BridgeError as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        legacy_migration = legacy_microphone_config_requires_migration(
            self.paths
        )
        if not self._confirm_mutation(
            (
                "保存并启用固定 A/B 音频总线"
                if virtual_microphone_capture is not None
                else "保存 /4 兼容配置（自动路由未启用）"
            ),
            (
                "将写入两个互不相同的 Active eRender 端点、"
                + (
                    "一个另行明确选择的 A 录音端，并按虚拟扬声器"
                    " B 的同名端点自动确定 B 录音端，保存 /6；"
                    "通话将直接使用固定 A/B 总线，不再查找"
                    " AudioService 进程；"
                    if virtual_microphone_capture is not None
                    else
                    "但未选择 Codex 虚拟麦克风输入；将明确保存"
                    "兼容 /4，按应用自动路由不会启用；"
                )
                + "固定 Reader HTTPS 来源、"
                "127.0.0.1:43128 和 process-only 边界；"
                "单用户实验模式会固定启用且无需配对；"
                f"上下文交付模式将设为 {context_delivery_mode}；"
                + (
                    "旧 microphoneEndpointId 将被明确替换且不会回退；"
                    if legacy_migration else ""
                )
                + "不会启动服务或音频。"
            ),
        ):
            return

        def action() -> dict[str, Any]:
            active = self.render_endpoint_provider()
            active_capture = (
                self.capture_endpoint_provider()
                if virtual_microphone_capture is not None
                else None
            )
            return save_enabled_config(
                self.paths,
                virtual_microphone,
                virtual_speaker,
                active_render_endpoints=active,
                active_capture_endpoints=active_capture,
                allow_legacy_migration=legacy_migration,
                context_delivery_mode=context_delivery_mode,
                virtual_microphone_capture_endpoint_id=(
                    virtual_microphone_capture.endpoint_id
                    if virtual_microphone_capture is not None
                    else None
                ),
            )

        def success(saved: dict[str, Any]) -> None:
            self.refresh_static()
            self.footer.configure(
                text=(
                    "配置已启用；"
                    + (
                        "固定 A/B 音频总线已启用；"
                        if saved.get(
                            "virtualMicrophoneCaptureEndpointId"
                        )
                        else "当前为 /4 兼容模式，自动音频路由未启用；"
                    )
                    + "没有启动服务、采音、GPT 或快捷键。"
                )
            )

        self.run_task("正在保存本机直连配置…", action, success)

    def on_disable_config(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "将先原子写入 localOptIn=false，再只停止 PID 与 EXE "
            "路径均匹配的直连代理。继续吗？",
            parent=self.root,
        ):
            return

        def action() -> tuple[bool, bool]:
            return disable_and_stop_direct_service(
                self.paths,
                self.process_runner,
            )

        def success(result: tuple[bool, bool]) -> None:
            _, stopped = result
            self.refresh_static()
            self.footer.configure(
                text=(
                    "本机直连已先停用并安全停止服务。"
                    if stopped
                    else "本机直连已停用；没有匹配服务需要停止。"
                )
            )

        self.run_task(
            "正在先停用配置、再安全停止服务…",
            action,
            success,
        )

    def on_start(self) -> None:
        try:
            virtual_microphone, virtual_speaker = (
                self.selected_virtual_endpoints()
            )
            virtual_microphone_capture = (
                self.selected_virtual_microphone_capture_endpoint()
            )
            context_delivery_mode = (
                self.selected_context_delivery_mode()
            )
        except BridgeError as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        legacy_migration = legacy_microphone_config_requires_migration(
            self.paths
        )
        if not self._confirm_mutation(
            (
                "启用并启动空闲直连服务"
                if virtual_microphone_capture is not None
                else "以 /4 兼容模式启动（自动路由未启用）"
            ),
            (
                "先原子保存两个虚拟播放端点并写入 localOptIn=true；"
                + (
                    "另行选择的虚拟麦克风 eCapture 将启用 /5 "
                    "按应用自动路由；"
                    if virtual_microphone_capture is not None
                    else "未选择虚拟麦克风 eCapture，将明确使用 /4，"
                    "按应用自动路由不会启用；"
                )
                + f"上下文交付模式为 {context_delivery_mode}；"
                + (
                    "旧 microphoneEndpointId 将被明确替换；"
                    if legacy_migration else ""
                )
                + "若已安装且 ownership "
                "通过则只运行后台 supervisor，否则直接启动固定 C# "
                "监听器并明确标记本登录未受监督。"
            ),
        ):
            return

        def action() -> tuple[str, int | None]:
            task = inspect_bootstrap_task(
                self.paths,
                self.control_paths,
                self.control_runner,
            )
            if task.exists and not task.owned:
                raise BridgeError(
                    "同名后台任务 ownership 未通过；"
                    "拒绝启用或旁路启动。"
                )
            active = self.render_endpoint_provider()
            active_capture = (
                self.capture_endpoint_provider()
                if virtual_microphone_capture is not None
                else None
            )
            save_enabled_config(
                self.paths,
                virtual_microphone,
                virtual_speaker,
                active_render_endpoints=active,
                active_capture_endpoints=active_capture,
                allow_legacy_migration=legacy_migration,
                context_delivery_mode=context_delivery_mode,
                virtual_microphone_capture_endpoint_id=(
                    virtual_microphone_capture.endpoint_id
                    if virtual_microphone_capture is not None
                    else None
                ),
            )
            if run_bootstrap_task_if_owned(
                self.paths,
                self.control_paths,
                self.control_runner,
            ):
                return "supervised", None
            pid = start_direct_service(
                self.paths,
                self.process_runner,
            )
            return "direct", pid

        def success(result: tuple[str, int | None]) -> None:
            mode, pid = result
            self.refresh_static()
            if mode == "supervised":
                self.footer.configure(
                    text=(
                        "已请求运行 ownership 通过的后台 supervisor；"
                        "在线仍以新鲜 runtime status 为准。"
                    ),
                    foreground="#245c3b",
                )
            else:
                self.footer.configure(
                    text=(
                        f"直连监听器已请求启动（PID {pid}）；"
                        "本登录未受后台 supervisor 保护。"
                    ),
                    foreground="#9a6700",
                )

        self.run_task(
            "正在启用配置并选择受监督启动路径…",
            action,
            success,
        )

    def on_refresh(self) -> None:
        try:
            status = self.refresh_static()
        except Exception as error:
            if getattr(self, "footer", None) is not None:
                self.footer.configure(text=f"状态读取失败：{error}")
            return
        if getattr(self, "footer", None) is not None:
            self.footer.configure(
                text=(
                    f"状态已刷新：{status.reason}。"
                    "刷新没有启动服务、应用、采音或快捷键。"
                )
            )

    def _confirm_mutation(
        self,
        heading: str,
        detail: str,
    ) -> bool:
        return messagebox.askyesno(
            APP_TITLE,
            f"{heading}\n\n{detail}\n\n是否继续？",
            parent=self.root,
        )

    def on_open_audio_settings(self) -> None:
        try:
            os.startfile("ms-settings:apps-volume")
        except OSError as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        self.footer.configure(
            text=(
                "已打开 Windows 音量混合器；请将 Codex/ChatGPT "
                "输出明确选择为虚拟扬声器 B。未修改全局默认设备。"
            )
        )

    def render_control_status(
        self,
        task: TaskInspection,
        serve: ServeInspection,
    ) -> None:
        if task.exists and task.owned:
            task_text = "● 登录后台引导器：已安装且合同匹配"
            task_color = "#167347"
        elif task.exists:
            task_text = "⚠ 登录后台引导器：同名未知任务，拒绝改动"
            task_color = "#9a6700"
        else:
            task_text = "○ 登录后台引导器：未安装"
            task_color = "#6b7280"
        self.bootstrap_install_status.configure(
            text=task_text,
            foreground=task_color,
        )

        serve_labels = {
            "ours": (
                "● Tailscale Serve：固定路径已指向本机直连端口",
                "#167347",
            ),
            "empty": ("○ Tailscale Serve：未配置", "#6b7280"),
            "foreign": (
                "⚠ Tailscale Serve：存在其它配置，拒绝覆盖",
                "#9a6700",
            ),
        }
        serve_text, serve_color = serve_labels.get(
            serve.state,
            ("○ Tailscale Serve：状态未知", "#6b7280"),
        )
        self.tailscale_install_status.configure(
            text=serve_text,
            foreground=serve_color,
        )

    def on_control_refresh(self) -> None:
        def action() -> tuple[TaskInspection, ServeInspection]:
            return (
                inspect_bootstrap_task(
                    self.paths,
                    self.control_paths,
                    self.control_runner,
                ),
                inspect_tailscale_serve(
                    self.control_paths,
                    self.control_runner,
                ),
            )

        def success(
            value: tuple[TaskInspection, ServeInspection],
        ) -> None:
            self.render_control_status(*value)
            self.footer.configure(
                text=(
                    "已完成只读查询；未注册/删除任务，"
                    "未应用/关闭 Serve，也未启动服务或音频。"
                )
            )

        self.run_task("正在只读查询安装状态…", action, success)

    def on_install_bootstrap(self) -> None:
        if not self._confirm_mutation(
            "安装登录后台引导器",
            (
                "将为当前 Windows 用户创建固定同名的交互式、"
                "隐藏、失败可重启任务；不会覆盖任何已有同名任务。"
            ),
        ):
            return

        def action() -> bool:
            if self.temporary_directory_factory is None:
                return install_bootstrap_task(
                    self.paths,
                    self.control_paths,
                    self.control_runner,
                )
            return install_bootstrap_task(
                self.paths,
                self.control_paths,
                self.control_runner,
                temporary_directory_factory=(
                    self.temporary_directory_factory
                ),
            )

        def success(changed: bool) -> None:
            if changed:
                self.bootstrap_install_status.configure(
                    text="● 登录后台引导器：已安装且合同匹配",
                    foreground="#167347",
                )
            self.footer.configure(
                text=(
                    "登录后台引导器已安装。"
                    if changed
                    else "登录后台引导器状态未改变。"
                )
            )

        self.run_task("正在安装登录后台引导器…", action, success)

    def on_remove_bootstrap(self) -> None:
        if not self._confirm_mutation(
            "回滚登录后台引导器",
            (
                "只会结束并删除合同、当前 SID、固定 EXE 和参数"
                "全部匹配的任务；未知同名任务不会被删除。"
            ),
        ):
            return

        def success(changed: bool) -> None:
            if changed:
                self.bootstrap_install_status.configure(
                    text="○ 登录后台引导器：未安装",
                    foreground="#6b7280",
                )
            self.footer.configure(
                text=(
                    "登录后台引导器已精确回滚。"
                    if changed
                    else "没有匹配的登录后台引导器。"
                )
            )

        self.run_task(
            "正在精确回滚登录后台引导器…",
            lambda: remove_bootstrap_task(
                self.paths,
                self.control_paths,
                self.control_runner,
            ),
            success,
        )

    def on_apply_tailscale(self) -> None:
        if not self._confirm_mutation(
            "配置 Tailscale Serve",
            (
                "只把 /reader-computer-voice/v1 映射到"
                " http://127.0.0.1:43128；检测到其它路径或后端"
                "时会拒绝覆盖。"
            ),
        ):
            return

        def success(changed: bool) -> None:
            self.tailscale_install_status.configure(
                text=(
                    "● Tailscale Serve："
                    "固定路径已指向本机直连端口"
                ),
                foreground="#167347",
            )
            self.footer.configure(
                text=(
                    "路径级 Tailscale Serve 已配置。"
                    if changed
                    else "路径级 Tailscale Serve 已是目标状态。"
                )
            )

        self.run_task(
            "正在配置路径级 Tailscale Serve…",
            lambda: apply_tailscale_serve(
                self.control_paths,
                self.control_runner,
            ),
            success,
        )

    def on_remove_tailscale(self) -> None:
        if not self._confirm_mutation(
            "回滚 Tailscale Serve",
            (
                "只关闭 /reader-computer-voice/v1 的固定映射；"
                "其它或混合配置会 fail closed。"
            ),
        ):
            return

        def success(changed: bool) -> None:
            if changed:
                self.tailscale_install_status.configure(
                    text="○ Tailscale Serve：未配置",
                    foreground="#6b7280",
                )
            self.footer.configure(
                text=(
                    "路径级 Tailscale Serve 已精确回滚。"
                    if changed
                    else "没有匹配的 Serve 映射。"
                )
            )

        self.run_task(
            "正在精确回滚路径级 Tailscale Serve…",
            lambda: remove_tailscale_serve(
                self.control_paths,
                self.control_runner,
            ),
            success,
        )


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        report = build_self_test_report(BridgePaths.discover())
        # PyInstaller's Windows --noconsole mode deliberately sets stdout and
        # stderr to None.  The package gate relies on this exit code, so the
        # self-test must remain headless instead of raising while printing.
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if sys.argv[1:] == ["--bootstrap"]:
        # The interactive current-user bootstrap owns the only keyboard
        # injection surface.  Do not start the native direct child until its
        # authenticated local pipe is already accepting requests.
        with WindowsShortcutBroker():
            return run_idle_bootstrap(
                BridgePaths.discover(),
                WindowsProcessRunner(),
            )
    if sys.argv[1:]:
        raise SystemExit("此源码入口不接受 Reader 提交的参数或命令。")
    root = tk.Tk()
    BridgeWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
