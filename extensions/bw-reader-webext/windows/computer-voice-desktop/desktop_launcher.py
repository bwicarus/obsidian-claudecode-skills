from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, ContextManager

from bridge_core import (
    BridgeError,
    BridgePaths,
    DIRECT_WSS_URL,
    DirectStatus,
    FIXED_APP_KIND,
    FIXED_LISTEN_HOST,
    FIXED_LISTEN_PORT,
    FIXED_OUTPUT_SCOPE,
    FIXED_SHORTCUT,
    Microphone,
    ProcessRunner,
    WindowsProcessRunner,
    build_self_test_report,
    disable_and_stop_direct_service,
    enumerate_microphones,
    load_direct_config,
    prepare_pairing,
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
APP_VERSION = "0.4.0-direct-supervisor-source"


class BridgeWindow:
    _ACTION_BUTTON_NAMES = (
        "enable_button",
        "start_button",
        "disable_button",
        "refresh_button",
        "generate_pair_button",
        "control_refresh_button",
        "bootstrap_install_button",
        "bootstrap_remove_button",
        "tailscale_apply_button",
        "tailscale_remove_button",
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
        microphone_provider: Callable[[], list[Microphone]] = (
            enumerate_microphones
        ),
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
        self.microphone_provider = microphone_provider
        self.microphones: list[Microphone] = []
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False

        root.title(APP_TITLE)
        root.geometry("700x980")
        root.minsize(640, 880)
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

        config_frame = self._section(outer, "本机配置")
        microphone_row = ttk.Frame(config_frame)
        microphone_row.pack(fill="x")
        ttk.Label(microphone_row, text="麦克风", width=10).pack(
            side="left"
        )
        self.microphone_combo = ttk.Combobox(
            microphone_row,
            state="readonly",
            width=53,
        )
        self.microphone_combo.pack(side="left", fill="x", expand=True)

        ttk.Label(
            config_frame,
            text=(
                f"只监听 {FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}；"
                f"输出范围固定为 {FIXED_OUTPUT_SCOPE}，无全系统回退。"
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

        pair_frame = self._section(outer, "Reader 一次性配对")
        ttk.Label(
            pair_frame,
            text=(
                "配对码在 Windows 生成，只把 SHA-256 摘要写入配置；"
                "明文不落盘。消费后只保留 Reader 的 ECDSA P-256 公钥和指纹。"
            ),
            foreground="#44546a",
            wraplength=590,
        ).pack(anchor="w")
        self.pair_code = ttk.Label(
            pair_frame,
            text="一次性配对码：尚未生成",
            style="Status.TLabel",
        )
        self.pair_code.pack(anchor="w", pady=(9, 0))
        self.generate_pair_button = ttk.Button(
            pair_frame,
            text="在 Windows 生成一次性配对码",
            command=self.on_generate_pairing,
        )
        self.generate_pair_button.pack(anchor="w", pady=(10, 0))

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
        # The old prototype referenced self.pair_button, which was never
        # created.  Every action therefore crashed in set_busy before reaching
        # its real operation.  Missing/optional controls are now ignored.
        for name in self._ACTION_BUTTON_NAMES:
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)
        if footer and getattr(self, "footer", None) is not None:
            self.footer.configure(text=footer)

    def selected_microphone(self) -> Microphone:
        index = self.microphone_combo.current()
        if index < 0 or index >= len(self.microphones):
            raise BridgeError("请先明确选择一个麦克风。")
        return self.microphones[index]

    def _refresh_microphones(self, selected_id: str = "") -> None:
        self.microphones = self.microphone_provider()
        self.microphone_combo.configure(
            values=[item.display_name for item in self.microphones]
        )
        index = next(
            (
                item_index
                for item_index, item in enumerate(self.microphones)
                if item.endpoint_id == selected_id
            ),
            -1,
        )
        if index >= 0:
            self.microphone_combo.current(index)
        elif self.microphones:
            self.microphone_combo.current(0)
        else:
            self.microphone_combo.set("")

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

    def render_pairing_config(
        self,
        config: dict[str, Any] | None,
    ) -> None:
        if config and config.get("pairedClientPublicKeySpki"):
            fingerprint = str(
                config.get("pairedClientFingerprintSha256", "")
            )
            self.pair_code.configure(
                text=(
                    "Reader 公钥已登记；指纹 "
                    + fingerprint[:12]
                    + "…"
                ),
                foreground="#167347",
            )
        elif config and config.get("pairingCodeHash"):
            self.pair_code.configure(
                text=(
                    "一次性配对摘要已登记；"
                    "明文只在生成后的当前显示周期可见"
                ),
                foreground="#9a6700",
            )
        else:
            self.pair_code.configure(
                text="一次性配对码：尚未生成",
                foreground="#6b7280",
            )

    def refresh_static(self) -> DirectStatus:
        config = load_direct_config(self.paths)
        selected_id = (
            str(config.get("microphoneEndpointId", ""))
            if config
            else ""
        )
        previous_selection = ""
        current_index = self.microphone_combo.current()
        if 0 <= current_index < len(self.microphones):
            previous_selection = self.microphones[current_index].endpoint_id
        self._refresh_microphones(selected_id or previous_selection)
        status = read_direct_status(self.paths, self.process_runner)
        self.render_status(status)
        self.render_pairing_config(config)
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
            microphone = self.selected_microphone()
        except BridgeError as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        if not self._confirm_mutation(
            "保存并启用本机配置",
            (
                "将写入所选麦克风、固定 Reader HTTPS 来源、"
                "127.0.0.1:43128 和 process-only 边界；"
                "不会启动服务或音频。"
            ),
        ):
            return

        def action() -> dict[str, Any]:
            active = self.microphone_provider()
            return save_enabled_config(
                self.paths,
                microphone,
                active_microphones=active,
            )

        def success(_: dict[str, Any]) -> None:
            self.refresh_static()
            self.footer.configure(
                text=(
                    "配置已启用；没有启动服务、采音、GPT 或快捷键。"
                )
            )

        self.run_task("正在保存本机直连配置…", action, success)

    def on_disable_config(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "将先原子写入 localOptIn=false，再只停止 PID 与 EXE "
            "路径均匹配的直连代理；尚未消费的配对码会清除，"
            "已登记公钥保留。继续吗？",
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
            self.pair_code.configure(text="一次性配对码：尚未生成")
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
            microphone = self.selected_microphone()
        except BridgeError as error:
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        if not self._confirm_mutation(
            "启用并启动空闲直连服务",
            (
                "先原子写入 localOptIn=true；若已安装且 ownership "
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
            active = self.microphone_provider()
            save_enabled_config(
                self.paths,
                microphone,
                active_microphones=active,
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

    def on_generate_pairing(self) -> None:
        config = load_direct_config(self.paths)
        replace = bool(
            config and config.get("pairedClientPublicKeySpki")
        )
        if replace:
            if not self._confirm_mutation(
                "重新配对 Reader",
                (
                    "会显式撤销旧 Reader 公钥和指纹，"
                    "再生成新的短期一次性配对码。"
                ),
            ):
                return
        elif not self._confirm_mutation(
            "生成一次性配对码",
            (
                "只把配对码 SHA-256 摘要和五分钟过期时间"
                "写入本机 direct config；明文不落盘。"
            ),
        ):
            return

        def success(material: Any) -> None:
            self.refresh_static()
            grouped = "-".join(
                (
                    material.display_code[:5],
                    material.display_code[5:],
                )
            )
            self.pair_code.configure(
                text=(
                    f"一次性配对码：{grouped}；"
                    f"有效至 {material.expires_at_utc}"
                ),
                foreground="#9a6700",
            )
            self.footer.configure(
                text=(
                    "明文配对码仅显示在本窗口；配置只保存 43 字符摘要。"
                )
            )

        self.run_task(
            "正在生成短期一次性配对码…",
            lambda: prepare_pairing(
                self.paths,
                replace_existing=replace,
            ),
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
