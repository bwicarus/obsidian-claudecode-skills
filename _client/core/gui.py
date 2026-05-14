"""主窗口 — customtkinter，Tab 分组布局。

Tabs:
  基础     server_url / api_token / 测试连接
  上传     dashboard_dir / 手动上传按钮
  AI       后端选择 + 该后端 settings + 测试 AI
  Anki     anki_path / ankiconnect_url / ping AnkiConnect / 启动 Anki
  vault    vault_path / register_script / 运行登记新笔记
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

from api_client import ApiClient  # type: ignore
from uploader import upload_dashboard, upload_dataset  # type: ignore
from ai_backends import (  # type: ignore
    list_backends, make_backend,
    backend_default_settings, backend_setting_fields,
)
from anki import AnkiClient  # type: ignore
from runner import run_script, open_program  # type: ignore
from tray import TrayIcon  # type: ignore
from watcher import VaultWatcher  # type: ignore
from floating_window import FloatingWindow  # type: ignore
import hotkey as hotkey_mod  # type: ignore
import startup as startup_mod  # type: ignore
from scheduler import DailyScheduler  # type: ignore
from cmd_server_thread import CmdServer, load_or_create_key  # type: ignore
from auth import do_device_link_flow  # type: ignore
from wizard import OnboardingWizard  # type: ignore


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class MainWindow:
    def __init__(self, cfg_path: Path, cfg: dict, core_version: str):
        self.cfg_path = cfg_path
        self.cfg = cfg
        self.core_version = core_version

        # 兜底默认值
        self.cfg.setdefault("server_url", "https://bwicarus.space")
        self.cfg.setdefault("ai_backend", "claude_cli")
        self.cfg.setdefault("ai", {})
        self.cfg.setdefault("anki", {"connect_url": "http://localhost:8765"})

        # 首次启动 / .exe 双击：所有空字段自动探测填默认（已填的不动）
        self._autofill_defaults()

        self.root = ctk.CTk()
        self.root.title(f"bwicarus-client  ·  core {core_version}")
        self.root.geometry("760x640")
        self.root.minsize(640, 540)

        self._entries: dict[str, ctk.CTkEntry] = {}
        self._ai_setting_entries: dict[str, ctk.CTkEntry] = {}
        self._ai_settings_frame: ctk.CTkFrame | None = None

        self._watcher: VaultWatcher | None = None
        self._tray: TrayIcon | None = None
        self._auto_watch_var: ctk.BooleanVar | None = None
        self._watcher_status_lbl: ctk.CTkLabel | None = None
        self._floating: FloatingWindow | None = None
        self._show_floating_on_start_var: ctk.BooleanVar | None = None
        self._qa_remote_access_var: ctk.BooleanVar | None = None
        self._qa_remote_daemon_var: ctk.BooleanVar | None = None
        self._qa_remote_daemon_chk: ctk.CTkCheckBox | None = None
        self._startup_var: ctk.BooleanVar | None = None
        self._sched_enabled_var: ctk.BooleanVar | None = None
        self._sched_wake_anki_var: ctk.BooleanVar | None = None
        self._sched_status_lbl: ctk.CTkLabel | None = None
        self._scheduler: DailyScheduler | None = None
        self._floating_pos_var: ctk.StringVar | None = None
        self._floating_click_through_var: ctk.BooleanVar | None = None
        self._hotkey_record_btn: ctk.CTkButton | None = None
        self._register_running = False  # 全局互斥标志：登记笔记正在跑
        self._cmd_server: CmdServer | None = None
        self._cmd_server_enabled_var: ctk.BooleanVar | None = None
        self._cmd_server_port_var: ctk.StringVar | None = None
        self._cmd_server_status_lbl: ctk.CTkLabel | None = None
        self._auto_upload_var: ctk.BooleanVar | None = None

        # 关窗拦截：默认最小化到托盘
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self._build_ui()
        # 首次启动：未完成配置向导 → 弹 wizard；
        # 否则已配过、只是 token 不在 → 弹一键登录提示。
        if not self.cfg.get("onboarding_completed"):
            self.root.after(300, self._launch_wizard)
        elif not (self.cfg.get("api_token") or "").strip():
            self.root.after(300, self._login_prompt)
        self._init_floating()
        self._init_tray()
        self._init_hotkey()
        # 按 config.auto_watch 决定是否自动启动监听
        if self.cfg.get("auto_watch"):
            self.root.after(500, self._start_watcher)
        # 按 config.show_floating_on_start 决定是否启动时显示悬浮窗
        if self.cfg.get("show_floating_on_start") and self._floating:
            self.root.after(800, self._floating.show)
        # scheduled_register 启用则启动 scheduler
        if (self.cfg.get("scheduled_register") or {}).get("enabled"):
            self.root.after(1500, self._refresh_scheduler)
        # cmd_server: 监听 0.0.0.0:9090，接收 iPad 快捷指令等远程触发
        if (self.cfg.get("cmd_server") or {}).get("enabled"):
            self.root.after(2000, self._start_cmd_server)
            # qa_browser daemon：常驻 127.0.0.1:9091，由 cmd_server 反代访问
            self.root.after(2500, self._start_qa_daemon)

    # ── 构建 ───────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        # ── 底部日志（pack first so tabs 上方占剩余空间）──────────────
        # 顶层日志容器：永远在窗口最底部
        self._log_container = ctk.CTkFrame(outer)
        # 显隐切换由 _toggle_log_visibility 改 pack 顺序
        self._log_container.pack(side="bottom", fill="x", pady=(8, 0))

        log_header = ctk.CTkFrame(self._log_container, fg_color="transparent")
        log_header.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(
            log_header, text="日志", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left")
        self._log_visible = True
        self._log_toggle_btn = ctk.CTkButton(
            log_header, text="隐藏 ▼", width=70, fg_color="gray30",
            command=self._toggle_log_visibility,
        )
        self._log_toggle_btn.pack(side="right")
        ctk.CTkButton(
            log_header, text="清空", width=60, fg_color="gray30",
            command=self._clear_log,
        ).pack(side="right", padx=(0, 6))

        # 日志正文（textbox 自带 scrollbar）
        self._log_body = ctk.CTkFrame(self._log_container, fg_color="transparent")
        self._log_body.pack(fill="x", padx=10, pady=(4, 8))
        self.log_text = ctk.CTkTextbox(
            self._log_body, height=160,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.log_text.pack(fill="x", expand=False)
        self.log_text.configure(state="disabled")

        # ── 顶部工具行（保存配置）──────────────────────────────────
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkButton(btn_row, text="保存配置", command=self._save_cfg, width=100).pack(side="left")
        ctk.CTkLabel(
            btn_row, text=f"配置文件 · {self.cfg_path}",
            font=ctk.CTkFont(size=11), text_color="gray60",
        ).pack(side="left", padx=10)

        # ── Tab 区（占据日志/工具行剩余的所有空间）─────────────────────
        tabs = ctk.CTkTabview(outer)
        tabs.pack(side="top", fill="both", expand=True)

        for name in ["基础", "AI", "Anki", "笔记登记", "任务监视", "截图问答", "高级"]:
            tabs.add(name)

        # 每个 tab 内部用 ScrollableFrame 包裹，tab 内容多了能自己滚动
        self._build_tab_basic(self._wrap_scroll(tabs.tab("基础")))
        self._build_tab_ai(self._wrap_scroll(tabs.tab("AI")))
        self._build_tab_anki(self._wrap_scroll(tabs.tab("Anki")))
        self._build_tab_vault(self._wrap_scroll(tabs.tab("笔记登记")))
        self._build_tab_tasks(self._wrap_scroll(tabs.tab("任务监视")))
        self._build_tab_qa(self._wrap_scroll(tabs.tab("截图问答")))
        self._build_tab_advanced(self._wrap_scroll(tabs.tab("高级")))

    def _wrap_scroll(self, tab):
        """给一个 tab 包一层 ScrollableFrame，返回 inner frame 给 _build_tab_xxx 用。"""
        inner = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        inner.pack(fill="both", expand=True)
        return inner

    def _toggle_log_visibility(self) -> None:
        if self._log_visible:
            self._log_body.pack_forget()
            self._log_toggle_btn.configure(text="显示 ▲")
        else:
            self._log_body.pack(fill="x", padx=10, pady=(4, 8))
            self._log_toggle_btn.configure(text="隐藏 ▼")
        self._log_visible = not self._log_visible

    def _clear_log(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

        self._log(f"core {self.core_version} 已加载")

    # ── 各 Tab ───────────────────────────────────────────
    def _build_tab_basic(self, tab) -> None:
        self._add_entry(tab, "server_url", "服务端地址")
        self._add_entry(tab, "api_token", "API token", show="•",
                        right_btn=[("粘贴", self._paste_token),
                                   ("复制", self._copy_token)])
        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.pack(fill="x", padx=4, pady=(10, 4))
        ctk.CTkButton(btn_row, text="测试连接", command=self._test_conn,
                      width=110).pack(side="left", padx=(140, 6))
        ctk.CTkButton(btn_row, text="一键登录（浏览器）", command=self._start_device_link,
                      width=160, fg_color="#6366f1", hover_color="#4f46e5"
                      ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row, text="跑配置向导", command=self._launch_wizard,
                      width=130, fg_color="gray30").pack(side="left")

        # 数据目录（只读显示 + 复制 + 在资源管理器打开）
        sep_d = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep_d.pack(fill="x", padx=4, pady=(14, 8))
        from paths import app_dir as _app_dir  # type: ignore
        data_dir_str = str(_app_dir())
        data_row = ctk.CTkFrame(tab, fg_color="transparent")
        data_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(data_row, text="数据目录", width=140, anchor="w").pack(side="left")
        ctk.CTkLabel(
            data_row, text=data_dir_str, font=ctk.CTkFont(family="Consolas", size=11),
            text_color="gray80", anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            data_row, text="打开", width=60, fg_color="gray30",
            command=lambda d=data_dir_str: self._open_in_explorer(d),
        ).pack(side="right")
        ctk.CTkLabel(
            tab,
            text="客户端配置、core 包、cmd_server 密钥都在这里。"
                 "首次启动选过的位置；要换位置：手动把整个目录复制到新位置后，"
                 "改 %APPDATA%\\bwicarus-client\\datadir.txt 指向新路径，再重启客户端。",
            text_color="gray50", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 6))

        # 开机自启
        sep = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep.pack(fill="x", padx=4, pady=(14, 8))
        startup_row = ctk.CTkFrame(tab, fg_color="transparent")
        startup_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(startup_row, text="开机自启", width=140, anchor="w").pack(side="left")
        self._startup_var = ctk.BooleanVar(value=startup_mod.is_enabled())
        ctk.CTkCheckBox(
            startup_row, text="登录 Windows 时自动启动客户端（写入 HKCU\\...\\Run）",
            variable=self._startup_var, command=self._on_startup_toggle,
        ).pack(side="left")

        # 防睡眠 / 合盖关屏：由独立的 autoscreen 项目（C:\autoscreen 的托盘图标）统一管理


    def _build_tab_ai(self, tab) -> None:
        # 后端下拉
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(8, 4))
        ctk.CTkLabel(row, text="AI 后端", width=140, anchor="w").pack(side="left")
        self._ai_backend_var = ctk.StringVar(value=self.cfg.get("ai_backend", "claude_cli"))
        self._ai_dropdown = ctk.CTkOptionMenu(
            row, values=list_backends(), variable=self._ai_backend_var,
            command=self._on_ai_backend_change,
        )
        self._ai_dropdown.pack(side="left", padx=(8, 0))

        # settings 容器（动态重建）
        self._ai_settings_frame = ctk.CTkFrame(tab)
        self._ai_settings_frame.pack(fill="x", padx=4, pady=(8, 4))

        self._render_ai_settings(self._ai_backend_var.get())
        self._add_button(tab, "测试当前 AI 后端", self._test_ai)

    def _build_tab_anki(self, tab) -> None:
        anki_cfg = self.cfg.get("anki", {})
        self._entries["anki.exe_path"] = self._add_entry(
            tab, "anki.exe_path", "Anki 可执行路径",
            initial=anki_cfg.get("exe_path", ""),
            right_btn=("…", lambda: self._pick_file("anki.exe_path", filetypes=[("可执行", "*.exe")])),
        )
        self._entries["anki.connect_url"] = self._add_entry(
            tab, "anki.connect_url", "AnkiConnect URL",
            initial=anki_cfg.get("connect_url", "http://localhost:8765"),
        )
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(8, 4))
        ctk.CTkButton(row, text="ping AnkiConnect", command=self._ping_anki, width=160).pack(side="left", padx=(140, 8))
        ctk.CTkButton(row, text="启动 Anki", command=self._launch_anki, width=120, fg_color="gray30").pack(side="left")

        self._anki_auto_restart_var = ctk.BooleanVar(
            value=bool(anki_cfg.get("auto_restart", False)))
        restart_row = ctk.CTkFrame(tab, fg_color="transparent")
        restart_row.pack(fill="x", padx=4, pady=(4, 0))
        ctk.CTkLabel(restart_row, text="", width=140).pack(side="left")
        ctk.CTkCheckBox(
            restart_row,
            text="AnkiConnect 不可达时自动杀进程并重启 Anki（防僵尸进程卡死调度）",
            variable=self._anki_auto_restart_var,
        ).pack(side="left", anchor="w")

    def _build_tab_qa(self, tab) -> None:
        ctk.CTkLabel(
            tab,
            text="本机截图问答：按钮 / 全局热键触发 Win+Shift+S 系统截图工具，截图后开浏览器到本机 HTTP server，"
                 "AI 调用走 AI Tab 当前选定的 backend。所有数据（图片/历史/笔记写回）全在本机。",
            text_color="gray60", font=ctk.CTkFont(size=11),
            wraplength=620, justify="left",
        ).pack(anchor="w", padx=4, pady=(8, 12))

        # 全局热键
        hk_row = ctk.CTkFrame(tab, fg_color="transparent")
        hk_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(hk_row, text="全局热键", width=140, anchor="w").pack(side="left")
        self._entries["qa_hotkey"] = ctk.CTkEntry(hk_row, width=180)
        self._entries["qa_hotkey"].insert(0, str(self.cfg.get("qa_hotkey") or "ctrl+shift+q"))
        self._entries["qa_hotkey"].pack(side="left", padx=(8, 4))
        self._hotkey_record_btn = ctk.CTkButton(
            hk_row, text="🎙 录制", width=84, fg_color="gray30",
            command=self._record_hotkey,
        )
        self._hotkey_record_btn.pack(side="left", padx=(4, 4))
        ctk.CTkButton(hk_row, text="应用", width=64, fg_color="gray30",
                      command=self._rebind_hotkey).pack(side="left", padx=(4, 4))
        self._hotkey_status = ctk.CTkLabel(hk_row, text="", text_color="gray60",
                                           font=ctk.CTkFont(size=11))
        self._hotkey_status.pack(side="left", padx=(8, 0))

        # vault 路径
        self._add_entry(
            tab, "qa_vault_path", "Obsidian vault 目录",
            initial=self.cfg.get("qa_vault_path") or self.cfg.get("vault_path") or self._guess_vault(),
            right_btn=("…", lambda: self._pick_dir("qa_vault_path")),
        )
        # 习题 / 错题路径（支持 vault 内子目录名 或 绝对路径）
        self._add_entry(
            tab, "qa_exercises_subdir", "习题目录",
            initial=self.cfg.get("qa_exercises_subdir") or "习题",
            right_btn=("…", lambda: self._pick_dir("qa_exercises_subdir")),
        )
        self._add_entry(
            tab, "qa_wrong_subdir", "错题目录",
            initial=self.cfg.get("qa_wrong_subdir") or "错题",
            right_btn=("…", lambda: self._pick_dir("qa_wrong_subdir")),
        )
        ctk.CTkLabel(
            tab,
            text="留空 = vault/习题；可填子目录名（vault 内）或绝对路径（vault 外，如 D:\\我的笔记\\习题）",
            text_color="gray50", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 4))

        # 知识索引 / anki records 目录在「高级」tab 统一管理（默认从 project_root 派生）

        # 浏览器路径（留空走系统默认）
        self._add_entry(
            tab, "qa_browser_path", "浏览器可执行路径",
            initial=self.cfg.get("qa_browser_path", ""),
            right_btn=("…", lambda: self._pick_file("qa_browser_path", filetypes=[("可执行", "*.exe")])),
        )
        ctk.CTkLabel(
            tab, text="留空 = 找系统 Chrome 走 --app 模式 / 没 Chrome 用系统默认浏览器；"
                      "填了路径就用该浏览器普通打开",
            text_color="gray50", font=ctk.CTkFont(size=10), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 8))

        # 远程访问
        remote_row = ctk.CTkFrame(tab, fg_color="transparent")
        remote_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(remote_row, text="远程访问", width=140, anchor="w").pack(side="left")
        self._qa_remote_access_var = ctk.BooleanVar(value=bool(self.cfg.get("qa_remote_access", False)))
        ctk.CTkCheckBox(
            remote_row, text="允许局域网其他设备访问对话窗（HTTP server 监听 0.0.0.0）",
            variable=self._qa_remote_access_var,
            command=self._sync_qa_remote_subopts,
        ).pack(side="left")

        # 子选项：iPad 常驻远程问答（依赖父开关）
        daemon_row = ctk.CTkFrame(tab, fg_color="transparent")
        daemon_row.pack(fill="x", padx=4, pady=(0, 2))
        ctk.CTkLabel(daemon_row, text="", width=140).pack(side="left")  # 占位对齐
        self._qa_remote_daemon_var = ctk.BooleanVar(
            value=bool(self.cfg.get("qa_remote_daemon", False))
        )
        self._qa_remote_daemon_chk = ctk.CTkCheckBox(
            daemon_row,
            text="└─ iPad 远程截图问答（常驻 daemon :9091，cmd_server :9090 /qa 注入截图）",
            variable=self._qa_remote_daemon_var,
        )
        self._qa_remote_daemon_chk.pack(side="left")

        ctk.CTkLabel(
            tab,
            text="远程开启后启动会在终端打印「http://<本机内网IP>:<port>」让你在 iPad / 手机上打开同一会话。"
                 "公网暴露请走 nginx + 鉴权，不要直接监听 0.0.0.0 公网网卡。",
            text_color="gray50", font=ctk.CTkFont(size=10), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 12))

        self._sync_qa_remote_subopts()

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=4)
        ctk.CTkButton(row, text="开始截图问答", command=self._launch_qa_browser).pack(side="left", padx=(140, 0))
        ctk.CTkButton(row, text="生成桌面快捷方式", command=self._make_qa_shortcut, width=160,
                      fg_color="gray30").pack(side="left", padx=(8, 0))

    def _make_qa_shortcut(self) -> None:
        """在 .exe 同目录 + 桌面各创建一个 截图问答.lnk，目标 = bwicarus-client.exe --qa"""
        import sys, subprocess
        from pathlib import Path

        if not getattr(sys, "frozen", False):
            self._log("✗ 源码模式没有 .exe，快捷方式只能在打包后的 .exe 模式下创建")
            return

        target_exe = sys.executable
        exe_dir = Path(target_exe).parent
        desktop = Path.home() / "Desktop"
        candidates = []
        if exe_dir.exists():
            candidates.append(exe_dir / "截图问答.lnk")
        if desktop.exists():
            candidates.append(desktop / "截图问答.lnk")

        if not candidates:
            self._log("✗ 没找到合适的位置（exe 目录 / 桌面）")
            return

        ok_count = 0
        for lnk in candidates:
            ps = (
                "$ws = New-Object -ComObject WScript.Shell; "
                f"$sc = $ws.CreateShortcut('{lnk}'); "
                f"$sc.TargetPath = '{target_exe}'; "
                "$sc.Arguments = '--qa'; "
                f"$sc.WorkingDirectory = '{exe_dir}'; "
                f"$sc.IconLocation = '{target_exe},0'; "
                "$sc.Description = 'bwicarus-client 截图问答'; "
                "$sc.Save();"
            )
            try:
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                )
                if r.returncode == 0 and lnk.exists():
                    self._log(f"✓ {lnk}")
                    ok_count += 1
                else:
                    self._log(f"✗ 创建失败 {lnk}：{(r.stderr or r.stdout).strip()[:200]}")
            except Exception as e:
                self._log(f"✗ 创建失败 {lnk}：{e}")
        if ok_count:
            self._log(f"双击快捷方式可直接触发截图问答（不开主 GUI）")

    def _launch_qa_browser(self) -> None:
        self._save_cfg()
        self._log("启动截图问答 — 接下来用系统截图工具截一块区域...")

        def task():
            try:
                from qa_browser import launch as launch_qa  # type: ignore
                launch_qa(get_cfg=self._gather_cfg)
                self._log("截图问答会话结束")
            except Exception as e:
                import traceback
                self._log(f"✗ 截图问答失败：{e}")
                self._log(traceback.format_exc())

        self._run_async(task)

    # ── 高级 tab：所有派生路径集中管理 ─────────────────────
    def _build_tab_advanced(self, tab) -> None:
        ctk.CTkLabel(
            tab,
            text="所有路径都从「主项目根目录」自动派生，正常情况下你只需要确认上面这一个。"
                 "下方的派生路径会跟着重新计算；如果你的某个目录在非默认位置，可以单独覆盖。",
            text_color="gray60", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=4, pady=(8, 6))

        self._add_entry(
            tab, "project_root", "主项目根目录",
            right_btn=[("…", lambda: self._pick_dir("project_root")),
                       ("重置派生", self._rederive_paths)],
        )

        sep = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep.pack(fill="x", padx=4, pady=(8, 8))
        ctk.CTkLabel(
            tab, text="派生路径（一般无需手动改）",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="gray70",
        ).pack(anchor="w", padx=4, pady=(0, 4))

        self._add_entry(tab, "scripts_dir", "scripts 目录",
                        right_btn=("…", lambda: self._pick_dir("scripts_dir")))
        self._add_entry(tab, "dashboard_dir", "dashboard 目录",
                        right_btn=("…", lambda: self._pick_dir("dashboard_dir")))
        self._add_entry(tab, "history_dir", "history 目录",
                        right_btn=("…", lambda: self._pick_dir("history_dir")))
        self._add_entry(tab, "register_script", "register_notes.py",
                        right_btn=("…", lambda: self._pick_file(
                            "register_script", filetypes=[("Python", "*.py")])))
        self._add_entry(tab, "task_state_file", "active_tasks.json",
                        right_btn=("…", lambda: self._pick_file(
                            "task_state_file", filetypes=[("JSON", "*.json")])))
        self._add_entry(tab, "qa_index_dir", "知识索引目录",
                        right_btn=("…", lambda: self._pick_dir("qa_index_dir")))
        self._add_entry(tab, "qa_anki_records_dir", "Anki records 目录",
                        right_btn=("…", lambda: self._pick_dir("qa_anki_records_dir")))

        sep2 = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep2.pack(fill="x", padx=4, pady=(8, 8))
        ctk.CTkLabel(
            tab, text="系统级路径",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="gray70",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self._add_entry(tab, "python_path", "python.exe",
                        right_btn=("…", lambda: self._pick_file(
                            "python_path", filetypes=[("可执行", "*.exe")])))

    def _rederive_paths(self) -> None:
        """点「重置派生」：用当前 project_root 重新算派生路径并刷新 entry。"""
        from paths import derive_paths  # type: ignore
        cfg = self._gather_cfg()
        derived = derive_paths(cfg.get("project_root"))
        for k, v in derived.items():
            self.cfg[k] = v
            entry = self._entries.get(k)
            if entry is not None:
                entry.delete(0, "end")
                entry.insert(0, v)
        self._log(f"✓ 已基于 {cfg.get('project_root')} 重新派生 {len(derived)} 条路径")

    def _build_tab_tasks(self, tab) -> None:
        ctk.CTkLabel(
            tab,
            text="读取主项目 task_tracker 写的 active_tasks.json（路径在「高级」tab 自动派生），"
                 "半透明置顶悬浮窗显示进度。",
            text_color="gray60", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=4, pady=(8, 4))

        opt_row = ctk.CTkFrame(tab, fg_color="transparent")
        opt_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(opt_row, text="启动行为", width=140, anchor="w").pack(side="left")
        self._show_floating_on_start_var = ctk.BooleanVar(
            value=bool(self.cfg.get("show_floating_on_start", False))
        )
        ctk.CTkCheckBox(
            opt_row, text="启动 client 时自动显示悬浮窗",
            variable=self._show_floating_on_start_var,
        ).pack(side="left")

        # 位置预设
        floating_cfg = self.cfg.get("floating") or {}
        pos_row = ctk.CTkFrame(tab, fg_color="transparent")
        pos_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(pos_row, text="悬浮窗位置", width=140, anchor="w").pack(side="left")
        self._floating_pos_var = ctk.StringVar(
            value=str(floating_cfg.get("position") or "auto"))
        ctk.CTkOptionMenu(
            pos_row,
            values=["auto", "top-left", "top-right", "bottom-left", "bottom-right", "custom"],
            variable=self._floating_pos_var,
            command=self._on_floating_pos_change,
            width=140,
        ).pack(side="left", padx=(8, 4))
        ctk.CTkLabel(
            pos_row,
            text="auto = 右下，custom = 拖动后自动记忆",
            text_color="gray50", font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        # 鼠标穿透
        click_row = ctk.CTkFrame(tab, fg_color="transparent")
        click_row.pack(fill="x", padx=4, pady=4)
        self._floating_click_through_var = ctk.BooleanVar(
            value=bool(floating_cfg.get("click_through", False)))
        ctk.CTkCheckBox(
            click_row, text="鼠标穿透（点击不阻挡下层窗口；勾选后无法拖动）",
            variable=self._floating_click_through_var,
            command=self._on_floating_click_through_change,
        ).pack(side="left", padx=(140, 0))

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.pack(fill="x", padx=4, pady=(10, 4))
        ctk.CTkButton(btn_row, text="显示悬浮窗", command=self._floating_show, width=120).pack(side="left", padx=(140, 8))
        ctk.CTkButton(btn_row, text="隐藏悬浮窗", command=self._floating_hide, width=120, fg_color="gray30").pack(side="left")

        # ── 远程触发服务（cmd_server） ────────────────────────────
        sep = ctk.CTkFrame(tab, fg_color="gray25", height=1)
        sep.pack(fill="x", padx=4, pady=(16, 8))
        ctk.CTkLabel(
            tab,
            text="远程触发（cmd_server）— iPad 快捷指令 / Tailscale 通过 HTTP 触发本机命令",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=4, pady=(0, 4))

        cs_cfg = self.cfg.get("cmd_server") or {}
        cs_row = ctk.CTkFrame(tab, fg_color="transparent")
        cs_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(cs_row, text="启用", width=140, anchor="w").pack(side="left")
        self._cmd_server_enabled_var = ctk.BooleanVar(value=bool(cs_cfg.get("enabled", False)))
        ctk.CTkCheckBox(
            cs_row, text="监听 0.0.0.0 接收远程命令（POST /run/newnote、/run/upload-website）",
            variable=self._cmd_server_enabled_var,
            command=self._on_cmd_server_toggle,
        ).pack(side="left")

        port_row = ctk.CTkFrame(tab, fg_color="transparent")
        port_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(port_row, text="端口", width=140, anchor="w").pack(side="left")
        self._cmd_server_port_var = ctk.StringVar(value=str(cs_cfg.get("port", 9090)))
        ctk.CTkEntry(port_row, textvariable=self._cmd_server_port_var, width=100).pack(side="left")
        self._cmd_server_status_lbl = ctk.CTkLabel(
            port_row, text="(未启动)", text_color="gray60", font=ctk.CTkFont(size=11))
        self._cmd_server_status_lbl.pack(side="left", padx=10)

        key_row = ctk.CTkFrame(tab, fg_color="transparent")
        key_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(key_row, text="本机密钥", width=140, anchor="w").pack(side="left")
        # 文件位置已统一到 %LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt（自动管理，用户无需关心）
        try:
            api_key = load_or_create_key()
        except Exception:
            api_key = "(读取失败)"
        ctk.CTkLabel(
            key_row, text=api_key, font=ctk.CTkFont(family="Consolas", size=11),
            text_color="gray80",
        ).pack(side="left")
        ctk.CTkButton(
            key_row, text="复制", width=60,
            command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(api_key),
                             self.root.update(),
                             self._log("本机密钥已复制到剪贴板")),
        ).pack(side="left", padx=8)

        ctk.CTkLabel(
            tab,
            text="⚠ 这是本机 cmd_server 的密钥（hex），跟 bwicarus.space 上 profile 页的 API token 不是同一个。\n"
                 "iPad 快捷指令的 key= 应该填上面这个本机密钥。\n"
                 "URL 模板：POST http://<Tailscale地址>:<端口>/run/newnote?key=<本机密钥>（不要带尖括号）",
            text_color="gray50", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 4))

    def _build_tab_vault(self, tab) -> None:
        self._add_entry(tab, "vault_path", "Obsidian vault 目录",
                        right_btn=("…", lambda: self._pick_dir("vault_path")))
        ctk.CTkLabel(
            tab,
            text="「刷新并上传网页」依次执行：anki_status → review_priority → export_dashboard → "
                 "上传 dashboard → export_history → 上传 history。所有派生路径自动从「高级」tab 的"
                 "「主项目根目录」推导，正常无需手动指定。",
            text_color="gray50", font=ctk.CTkFont(size=11), wraplength=620, justify="left",
        ).pack(anchor="w", padx=144, pady=(0, 6))
        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.pack(fill="x", padx=4, pady=(4, 4))
        ctk.CTkButton(btn_row, text="立即运行登记新笔记", command=self._run_register,
                      width=180).pack(side="left", padx=(140, 8))
        ctk.CTkButton(btn_row, text="刷新并上传网页", command=self._upload_website,
                      width=160).pack(side="left")

        auto_row = ctk.CTkFrame(tab, fg_color="transparent")
        auto_row.pack(fill="x", padx=4, pady=(2, 4))
        self._auto_upload_var = ctk.BooleanVar(
            value=bool(self.cfg.get("auto_upload_after_register", False)))
        ctk.CTkCheckBox(
            auto_row,
            text="登记完成后自动「刷新并上传网页」（默认关；开后会跑 anki_status / review_priority / export 等)",
            variable=self._auto_upload_var,
        ).pack(side="left", padx=(140, 0))

        # 每日定时
        sep0 = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep0.pack(fill="x", padx=4, pady=(14, 8))
        sched_cfg = self.cfg.get("scheduled_register") or {}
        sched_row = ctk.CTkFrame(tab, fg_color="transparent")
        sched_row.pack(fill="x", padx=4, pady=4)
        self._sched_enabled_var = ctk.BooleanVar(value=bool(sched_cfg.get("enabled", False)))
        ctk.CTkCheckBox(
            sched_row, text="每日定时自动跑完整任务（register + 必复习计算 + 仪表板）",
            variable=self._sched_enabled_var, command=self._on_sched_toggle,
        ).pack(side="left")

        time_row = ctk.CTkFrame(tab, fg_color="transparent")
        time_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(time_row, text="触发时间 (HH:MM)", width=140, anchor="w").pack(side="left")
        self._entries["scheduled_register.time"] = ctk.CTkEntry(time_row, width=80)
        self._entries["scheduled_register.time"].insert(
            0, str(sched_cfg.get("time") or "04:00"))
        self._entries["scheduled_register.time"].pack(side="left", padx=(8, 4))
        self._sched_wake_anki_var = ctk.BooleanVar(
            value=bool(sched_cfg.get("wake_anki", True)))
        ctk.CTkCheckBox(
            time_row, text="触发前启动 Anki（让 AnkiConnect 可用）",
            variable=self._sched_wake_anki_var,
        ).pack(side="left", padx=(12, 0))

        upload_after_row = ctk.CTkFrame(tab, fg_color="transparent")
        upload_after_row.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(upload_after_row, text="", width=140).pack(side="left")
        self._sched_upload_after_var = ctk.BooleanVar(
            value=bool(sched_cfg.get("upload_after", False)))
        ctk.CTkCheckBox(
            upload_after_row,
            text="完成后上传网页（dashboard + history 推到服务器）",
            variable=self._sched_upload_after_var,
        ).pack(side="left", padx=(8, 0))

        manual_row = ctk.CTkFrame(tab, fg_color="transparent")
        manual_row.pack(fill="x", padx=4, pady=(4, 4))
        ctk.CTkLabel(manual_row, text="", width=140).pack(side="left")
        ctk.CTkButton(
            manual_row, text="立即跑完整定时任务（含必复习计算）",
            command=lambda: self._run_full_daily(manual=True),
            width=260,
        ).pack(side="left", padx=(8, 0))

        status_row = ctk.CTkFrame(tab, fg_color="transparent")
        status_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(status_row, text="调度器状态", width=140, anchor="w").pack(side="left")
        self._sched_status_lbl = ctk.CTkLabel(
            status_row, text="未启动", text_color="gray60", anchor="w")
        self._sched_status_lbl.pack(side="left")

        # 自动监听开关
        sep = ctk.CTkFrame(tab, height=1, fg_color="gray25")
        sep.pack(fill="x", padx=4, pady=(14, 8))

        watch_row = ctk.CTkFrame(tab, fg_color="transparent")
        watch_row.pack(fill="x", padx=4, pady=4)
        self._auto_watch_var = ctk.BooleanVar(value=bool(self.cfg.get("auto_watch", False)))
        ctk.CTkCheckBox(
            watch_row, text="启用自动监听 (默认关闭，建议手动同步为主)",
            variable=self._auto_watch_var, command=self._on_auto_watch_toggle,
        ).pack(side="left")

        # 防抖 / 冷却参数（默认 90s / 600s）
        param_row = ctk.CTkFrame(tab, fg_color="transparent")
        param_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(param_row, text="防抖 / 冷却 (秒)", width=140, anchor="w").pack(side="left")
        self._entries["watch_debounce_sec"] = ctk.CTkEntry(param_row, width=80)
        self._entries["watch_debounce_sec"].insert(0, str(self.cfg.get("watch_debounce_sec", 90)))
        self._entries["watch_debounce_sec"].pack(side="left", padx=(8, 4))
        ctk.CTkLabel(param_row, text="·", text_color="gray50").pack(side="left", padx=4)
        self._entries["watch_cooldown_sec"] = ctk.CTkEntry(param_row, width=80)
        self._entries["watch_cooldown_sec"].insert(0, str(self.cfg.get("watch_cooldown_sec", 600)))
        self._entries["watch_cooldown_sec"].pack(side="left", padx=(4, 4))
        ctk.CTkLabel(
            param_row,
            text="（变化后安静 防抖秒 才触发；触发后 冷却秒 内跳过）",
            text_color="gray50", font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        status_row = ctk.CTkFrame(tab, fg_color="transparent")
        status_row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(status_row, text="监听状态", width=140, anchor="w").pack(side="left")
        self._watcher_status_lbl = ctk.CTkLabel(status_row, text="未启动", text_color="gray60", anchor="w")
        self._watcher_status_lbl.pack(side="left")

    # ── 通用控件辅助 ─────────────────────────────────────
    def _add_entry(self, parent, key: str, label: str, *, show=None, initial=None,
                   right_btn=None) -> ctk.CTkEntry:
        """right_btn: 单个 (text, cmd) 或多个 [(text, cmd), ...]，靠右排列。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(row, text=label, width=140, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, show=show)
        v = initial if initial is not None else self.cfg.get(key, "")
        entry.insert(0, str(v))
        entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        if right_btn:
            buttons = right_btn if isinstance(right_btn, list) else [right_btn]
            # 末位按钮先 pack 到最右，列表中越靠后越偏右
            for text, cmd in buttons:
                ctk.CTkButton(row, text=text, width=64 if text != "…" else 34,
                              command=cmd, fg_color="gray30").pack(side="right", padx=(4, 0))
        self._entries[key] = entry
        return entry

    def _add_button(self, parent, text: str, cmd) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(10, 4))
        ctk.CTkButton(row, text=text, command=cmd).pack(side="left", padx=(140, 0))

    def _render_ai_settings(self, backend_name: str) -> None:
        # 清空旧 widget
        assert self._ai_settings_frame is not None
        for child in self._ai_settings_frame.winfo_children():
            child.destroy()
        self._ai_setting_entries.clear()

        ai_all = self.cfg.get("ai", {}) or {}
        cur_settings = ai_all.get(backend_name) or backend_default_settings(backend_name)

        for key, label, secret in backend_setting_fields(backend_name):
            row = ctk.CTkFrame(self._ai_settings_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=4)
            ctk.CTkLabel(row, text=label, width=130, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, show="•" if secret else None)
            entry.insert(0, str(cur_settings.get(key, "")))
            entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
            if secret:
                ctk.CTkButton(row, text="复制", width=58, fg_color="gray30",
                              command=lambda e=entry, l=label: self._copy_from(e, label=l)
                              ).pack(side="right", padx=(4, 0))
                ctk.CTkButton(row, text="粘贴", width=58, fg_color="gray30",
                              command=lambda e=entry: self._paste_into(e)
                              ).pack(side="right", padx=(4, 0))
            self._ai_setting_entries[key] = entry

    def _on_ai_backend_change(self, val: str) -> None:
        # 切换前先把当前 backend 的输入暂存进 cfg，避免切换丢失
        self._gather_ai_settings_into_cfg(self._current_displayed_backend())
        self._render_ai_settings(val)

    def _current_displayed_backend(self) -> str:
        # 当前 dropdown 选中的就是 displayed
        return self._ai_backend_var.get()

    # ── 行为 ───────────────────────────────────────────────
    def _paste_token(self) -> None:
        self._paste_into(self._entries["api_token"])

    def _copy_token(self) -> None:
        self._copy_from(self._entries["api_token"], label="API token")

    def _open_in_explorer(self, path: str) -> None:
        try:
            import os, subprocess
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
            self._log(f"已在资源管理器打开 {path}")
        except Exception as e:
            self._log(f"✗ 打开失败：{e}")

    def _copy_from(self, entry: ctk.CTkEntry, *, label: str = "内容") -> None:
        try:
            text = entry.get()
        except Exception:
            text = ""
        if not text:
            self._log(f"⊘ {label} 为空，无法复制")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()  # 把剪贴板锁住，避免 tk 退出时被回收
            self._log(f"✓ {label} 已复制到剪贴板")
        except Exception as e:
            self._log(f"✗ 复制失败：{e}")

    def _paste_into(self, entry: ctk.CTkEntry) -> None:
        try:
            clip = self.root.clipboard_get()
        except Exception as e:
            self._log(f"粘贴失败：剪贴板为空或无法读取（{e}）")
            return
        clip = (clip or "").strip()
        if not clip:
            self._log("粘贴失败：剪贴板为空")
            return
        entry.delete(0, "end")
        entry.insert(0, clip)
        self._log(f"已粘贴（{len(clip)} 字符）")

    def _autofill_defaults(self) -> None:
        """启动时给空字段自动填合理默认。

        模型：两个根目录决定其余
          1. project_root (默认 C:\\claude) → 派生 scripts/dashboard/history/index/...
          2. vault_path  (探测 obsidian 常见位置) → 派生 qa_vault_path
        派生路径只在用户没显式指定时填，已填的从不覆盖。
        """
        import os
        from pathlib import Path
        from paths import derive_paths, guess_vault, DEFAULT_PROJECT_ROOT  # type: ignore

        def first_existing(paths: list[str]) -> str:
            for p in paths:
                try:
                    if Path(p).exists():
                        return p
                except Exception:
                    continue
            return ""

        # 主项目根：给一个默认（不要求实在），用户可改
        self.cfg.setdefault("project_root", DEFAULT_PROJECT_ROOT)
        # 派生子路径
        derived = derive_paths(self.cfg.get("project_root"))
        for k, v in derived.items():
            self.cfg.setdefault(k, v)
        # python 解释器：frozen .exe 的 sys.executable 不能用，必须找真 python
        if not self.cfg.get("python_path"):
            py = first_existing([
                r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe",
                r"C:\Users\bwica\AppData\Local\Programs\Python\Python312\python.exe",
                r"C:\Users\bwica\AppData\Local\Programs\Python\Python311\python.exe",
                r"C:\Python313\python.exe",
                r"C:\Python312\python.exe",
                r"C:\Program Files\Python313\python.exe",
                r"C:\Program Files\Python312\python.exe",
            ])
            if not py:
                # PATH 里找
                import shutil
                py = shutil.which("python") or shutil.which("py") or ""
            if py:
                self.cfg["python_path"] = py

        # vault：探测
        if not self.cfg.get("vault_path"):
            v = guess_vault() or self._guess_vault()
            if v:
                self.cfg["vault_path"] = v
        if not self.cfg.get("qa_vault_path"):
            self.cfg["qa_vault_path"] = self.cfg.get("vault_path") or ""

        # qa 子目录默认
        self.cfg.setdefault("qa_exercises_subdir", "习题")
        self.cfg.setdefault("qa_wrong_subdir", "错题")
        self.cfg.setdefault("qa_hotkey", "ctrl+shift+q")
        # 每日定时默认值（关闭 + 04:00 + 启动 Anki）
        sched = dict(self.cfg.get("scheduled_register") or {})
        sched.setdefault("enabled", False)
        sched.setdefault("time", "04:00")
        sched.setdefault("wake_anki", True)
        self.cfg["scheduled_register"] = sched
        # 悬浮窗默认值
        floating = dict(self.cfg.get("floating") or {})
        floating.setdefault("position", "auto")
        floating.setdefault("click_through", False)
        self.cfg["floating"] = floating

        # Anki
        anki = dict(self.cfg.get("anki") or {})
        if not anki.get("exe_path"):
            anki["exe_path"] = first_existing([
                r"C:\Program Files\Anki\anki.exe",
                r"C:\Program Files (x86)\Anki\anki.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Anki\anki.exe"),
            ])
        if not anki.get("connect_url"):
            anki["connect_url"] = "http://localhost:8765"
        self.cfg["anki"] = anki

        # 浏览器（Chrome 找到则填，方便 --app 模式；找不到留空走系统默认）
        if not self.cfg.get("qa_browser_path"):
            self.cfg["qa_browser_path"] = first_existing([
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ])

        # AI CLI 命令真实路径（codex.cmd / claude.exe 通常不在 PATH，要写完整路径）
        ai = dict(self.cfg.get("ai") or {})
        # codex_cli
        codex_cfg = dict(ai.get("codex_cli") or {})
        cur_codex = (codex_cfg.get("command") or "").strip()
        if not cur_codex or cur_codex == "codex":
            found = first_existing([
                os.path.expandvars(r"%APPDATA%\npm\codex.cmd"),
                os.path.expandvars(r"%APPDATA%\npm\codex.bat"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\codex\codex.cmd"),
                r"C:\Program Files\nodejs\codex.cmd",
            ])
            if found:
                codex_cfg["command"] = found
                ai["codex_cli"] = codex_cfg
        # claude_cli
        claude_cfg = dict(ai.get("claude_cli") or {})
        cur_claude = (claude_cfg.get("command") or "").strip()
        if not cur_claude or cur_claude == "claude":
            found = first_existing([
                r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\claude.exe"),
                os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
                os.path.expandvars(r"%APPDATA%\npm\claude.bat"),
            ])
            if found:
                claude_cfg["command"] = found
                ai["claude_cli"] = claude_cfg
        self.cfg["ai"] = ai

        # 持久化探测结果
        try:
            self.cfg_path.write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _guess_vault(self) -> str:
        """猜一个 Obsidian vault 路径让用户首次启动看到默认值。env OBSIDIAN_VAULT 优先；找不到则空。"""
        import os
        from pathlib import Path
        home = Path.home()
        env_vault = os.environ.get("OBSIDIAN_VAULT")
        candidates = []
        if env_vault:
            candidates.append(Path(env_vault))
        candidates += [
            Path(r"C:\obsidian"),
            home / "Documents" / "Obsidian Vault",
            home / "Documents" / "obsidian",
            home / "OneDrive" / "Documents" / "Obsidian Vault",
            home / "OneDrive" / "Obsidian Vault",
            home / "iCloudDrive" / "iCloud~md~obsidian",
        ]
        for p in candidates:
            try:
                if p.exists() and p.is_dir() and any(p.glob("*.md")):
                    return str(p)
            except Exception:
                continue
        return ""

    def _pick_dir(self, key: str) -> None:
        d = filedialog.askdirectory(title=f"选择 {key}")
        if d:
            self._entries[key].delete(0, "end")
            self._entries[key].insert(0, d)

    def _pick_file(self, key: str, filetypes=None) -> None:
        f = filedialog.askopenfilename(title=f"选择 {key}", filetypes=filetypes or [])
        if f:
            self._entries[key].delete(0, "end")
            self._entries[key].insert(0, f)

    def _gather_ai_settings_into_cfg(self, backend_name: str) -> None:
        if not self._ai_setting_entries:
            return
        ai = dict(self.cfg.get("ai", {}) or {})
        ai[backend_name] = {k: e.get().strip() for k, e in self._ai_setting_entries.items()}
        self.cfg["ai"] = ai

    def _gather_cfg(self) -> dict:
        cfg = dict(self.cfg)
        # 顶层字段
        for k, e in self._entries.items():
            v = e.get().strip()
            if "." in k:
                # 嵌套（如 anki.exe_path）
                top, sub = k.split(".", 1)
                d = dict(cfg.get(top, {}) or {})
                if v:
                    d[sub] = v
                else:
                    d.pop(sub, None)
                cfg[top] = d
            else:
                if v:
                    cfg[k] = v
                else:
                    cfg.pop(k, None)
        # AI backend 选择 + 当前 backend settings
        backend = self._ai_backend_var.get()
        cfg["ai_backend"] = backend
        ai = dict(cfg.get("ai", {}) or {})
        if self._ai_setting_entries:
            ai[backend] = {k: e.get().strip() for k, e in self._ai_setting_entries.items()}
        cfg["ai"] = ai
        # 数字字段类型规整（写到 config.json 时不带引号）
        for k in ("watch_debounce_sec", "watch_cooldown_sec"):
            v = cfg.get(k)
            if isinstance(v, str):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    cfg.pop(k, None)
        # 悬浮窗启动开关
        if hasattr(self, "_show_floating_on_start_var") and self._show_floating_on_start_var is not None:
            cfg["show_floating_on_start"] = bool(self._show_floating_on_start_var.get())
        # qa 远程访问开关
        if hasattr(self, "_qa_remote_access_var") and self._qa_remote_access_var is not None:
            cfg["qa_remote_access"] = bool(self._qa_remote_access_var.get())
        # qa iPad 常驻 daemon 子开关
        if hasattr(self, "_qa_remote_daemon_var") and self._qa_remote_daemon_var is not None:
            cfg["qa_remote_daemon"] = bool(self._qa_remote_daemon_var.get())
        # scheduled_register checkbox 收集（time 已通过 entry "scheduled_register.time" 嵌套写入）
        sched = dict(cfg.get("scheduled_register") or {})
        if self._sched_enabled_var is not None:
            sched["enabled"] = bool(self._sched_enabled_var.get())
        if self._sched_wake_anki_var is not None:
            sched["wake_anki"] = bool(self._sched_wake_anki_var.get())
        if hasattr(self, "_sched_upload_after_var") and self._sched_upload_after_var is not None:
            sched["upload_after"] = bool(self._sched_upload_after_var.get())
        cfg["scheduled_register"] = sched
        # anki.auto_restart checkbox
        if hasattr(self, "_anki_auto_restart_var") and self._anki_auto_restart_var is not None:
            anki = dict(cfg.get("anki") or {})
            anki["auto_restart"] = bool(self._anki_auto_restart_var.get())
            cfg["anki"] = anki
        # floating 位置 + 鼠标穿透
        floating = dict(cfg.get("floating") or {})
        if self._floating_pos_var is not None:
            floating["position"] = self._floating_pos_var.get()
        if self._floating_click_through_var is not None:
            floating["click_through"] = bool(self._floating_click_through_var.get())
        cfg["floating"] = floating
        # 登记后是否自动上传网页
        if hasattr(self, "_auto_upload_var") and self._auto_upload_var is not None:
            cfg["auto_upload_after_register"] = bool(self._auto_upload_var.get())
        # cmd_server 远程触发
        cs = dict(cfg.get("cmd_server") or {})
        if self._cmd_server_enabled_var is not None:
            cs["enabled"] = bool(self._cmd_server_enabled_var.get())
        if self._cmd_server_port_var is not None:
            try:
                cs["port"] = int(self._cmd_server_port_var.get().strip())
            except ValueError:
                cs.setdefault("port", 9090)
        cfg["cmd_server"] = cs
        return cfg

    def _save_cfg(self) -> None:
        self.cfg = self._gather_cfg()
        self.cfg_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log("配置已保存")

    def _test_conn(self) -> None:
        cfg = self._gather_cfg()
        url, token = (cfg.get("server_url") or "").rstrip("/"), cfg.get("api_token") or ""
        if not url or not token:
            messagebox.showwarning("缺少配置", "请先填写服务端地址和 API token")
            return
        self._run_async(lambda: self._log_result(ApiClient(url, token).ping()))

    def _upload_website(self) -> None:
        """完整刷新 + 上传：anki_status → review_priority → export_dashboard
        → upload dashboard → export_history → upload history。
        登记完成后自动跑的也是同一个流程。"""
        cfg = self._gather_cfg()
        url   = (cfg.get("server_url") or "").rstrip("/")
        token = cfg.get("api_token") or ""
        if not (url and token):
            messagebox.showwarning("缺少配置", "服务端地址、API token 都要填")
            return
        self._save_cfg()
        client = ApiClient(url, token)
        self._run_async(lambda: self._website_pipeline(cfg, client))

    # 别名：保持旧调用点兼容
    _upload_dashboard = _upload_website

    def _website_pipeline(self, cfg: dict, client: ApiClient) -> None:
        py_exe = cfg.get("python_path") or None
        scripts_dir = (cfg.get("scripts_dir") or "C:/claude/scripts").strip()
        dash_dir = (cfg.get("dashboard_dir") or "").strip()
        hist_dir = (cfg.get("history_dir") or "").strip()

        def script(name: str) -> str:
            return str(Path(scripts_dir) / name)

        # ── 0. 确保 AnkiConnect 可用（按 cfg.anki.auto_restart 决定要不要杀僵尸+重启） ──
        anki_cfg = cfg.get("anki") or {}
        anki_url = anki_cfg.get("connect_url", "http://localhost:8765")
        anki_exe = anki_cfg.get("exe_path", "")
        auto_restart = bool(anki_cfg.get("auto_restart", False))
        self._log("▶ 0/6 检查 AnkiConnect")
        ok, msg = AnkiClient(anki_url).ensure_alive(anki_exe, force_restart=auto_restart)
        self._log(("✓ " if ok else "✗ ") + msg)
        if not ok:
            self._log("AnkiConnect 不可用，整个流程中止。开启「自动重启」开关或手动启动 Anki 后重试。")
            return
        wait = "0"  # 上一步已确认在线，anki_status.py 不需要再等

        # ── 1. 更新 Anki 学习状态 ──
        self._log("▶ 1/6 更新 Anki 学习状态")
        self._run_subscript(
            script("anki_status.py"),
            ["--all", "--write-frontmatter", "--write-record", "--wait-seconds", wait],
            py_exe,
        )

        # ── 2. 计算复习优先级 ──
        self._log("▶ 2/6 计算复习优先级")
        self._run_subscript(
            script("review_priority.py"),
            ["--write-frontmatter", "--write-record"],
            py_exe,
        )

        # ── 3. 生成 dashboard.json ──
        self._log("▶ 3/6 生成 dashboard.json")
        self._run_subscript(script("export_dashboard.py"), [], py_exe)

        # ── 4. 上传 dashboard ──
        self._log("▶ 4/6 上传 dashboard")
        if dash_dir:
            for evt in upload_dataset(client, Path(dash_dir), "dashboard"):
                self._log(evt)
        else:
            self._log("⊘ 未配置 dashboard 目录，跳过")

        # ── 5. 导出 history.json ──
        self._log("▶ 5/6 导出 history.json")
        self._run_subscript(script("export_history.py"), [], py_exe)

        # ── 6. 上传 history ──
        self._log("▶ 6/6 上传 history")
        if hist_dir:
            for evt in upload_dataset(client, Path(hist_dir), "history"):
                self._log(evt)
        else:
            self._log("⊘ 未配置 history 目录，跳过")

        self._log("✓ 网页刷新+上传 完成")

    def _run_subscript(self, script_path: str, args: list[str], py_exe: str | None) -> None:
        """跑一个主项目脚本并把每行输出写到日志。脚本不存在则记一行警告。"""
        if not script_path or not Path(script_path).exists():
            self._log(f"⊘ 跳过：找不到 {script_path}")
            return
        for line in run_script(script_path, *args, python_exe=py_exe):
            self._log(line)

    def _test_ai(self) -> None:
        self._save_cfg()
        backend = self.cfg.get("ai_backend", "claude_cli")
        settings = (self.cfg.get("ai") or {}).get(backend, {})
        try:
            ad = make_backend(backend, settings)
        except Exception as e:
            self._log(f"✗ 创建 backend 失败：{e}")
            return

        def task():
            self._log(f"测试 AI 后端 {backend}（连通性）...")
            ok, msg = ad.ping()
            self._log(("✓ " if ok else "✗ ") + msg)
            if not ok:
                return
            self._log(f"测试 chat（让 AI 回个 pong）...")
            try:
                reply = ad.chat([
                    {"role": "system", "content": "只用一个词回答用户。"},
                    {"role": "user", "content": "请回复：pong"},
                ])
                preview = reply.strip().splitlines()[0][:80] if reply else "(空)"
                self._log(f"✓ chat 回复：{preview}")
            except Exception as e:
                self._log(f"✗ chat 失败：{e}")

        self._run_async(task)

    def _ping_anki(self) -> None:
        cfg = self._gather_cfg()
        url = (cfg.get("anki") or {}).get("connect_url", "http://localhost:8765")
        self._log(f"ping AnkiConnect {url}...")
        self._run_async(lambda: self._log_result(AnkiClient(url).ping()))

    def _launch_anki(self) -> None:
        cfg = self._gather_cfg()
        exe = (cfg.get("anki") or {}).get("exe_path", "")
        if not exe:
            messagebox.showwarning("缺少配置", "先填 Anki 可执行路径")
            return
        ok, msg = open_program(exe)
        self._log(("✓ " if ok else "✗ ") + msg)

    def _run_register(self) -> None:
        if self._register_running:
            self._log("⊘ 登记笔记正在运行中，跳过本次触发")
            return
        cfg = self._gather_cfg()
        script = cfg.get("register_script") or ""
        if not script:
            messagebox.showwarning("缺少配置", "先填 register_notes.py 路径")
            return
        self._save_cfg()
        # 把 server / token / vault 等通过环境变量传给 register（如它需要）
        env = {
            "BWICARUS_SERVER":  cfg.get("server_url", ""),
            "BWICARUS_TOKEN":   cfg.get("api_token", ""),
            "BWICARUS_VAULT":   cfg.get("vault_path", ""),
            "BWICARUS_AI_BACKEND": cfg.get("ai_backend", ""),
        }
        self._log(f"启动 {script}...")

        py_exe = cfg.get("python_path") or None
        self._register_running = True

        def task():
            # 互斥：跑期间 watcher 触发会被自动跳过
            if self._watcher:
                self._watcher.set_busy(True)
            try:
                for line in run_script(script, extra_env=env, python_exe=py_exe):
                    self._log(line)
                # 完成后：仅当显式开关「auto_upload_after_register」开启时才跑刷新+上传
                # 默认关：用户点登记就只登记笔记本身，避免被一长串 anki/review/dashboard/history 步骤拖慢
                if cfg.get("auto_upload_after_register"):
                    url, token = cfg.get("server_url"), cfg.get("api_token")
                    if url and token:
                        self._log("登记完成，自动刷新并上传网页...")
                        client = ApiClient(url.rstrip("/"), token)
                        self._website_pipeline(cfg, client)
                else:
                    self._log("登记完成。要更新仪表板请单独点「刷新并上传网页」。")
            finally:
                if self._watcher:
                    self._watcher.set_busy(False)
                self._register_running = False

        self._run_async(task)

    def _run_full_daily(self, *, manual: bool) -> None:
        """触发完整 daily 流程（等价主项目 daily_anki_status.ps1）。

        - manual=True 来自「立即跑完整定时任务」按钮，force_restart=cfg.anki.auto_restart
        - manual=False 来自 scheduler 凌晨触发，force_restart=cfg.scheduled_register.wake_anki
        """
        if self._register_running:
            self._log("⊘ 笔记登记正在运行，跳过本次完整定时任务")
            return
        cfg = self._gather_cfg()
        sched_cfg = cfg.get("scheduled_register") or {}
        anki_cfg = cfg.get("anki") or {}
        upload = bool(sched_cfg.get("upload_after", False))
        if manual:
            force_restart = bool(anki_cfg.get("auto_restart", False))
        else:
            force_restart = bool(sched_cfg.get("wake_anki", True))
        self._save_cfg()
        self._run_async(lambda: self._full_daily_pipeline(
            cfg, upload=upload, force_restart=force_restart))

    def _full_daily_pipeline(self, cfg: dict, *, upload: bool, force_restart: bool) -> None:
        """ensure_alive → register → anki_status → review_priority →
        build_review_deck → cleanup_orphans → export_dashboard →
        (可选) upload dashboard + history → AnkiWeb sync。"""
        py_exe = cfg.get("python_path") or None
        scripts_dir = (cfg.get("scripts_dir") or "C:/claude/scripts").strip()
        register_script = (cfg.get("register_script") or "").strip() or \
            str(Path(scripts_dir) / "register_notes.py")

        def script(name: str) -> str:
            return str(Path(scripts_dir) / name)

        anki_cfg = cfg.get("anki") or {}
        anki_url = anki_cfg.get("connect_url", "http://localhost:8765")
        anki_exe = anki_cfg.get("exe_path", "")

        self._log("▶ 0/8 检查 AnkiConnect")
        ok, msg = AnkiClient(anki_url).ensure_alive(anki_exe, force_restart=force_restart)
        self._log(("✓ " if ok else "✗ ") + msg)
        if not ok:
            self._log("✗ AnkiConnect 不可用，完整定时任务中止")
            return

        env = {
            "BWICARUS_SERVER":  cfg.get("server_url", ""),
            "BWICARUS_TOKEN":   cfg.get("api_token", ""),
            "BWICARUS_VAULT":   cfg.get("vault_path", ""),
            "BWICARUS_AI_BACKEND": cfg.get("ai_backend", ""),
        }
        self._register_running = True
        try:
            if self._watcher:
                self._watcher.set_busy(True)

            self._log("▶ 1/8 登记新笔记")
            if not Path(register_script).exists():
                self._log(f"⊘ 找不到 {register_script}")
            else:
                for line in run_script(register_script, extra_env=env, python_exe=py_exe):
                    self._log(line)

            self._log("▶ 2/8 更新 Anki 学习状态")
            self._run_subscript(script("anki_status.py"),
                ["--all", "--write-frontmatter", "--write-record", "--wait-seconds", "0"], py_exe)

            self._log("▶ 3/8 计算复习优先级")
            self._run_subscript(script("review_priority.py"),
                ["--write-frontmatter", "--write-record"], py_exe)

            self._log("▶ 4/8 重建必复习牌组（必复习计算）")
            self._run_subscript(script("build_review_deck.py"), [], py_exe)

            self._log("▶ 5/8 清理孤儿")
            self._run_subscript(script("cleanup_orphans.py"), ["--apply"], py_exe)

            self._log("▶ 6/8 生成 dashboard.json")
            self._run_subscript(script("export_dashboard.py"), [], py_exe)

            if upload:
                url, token = cfg.get("server_url"), cfg.get("api_token")
                if url and token:
                    client = ApiClient(url.rstrip("/"), token)
                    dash_dir = (cfg.get("dashboard_dir") or "").strip()
                    hist_dir = (cfg.get("history_dir") or "").strip()
                    self._log("▶ 7/8 上传 dashboard")
                    if dash_dir:
                        for evt in upload_dataset(client, Path(dash_dir), "dashboard"):
                            self._log(evt)
                    else:
                        self._log("⊘ 未配置 dashboard 目录，跳过")
                    self._log("▶ 7b/8 导出 + 上传 history")
                    self._run_subscript(script("export_history.py"), [], py_exe)
                    if hist_dir:
                        for evt in upload_dataset(client, Path(hist_dir), "history"):
                            self._log(evt)
                    else:
                        self._log("⊘ 未配置 history 目录，跳过")
                else:
                    self._log("⊘ 缺少 server_url/api_token，跳过上传")
            else:
                self._log("⊘ 「完成后上传网页」未启用，跳过上传")

            self._log("▶ 8/8 AnkiWeb 同步")
            ok2, msg2 = AnkiClient(anki_url).sync()
            self._log(("✓ " if ok2 else "✗ ") + msg2)

            self._log("✓ 完整定时任务 done")
        finally:
            if self._watcher:
                self._watcher.set_busy(False)
            self._register_running = False

    # ── 配置向导 ───────────────────────────────────────────
    def _launch_wizard(self) -> None:
        OnboardingWizard(
            self.root, dict(self.cfg),
            done_callback=self._on_wizard_done,
            on_log=self._log,
        )

    def _on_wizard_done(self, updates: dict) -> None:
        if not updates:
            return
        self.cfg.update(updates)
        # 写盘
        self.cfg_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"✓ 向导完成，写入 {len(updates)} 项")
        # 把改动同步到主窗口的可见字段
        for k, v in updates.items():
            if k == "ai":
                # ai 是 dict，单独处理：刷新当前选中 backend 的 entries
                continue
            entry = self._entries.get(k)
            if entry is not None and isinstance(v, str):
                entry.delete(0, "end")
                entry.insert(0, v)
        # anki.exe_path / connect_url 也手动塞回 entries（key 形式 'anki.xxx'）
        anki = updates.get("anki") or {}
        for sub in ("exe_path", "connect_url"):
            if sub in anki:
                e = self._entries.get(f"anki.{sub}")
                if e is not None:
                    e.delete(0, "end"); e.insert(0, str(anki[sub]))
        # AI backend 选择 + settings 重新渲染
        if "ai_backend" in updates:
            self._ai_backend_var.set(updates["ai_backend"])
        self._render_ai_settings(self._ai_backend_var.get())

    # ── 登录（device-link OAuth） ──────────────────────────
    def _login_prompt(self) -> None:
        """没 token 时主动引导：打开浏览器登录 → 自动配置。"""
        server_url = (self.cfg.get("server_url") or "https://bwicarus.space").strip()
        ans = messagebox.askyesno(
            "连接到 bwicarus.space",
            f"客户端尚未连接。\n\n点「是」会打开浏览器跳到 {server_url}，\n"
            "登录账号后点「允许并连接」即可自动完成配置（无需手动复制 token）。\n\n"
            "点「否」可以稍后从「基础」tab 手动粘贴 token。",
        )
        if not ans:
            return
        self._start_device_link()

    def _start_device_link(self) -> None:
        server_url = (self._gather_cfg().get("server_url") or "https://bwicarus.space").strip()
        self._log(f"启动设备授权流程：{server_url}")

        def task():
            try:
                token = do_device_link_flow(
                    server_url, label="bwicarus-client",
                    on_status=self._log,
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("登录失败", str(e)))
                self._log(f"✗ 登录失败：{e}")
                return
            self.cfg["api_token"] = token
            self.cfg["server_url"] = server_url
            entry = self._entries.get("api_token")
            if entry is not None:
                self.root.after(0, lambda: (entry.delete(0, "end"), entry.insert(0, token)))
            self.cfg_path.write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log("✓ 已连接，token 已自动保存到本地配置")
            self.root.after(0, lambda: messagebox.showinfo(
                "已连接", "客户端已成功连接到服务器，现在可以正常使用了。"))

        self._run_async(task)

    # ── 远程触发服务（cmd_server） ─────────────────────────
    def _on_cmd_server_toggle(self) -> None:
        enabled = bool(self._cmd_server_enabled_var.get()) if self._cmd_server_enabled_var else False
        self.cfg = self._gather_cfg()
        self.cfg_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if enabled:
            self._start_cmd_server()
        else:
            self._stop_cmd_server()

    def _sync_qa_remote_subopts(self) -> None:
        """父开关「远程访问」变化时，启/禁用子复选框。"""
        chk = getattr(self, "_qa_remote_daemon_chk", None)
        parent_var = getattr(self, "_qa_remote_access_var", None)
        if chk is None or parent_var is None:
            return
        if parent_var.get():
            chk.configure(state="normal")
        else:
            chk.configure(state="disabled")

    def _start_qa_daemon(self) -> None:
        """常驻 qa_browser server，bind 0.0.0.0:9091，供 iPad（Tailscale 内）直连。
        仅当「远程访问」+「iPad 远程截图问答」两个开关都打开时启动。"""
        if getattr(self, "_qa_daemon", None) is not None:
            return
        if not self.cfg.get("qa_remote_access"):
            return
        if not self.cfg.get("qa_remote_daemon"):
            return
        try:
            import qa_browser  # type: ignore
        except Exception as e:
            self._log(f"✗ qa-daemon import 失败: {e}")
            return
        server = qa_browser.start_server_daemon(
            get_cfg=lambda: self.cfg, port=9091, bind="0.0.0.0"
        )
        self._qa_daemon = server
        if server is not None:
            try:
                import socket as _sock
                host_ip = _sock.gethostbyname(_sock.gethostname())
            except Exception:
                host_ip = "0.0.0.0"
            self._log(
                f"✓ qa-daemon: http://{host_ip}:9091  (iPad 远程问答；Tailscale 设备直连此 URL)"
            )

    def _start_cmd_server(self) -> None:
        if self._cmd_server is not None and self._cmd_server.is_running():
            return
        cs_cfg = self.cfg.get("cmd_server") or {}
        port = int(cs_cfg.get("port", 9090) or 9090)

        def trigger_register() -> str:
            # 在主线程调度，避免 tk 跨线程访问
            self.root.after(0, self._run_register)
            return "已触发登记新笔记（请关注监视栏进度）"

        def trigger_upload() -> str:
            self.root.after(0, self._upload_dashboard)
            return "已触发 dashboard 上传"

        def trigger_status() -> str:
            return f"register_running={self._register_running}"

        callbacks = {
            "newnote": trigger_register,
            "upload-website": trigger_upload,
            "status": trigger_status,
        }
        self._cmd_server = CmdServer(callbacks, port=port, on_log=self._log)
        ok, msg = self._cmd_server.start()
        if ok:
            self._log(f"✓ cmd_server: {msg}")
            if self._cmd_server_status_lbl:
                self._cmd_server_status_lbl.configure(text=f"已启动 · {msg}", text_color="#4ade80")
        else:
            self._log(f"✗ cmd_server: {msg}")
            if self._cmd_server_status_lbl:
                self._cmd_server_status_lbl.configure(text=f"启动失败: {msg}", text_color="#f87171")
            self._cmd_server = None

    def _stop_cmd_server(self) -> None:
        if self._cmd_server is None:
            return
        self._cmd_server.stop()
        self._cmd_server = None
        self._log("cmd_server 已停止")
        if self._cmd_server_status_lbl:
            self._cmd_server_status_lbl.configure(text="(未启动)", text_color="gray60")

    # ── 自动监听 ───────────────────────────────────────────
    def _on_auto_watch_toggle(self) -> None:
        if self._auto_watch_var.get():
            self._start_watcher()
        else:
            self._stop_watcher()
        # 持久化
        self.cfg = self._gather_cfg()
        self.cfg["auto_watch"] = bool(self._auto_watch_var.get())
        self.cfg_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _start_watcher(self) -> None:
        cfg = self._gather_cfg()
        vault = cfg.get("vault_path") or ""
        if not vault:
            self._log("✗ 自动监听启动失败：未配置 vault 路径")
            self._set_watcher_status("未启动（缺 vault 路径）", "gray60")
            if self._auto_watch_var:
                self._auto_watch_var.set(False)
            return
        if self._watcher and self._watcher.is_running:
            return
        try:
            debounce = float(cfg.get("watch_debounce_sec", 90) or 90)
            cooldown = float(cfg.get("watch_cooldown_sec", 600) or 600)
        except ValueError:
            debounce, cooldown = 90.0, 600.0
            self._log("✗ 防抖/冷却参数解析失败，回到默认 90s / 600s")
        debounce = max(5.0, debounce)
        cooldown = max(0.0, cooldown)
        self._watcher = VaultWatcher(
            vault,
            on_burst=self._on_vault_burst,
            on_skip=self._on_watcher_skip,
            debounce_sec=debounce,
            cooldown_sec=cooldown,
        )
        ok, msg = self._watcher.start()
        self._log(("✓ " if ok else "✗ ") + msg)
        if ok:
            self._set_watcher_status(
                f"监听中 · 防抖 {int(debounce)}s · 冷却 {int(cooldown)}s · {vault}",
                "#10b981",
            )
        else:
            self._set_watcher_status(msg, "gray60")
            if self._auto_watch_var:
                self._auto_watch_var.set(False)

    def _stop_watcher(self) -> None:
        if self._watcher:
            ok, msg = self._watcher.stop()
            self._log(msg)
        self._watcher = None
        self._set_watcher_status("未启动", "gray60")

    def _set_watcher_status(self, text: str, color: str) -> None:
        if self._watcher_status_lbl is not None:
            self.root.after(0, lambda: self._watcher_status_lbl.configure(text=text, text_color=color))

    def _on_vault_burst(self, paths: list[str]) -> None:
        # 在 watcher 线程被调用 → 切回主线程 schedule
        self._log(f"检测到 {len(paths)} 个 .md 变化（已通过防抖 + 冷却），触发同步…")
        if self._tray:
            self._tray.notify(f"检测到 {len(paths)} 个笔记变化，开始同步")
        self.root.after(0, self._run_register)

    def _on_watcher_skip(self, reason: str) -> None:
        self._log(f"⊘ {reason}")

    # ── 开机自启 ───────────────────────────────────────────
    def _on_startup_toggle(self) -> None:
        if self._startup_var is None:
            return
        if self._startup_var.get():
            ok, msg = startup_mod.enable()
        else:
            ok, msg = startup_mod.disable()
        self._log(("✓ " if ok else "✗ ") + msg)
        # 重新读真实状态（万一写入失败）
        self._startup_var.set(startup_mod.is_enabled())

    # ── 每日定时调度 ────────────────────────────────────────
    def _on_sched_toggle(self) -> None:
        self._save_cfg()
        self._refresh_scheduler()

    def _refresh_scheduler(self) -> None:
        """根据当前 cfg.scheduled_register.enabled 启停 scheduler。"""
        cfg = self._gather_cfg()
        sched_cfg = cfg.get("scheduled_register") or {}
        enabled = bool(sched_cfg.get("enabled"))
        if enabled and (not self._scheduler or not self._scheduler.is_running):
            self._scheduler = DailyScheduler(
                get_cfg=self._gather_cfg,
                on_trigger=self._sched_trigger,
                on_log=self._log,
            )
            ok, msg = self._scheduler.start()
            self._log(("✓ " if ok else "✗ ") + msg)
            self._poll_sched_status()
        elif not enabled and self._scheduler and self._scheduler.is_running:
            ok, msg = self._scheduler.stop()
            self._log(msg)
            if self._sched_status_lbl:
                self._sched_status_lbl.configure(text="未启动", text_color="gray60")

    def _poll_sched_status(self) -> None:
        if not self._scheduler or not self._scheduler.is_running:
            return
        s = self._scheduler.status()
        if self._sched_status_lbl:
            color = "#10b981" if "已启用" in s else "gray60"
            self._sched_status_lbl.configure(text=s, text_color=color)
        # 每 30 秒刷新状态文字（下次触发倒计时）
        self.root.after(30000, self._poll_sched_status)

    def _sched_trigger(self) -> None:
        """scheduler 凌晨触发 → 跑完整 daily 流程（含必复习计算）。

        AnkiConnect 健康检查由 _full_daily_pipeline 内部处理（force_restart 由
        cfg.scheduled_register.wake_anki 决定）。
        """
        self.root.after(0, lambda: self._run_full_daily(manual=False))

    # ── 全局热键 ───────────────────────────────────────────
    def _init_hotkey(self) -> None:
        if not hotkey_mod.is_available():
            return
        combo = (self.cfg.get("qa_hotkey") or "ctrl+shift+q").strip()
        ok, msg = hotkey_mod.register(combo, self._on_hotkey_qa)
        self._log(("✓ " if ok else "✗ ") + msg)
        if hasattr(self, "_hotkey_status") and self._hotkey_status:
            self._hotkey_status.configure(
                text=("已绑定" if ok else msg),
                text_color=("#10b981" if ok else "#f87171"),
            )

    def _on_hotkey_qa(self) -> None:
        # 在 keyboard 子线程被调，切回主线程触发
        self.root.after(0, self._launch_qa_browser)

    def _rebind_hotkey(self) -> None:
        if not hotkey_mod.is_available():
            self._log("✗ keyboard 库不可用，热键功能禁用")
            return
        new_combo = self._entries.get("qa_hotkey")
        if not new_combo:
            return
        combo = new_combo.get().strip() or "ctrl+shift+q"
        self.cfg["qa_hotkey"] = combo
        self._save_cfg()
        ok, msg = hotkey_mod.register(combo, self._on_hotkey_qa)
        self._log(("✓ " if ok else "✗ ") + msg)
        if self._hotkey_status:
            self._hotkey_status.configure(
                text=("已绑定" if ok else msg),
                text_color=("#10b981" if ok else "#f87171"),
            )

    def _record_hotkey(self) -> None:
        """点击「录制」按钮 → 进入捕获状态 → 用户按组合键 → 自动绑定。"""
        if not hotkey_mod.is_available():
            self._log("✗ keyboard 库不可用，热键功能禁用")
            return
        # 录制期间释放当前注册的热键，避免冲突
        cur = (self.cfg.get("qa_hotkey") or "").strip()
        if cur:
            hotkey_mod.unregister(cur)

        entry = self._entries["qa_hotkey"]
        entry.configure(state="disabled")
        entry.configure(placeholder_text="按下组合键…")
        entry.delete(0, "end")
        if self._hotkey_record_btn:
            self._hotkey_record_btn.configure(text="录制中…", state="disabled")
        if self._hotkey_status:
            self._hotkey_status.configure(text="按下你想用的组合键（如 Ctrl+Alt+Q）",
                                          text_color="#fbbf24")

        def task():
            ok, result = hotkey_mod.record(timeout=15.0)
            self.root.after(0, lambda: self._on_hotkey_recorded(ok, result, cur))

        threading.Thread(target=task, daemon=True).start()

    def _on_hotkey_recorded(self, ok: bool, result: str, fallback: str) -> None:
        entry = self._entries["qa_hotkey"]
        entry.configure(state="normal")
        if self._hotkey_record_btn:
            self._hotkey_record_btn.configure(text="🎙 录制", state="normal")

        combo = result if ok else fallback
        entry.delete(0, "end")
        if combo:
            entry.insert(0, combo)
        if not ok:
            self._log(f"✗ 录制失败：{result}（恢复为 {fallback or '无'}）")
            # 恢复原热键绑定
            if fallback:
                hotkey_mod.register(fallback, self._on_hotkey_qa)
            if self._hotkey_status:
                self._hotkey_status.configure(text=result, text_color="#f87171")
            return

        # 成功：保存 + 绑定
        self.cfg["qa_hotkey"] = combo
        self._save_cfg()
        ok2, msg = hotkey_mod.register(combo, self._on_hotkey_qa)
        self._log(("✓ 已录制并绑定 " if ok2 else "✗ ") + combo + ("：" + msg if not ok2 else ""))
        if self._hotkey_status:
            self._hotkey_status.configure(
                text=(f"已绑定 {combo}" if ok2 else msg),
                text_color=("#10b981" if ok2 else "#f87171"),
            )

    # ── 任务监视悬浮窗 ─────────────────────────────────────
    def _init_floating(self) -> None:
        try:
            self._floating = FloatingWindow(
                self.root,
                state_file_getter=lambda: (self._gather_cfg().get("task_state_file") or None),
                get_cfg=self._gather_cfg,
                on_save_position=self._save_floating_position,
            )
        except Exception as e:
            self._log(f"✗ 任务监视悬浮窗初始化失败：{e}")
            self._floating = None

    def _save_floating_position(self, x: int, y: int) -> None:
        """悬浮窗拖动结束时持久化位置。"""
        cfg = self._gather_cfg()
        floating = dict(cfg.get("floating") or {})
        floating["custom_x"] = int(x)
        floating["custom_y"] = int(y)
        # 拖动后自动切到 custom 模式
        floating["position"] = "custom"
        self.cfg["floating"] = floating
        if self._floating_pos_var is not None:
            self._floating_pos_var.set("custom")
        self.cfg_path.write_text(
            json.dumps(self._gather_cfg(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _on_floating_pos_change(self, val: str) -> None:
        cfg = self._gather_cfg()
        floating = dict(cfg.get("floating") or {})
        floating["position"] = val
        self.cfg["floating"] = floating
        self._save_cfg()
        if self._floating:
            self._floating.apply_cfg_position()

    def _on_floating_click_through_change(self) -> None:
        cfg = self._gather_cfg()
        floating = dict(cfg.get("floating") or {})
        enabled = bool(self._floating_click_through_var.get())
        floating["click_through"] = enabled
        self.cfg["floating"] = floating
        self._save_cfg()
        if self._floating:
            self._floating.set_click_through(enabled)

    def _floating_show(self) -> None:
        if self._floating:
            self.root.after(0, self._floating.show)

    def _floating_hide(self) -> None:
        if self._floating:
            self.root.after(0, self._floating.hide)

    def _floating_toggle(self) -> None:
        if self._floating:
            self.root.after(0, self._floating.toggle)

    # ── 系统托盘 ───────────────────────────────────────────
    def _init_tray(self) -> None:
        try:
            self._tray = TrayIcon(
                on_show=self._show_window,
                on_sync_now=self._sync_now,
                on_quit=self._quit_app,
                on_toggle_floating=self._floating_toggle if self._floating else None,
            )
            self._tray.start()
        except Exception as e:
            self._log(f"✗ 托盘启动失败：{e}")
            self._tray = None

    def _show_window(self) -> None:
        self.root.after(0, lambda: (
            self.root.deiconify(),
            self.root.lift(),
            self.root.focus_force(),
        ))

    def _sync_now(self) -> None:
        self.root.after(0, self._run_register)

    def _quit_app(self) -> None:
        self._stop_watcher()
        if self._scheduler and self._scheduler.is_running:
            try: self._scheduler.stop()
            except Exception: pass
        try: hotkey_mod.unregister_all()
        except Exception: pass
        if self._floating:
            try: self._floating.destroy()
            except Exception: pass
        if self._cmd_server is not None:
            try: self._cmd_server.stop()
            except Exception: pass
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    def _on_close_window(self) -> None:
        # 默认隐藏到托盘，不退出（这样 watcher 仍能后台跑）
        self.root.withdraw()
        if self._tray:
            self._tray.notify("已最小化到托盘，右键图标可恢复")

    # ── 工具 ───────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        def write():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, write)

    def _log_result(self, result: tuple[bool, str]) -> None:
        ok, msg = result
        self._log(("✓ " if ok else "✗ ") + msg)

    def _run_async(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._stop_watcher()
            if self._tray:
                self._tray.stop()
