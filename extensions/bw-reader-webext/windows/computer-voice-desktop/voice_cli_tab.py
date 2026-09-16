# -*- coding: utf-8 -*-
"""ReaderPC 服务器窗口里的「语音 CLI」分页：自建 Codex 语音会话（voice_cli_runner.py）的控制台。

只做代理：运行器常驻 127.0.0.1:43131，这里拉起/停掉它，读状态与事件流，改设置，发测试输入，看额度。
所有 HTTP 都在工作线程里跑，结果经 root.after 回到 UI 线程；UI 永远不阻塞。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable
import urllib.error
import urllib.request

RUNNER_URL = "http://127.0.0.1:43131"
VOICE_CLI_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BWReader" / "voice-cli"
RUNNER_CONFIG = VOICE_CLI_DIR / "runner.json"
POLL_MS = 2_000
QUIET_KINDS = {"tokens", "dc_turn_created", "dc_turn_done", "peer_state", "notify", "server_request"}
STATE_LABEL = {"idle": "空闲", "starting": "建立中", "connected": "通话中", "reconnecting": "重连中", "stopping": "停止中"}
FIELDS = (
    # key, 标签, 类型, 备注
    ("backendModel", "后台模型", "model"),
    ("effort", "推理强度", "effort"),
    ("voice", "声音（空=默认）", "voice"),
    ("realtimeModel", "语音模型（空=默认）", "text"),
    ("handoffMode", "交回模式", "choice:thinking,commentary,bemTags"),
    ("includeStartupContext", "带 Codex 启动上下文", "bool"),
    ("clientManagedHandoffs", "关闭自动交回（全 DIY）", "bool"),
    ("autoReconnect", "掉线自动重开", "bool"),
    ("inputDevice", "输入设备（麦克风侧）", "indevice"),
    ("outputDevice", "输出设备（扬声器侧）", "outdevice"),
)


def http_json(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(RUNNER_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": f"运行器不可达: {e}", "runnerDown": True}


def read_runner_config() -> dict:
    try:
        return json.loads(RUNNER_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def launch_runner(cfg: dict) -> tuple[bool, str]:
    """按 runner.json 拉起运行器（脱离本进程），等它把 HTTP 口开起来。"""
    py, script = cfg.get("python"), cfg.get("script")
    if not (py and script and Path(py).exists() and Path(script).exists()):
        return False, f"runner.json 里的 python/script 不存在：{RUNNER_CONFIG}"
    VOICE_CLI_DIR.mkdir(parents=True, exist_ok=True)
    out = open(VOICE_CLI_DIR / "runner.out", "ab")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen([py, script], stdout=out, stderr=subprocess.STDOUT, cwd=str(VOICE_CLI_DIR), creationflags=flags)
    for _ in range(60):
        time.sleep(0.25)
        if not http_json("GET", "/status", timeout=1).get("runnerDown"):
            return True, f"运行器已启动 pid {proc.pid}"
        if proc.poll() is not None:
            break
    return False, f"运行器没起来（退出码 {proc.poll()}），看 {VOICE_CLI_DIR / 'runner.out'}"


def format_event(ev: dict) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("t", 0)))
    k = ev.get("kind", "")
    if k == "transcript":
        body = ("👤 " if ev.get("role") == "user" else "🔊 ") + str(ev.get("text", ""))
    elif k in ("say", "tell", "inject", "turn_start"):
        body = f"➡ {k}{'(' + ev['role'] + ')' if ev.get('role') else ''}: {ev.get('text', '')}" + ("  [用户说话中]" if ev.get("userSpeaking") else "")
    elif k in ("item/started", "item/completed"):
        body = f"{'⚙ 开始' if k == 'item/started' else '✓ 完成'} {ev.get('itemType')}{' ' + str(ev['tool']) if ev.get('tool') else ''}{' — ' + str(ev['text']) if ev.get('text') else ''}"
    elif k == "tokens":
        body = f"tokens 输入 {ev.get('input')}（缓存 {ev.get('cached')}）输出 {ev.get('output')}"
    elif k.startswith("dc_turn"):
        body = f"dc {k[3:]} {ev.get('role')}{' \"' + str(ev.get('transcript')) + '\"' if ev.get('transcript') else ''}"
    elif k == "app_server_stderr":
        body = f"⚠ app-server: {ev.get('message')}"
    else:
        rest = {x: y for x, y in ev.items() if x not in ("seq", "t", "kind")}
        body = k + (" " + json.dumps(rest, ensure_ascii=False) if rest else "")
    return f"[{t}] {body}"


class ScrollFrame(ttk.Frame):
    """竖向可滚动的容器：Canvas + 内层 Frame + 滚动条，鼠标滚轮在其上时滚动。内容放 self.inner。"""

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class VoiceCliTab:
    def __init__(self, notebook: ttk.Notebook, root: tk.Misc):
        self.root = root
        self.notebook = notebook
        self.since = 0
        self.catalog: dict | None = None
        self.settings: dict | None = None
        self.hot: list[str] = []
        self.cold: list[str] = []
        self.vars: dict[str, tk.Variable] = {}
        self.widgets: dict[str, tk.Widget] = {}
        self._polling = False
        self.closed = False
        self.frame = ttk.Frame(notebook, padding=(2, 8))
        notebook.add(self.frame, text="语音 CLI")
        # 内容比窗口高（2026-09-13 用户：「没有滑动条导致看不到下方内容」）→ 整页放进可滚动容器
        self.scroll = ScrollFrame(self.frame)
        self.scroll.pack(fill="both", expand=True)
        self.content = self.scroll.inner
        self.devices: dict[str, list[str]] = {"in": [], "out": []}
        self._build()
        root.after(1_200, self._tick)

    # ---------- 布局 ----------
    def _build(self) -> None:
        f = self.content
        head = ttk.Frame(f)
        head.pack(fill="x")
        self.state_label = ttk.Label(head, text="运行器未启动", style="ReaderPC.Heading.TLabel")
        self.state_label.pack(side="left")
        self.meta_label = ttk.Label(head, text="", foreground="#666")
        self.meta_label.pack(side="left", padx=(10, 0))

        rows = ttk.Frame(f)
        rows.pack(fill="x", pady=(4, 2))
        self.detail_label = ttk.Label(rows, text="自建 Codex 语音会话：app-server（ChatGPT 登录）+ WebRTC v3，音频走 App 的两条虚拟线缆。", foreground="#555", wraplength=760, justify="left")
        self.detail_label.pack(anchor="w")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(4, 6))
        for text, cmd in (("启动运行器", self.runner_start), ("停运行器", self.runner_stop)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=6)
        for text, path in (("开始会话", "/session/start"), ("停止", "/session/stop"), ("重开", "/session/restart"), ("静音麦", "/pause"), ("恢复麦", "/resume")):
            ttk.Button(btns, text=text, command=lambda p=path: self._post(p, {}, note=text)).pack(side="left", padx=(0, 4))
        self.restart_label = ttk.Label(btns, text="", foreground="#b26a00")
        self.restart_label.pack(side="left", padx=(10, 0))

        body = ttk.Panedwindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 6, 0))
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        # 设置
        sf = ttk.LabelFrame(left, text="设置（绿=改完即生效 · 橙=要重开会话）", padding=(6, 4))
        sf.pack(fill="x")
        self.settings_frame = sf
        self.settings_grid = ttk.Frame(sf)
        self.settings_grid.pack(fill="x")
        ttk.Label(self.settings_grid, text="运行器未连接").grid(row=0, column=0, sticky="w")
        pf = ttk.Frame(sf)
        pf.pack(fill="x", pady=(4, 0))
        ttk.Label(pf, text="语音模型指令").pack(anchor="w")
        self.prompt_text = tk.Text(pf, height=3, wrap="word", font=("Microsoft YaHei UI", 9))
        self.prompt_text.pack(fill="x")
        sb = ttk.Frame(sf)
        sb.pack(fill="x", pady=(4, 0))
        ttk.Button(sb, text="保存设置", command=self.save_settings).pack(side="left")
        ttk.Button(sb, text="刷新模型/声音列表", command=lambda: self.load_settings(True)).pack(side="left", padx=(6, 0))

        # 测试输入
        tf = ttk.LabelFrame(left, text="测试输入", padding=(6, 4))
        tf.pack(fill="x", pady=(8, 0))
        self.input_text = tk.Text(tf, height=2, wrap="word", font=("Microsoft YaHei UI", 9))
        self.input_text.pack(fill="x")
        self.input_text.insert("1.0", "【状态】用户正在读《测试书》第 42 页。")
        ir = ttk.Frame(tf)
        ir.pack(fill="x", pady=(4, 0))
        ttk.Label(ir, text="角色").pack(side="left")
        self.role_var = tk.StringVar(value="developer")
        ttk.Combobox(ir, textvariable=self.role_var, values=("developer", "user", "assistant"), width=10, state="readonly").pack(side="left", padx=(4, 8))
        for text, path in (("念出", "/say"), ("塞给语音", "/tell"), ("给后台起轮", "/turn"), ("塞后台历史", "/inject")):
            ttk.Button(ir, text=text, command=lambda p=path: self.send_input(p)).pack(side="left", padx=(0, 4))

        # 额度
        qf = ttk.LabelFrame(left, text="额度", padding=(6, 4))
        qf.pack(fill="x", pady=(8, 0))
        self.quota_label = ttk.Label(qf, text="（点刷新）", wraplength=360, justify="left")
        self.quota_label.pack(anchor="w")
        ttk.Button(qf, text="刷新额度", command=self.refresh_quota).pack(anchor="e")

        # 字幕 + 终端
        cf = ttk.LabelFrame(right, text="字幕", padding=(6, 4))
        cf.pack(fill="x")
        self.transcript_text = tk.Text(cf, height=5, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.transcript_text.pack(fill="x")
        lf = ttk.LabelFrame(right, text="终端（运行器事件流：字幕 / 委派 / 工具 / 数据通道 / app-server 报错）", padding=(6, 4))
        lf.pack(fill="both", expand=True, pady=(8, 0))
        top = ttk.Frame(lf)
        top.pack(fill="x")
        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="显示全部（含 tokens / 数据通道 turn 事件）", variable=self.verbose_var).pack(side="left")
        ttk.Button(top, text="清屏", command=self.clear_log).pack(side="right")
        self.log_text = tk.Text(lf, height=14, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    # ---------- 线程工具 ----------
    def _bg(self, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        def run():
            try:
                result = fn()
            except Exception as e:  # 永不让线程异常吞掉 UI 回调
                result = {"ok": False, "msg": str(e)}
            if done and not self.closed:
                try:
                    self.root.after(0, lambda: done(result))
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _post(self, path: str, body: dict, note: str = "") -> None:
        if note:
            self.meta_label.configure(text=f"{note}…")
        self._bg(lambda: http_json("POST", path, body, timeout=150), lambda d: self._note(d, note))

    def _note(self, d: dict, note: str = "") -> None:
        msg = d.get("msg") or ("完成" if d.get("ok", True) else "失败")
        self.meta_label.configure(text=f"{note}: {msg}" if note else msg, foreground="#167347" if d.get("ok", True) else "#b00020")
        if d.get("needsRestart") is not None:
            self._show_needs_restart(d.get("needsRestart") or [])

    # ---------- 运行器 ----------
    def runner_start(self) -> None:
        self.meta_label.configure(text="正在拉起运行器…", foreground="#555")
        cfg = read_runner_config()
        self._bg(lambda: dict(zip(("ok", "msg"), launch_runner(cfg))), lambda d: (self._note(d), self.load_settings(True)))

    def runner_stop(self) -> None:
        self._post("/shutdown", {}, note="停运行器")

    # ---------- 设置 ----------
    def load_settings(self, with_catalog: bool) -> None:
        def fetch():
            s = http_json("GET", "/settings")
            if s.get("runnerDown"):
                return s
            if with_catalog or not self.catalog:
                c = http_json("GET", "/catalog", timeout=60)
                if not c.get("runnerDown") and c.get("models") is not None:
                    s["_catalog"] = c
                dv = http_json("GET", "/devices", timeout=15)
                if dv.get("devices") is not None:
                    s["_devices"] = dv["devices"]
            return s
        self._bg(fetch, self._render_settings)

    def _render_settings(self, d: dict) -> None:
        if d.get("runnerDown"):
            return
        self.settings = d.get("settings") or {}
        self.hot = d.get("hotKeys") or []
        self.cold = d.get("coldKeys") or []
        if d.get("_catalog"):
            self.catalog = d["_catalog"]
        if d.get("_devices") is not None:
            # 同一设备在 WASAPI/WDM-KS/MME 下各出现一次 → 按名字去重，保持首次出现顺序
            # MME 把名字截到 31 个字符，WASAPI 是全名 → WASAPI 优先，截断名若是某个全名的前缀就不再列
            def collect(key: str) -> list[str]:
                names: list[str] = []
                ordered = sorted(d["_devices"], key=lambda x: 0 if x.get("api") == "Windows WASAPI" else 1)
                for dev in ordered:
                    if dev.get(key, 0) <= 0:
                        continue
                    name = str(dev.get("name") or "").strip()
                    if not name or any(n == name or n.startswith(name) or name.startswith(n) for n in names):
                        continue
                    names.append(name)
                return names
            self.devices = {"in": collect("in"), "out": collect("out")}
        for w in self.settings_grid.winfo_children():
            w.destroy()
        self.vars.clear()
        self.widgets.clear()
        voices = (self.catalog or {}).get("voices") or {}
        models = (self.catalog or {}).get("models") or []
        for row, (key, label, kind) in enumerate(FIELDS):
            v = self.settings.get(key)
            color = "#167347" if key in self.hot else ("#b26a00" if key in self.cold else "#999")
            ttk.Label(self.settings_grid, text="●", foreground=color).grid(row=row, column=0, sticky="w")
            ttk.Label(self.settings_grid, text=label).grid(row=row, column=1, sticky="w", padx=(2, 8))
            if kind == "bool":
                var: tk.Variable = tk.BooleanVar(value=bool(v))
                w: tk.Widget = ttk.Checkbutton(self.settings_grid, variable=var)
            elif kind == "model":
                var = tk.StringVar(value=str(v or ""))
                w = ttk.Combobox(self.settings_grid, textvariable=var, values=[m["id"] for m in models] or [str(v or "")], width=22)
                w.bind("<<ComboboxSelected>>", lambda _e: self._refresh_effort_choices())
            elif kind == "effort":
                var = tk.StringVar(value=str(v or ""))
                w = ttk.Combobox(self.settings_grid, textvariable=var, values=self._effort_choices(str(self.settings.get("backendModel") or "")), width=10, state="readonly")
            elif kind == "voice":
                ver = str(self.settings.get("version") or "v3")
                lst = voices.get("v2" if ver == "v2" else "v1") or []
                var = tk.StringVar(value=str(v or ""))
                w = ttk.Combobox(self.settings_grid, textvariable=var, values=[""] + list(lst), width=12, state="readonly")
            elif kind in ("indevice", "outdevice"):
                names = self.devices["in" if kind == "indevice" else "out"]
                cur = str(v or "")
                if cur and cur not in names:
                    names = [cur] + names
                var = tk.StringVar(value=cur)
                w = ttk.Combobox(self.settings_grid, textvariable=var, values=names, width=44)
            elif kind.startswith("choice:"):
                var = tk.StringVar(value=str(v or ""))
                w = ttk.Combobox(self.settings_grid, textvariable=var, values=kind.split(":", 1)[1].split(","), width=12, state="readonly")
            else:
                var = tk.StringVar(value=str(v if v is not None else ""))
                w = ttk.Entry(self.settings_grid, textvariable=var, width=34)
            w.grid(row=row, column=2, sticky="w", pady=1)
            self.vars[key] = var
            self.widgets[key] = w
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", str(self.settings.get("prompt") or ""))
        self._show_needs_restart(d.get("needsRestart") or [])

    def _effort_choices(self, model_id: str) -> list[str]:
        for m in (self.catalog or {}).get("models") or []:
            if m.get("id") == model_id and m.get("efforts"):
                return list(m["efforts"])
        return ["low", "medium", "high", "xhigh", "max", "ultra"]

    def _refresh_effort_choices(self) -> None:
        w = self.widgets.get("effort")
        if isinstance(w, ttk.Combobox):
            w.configure(values=self._effort_choices(str(self.vars["backendModel"].get())))

    def _show_needs_restart(self, keys: list[str]) -> None:
        self.restart_label.configure(text=("需要重开会话：" + ", ".join(keys)) if keys else "")

    def save_settings(self) -> None:
        if self.settings is None:
            self._note({"ok": False, "msg": "设置还没加载"})
            return
        patch: dict[str, Any] = {}
        for key, _label, kind in FIELDS:
            var = self.vars.get(key)
            if var is None:
                continue
            val: Any = bool(var.get()) if kind == "bool" else str(var.get())
            if val != self.settings.get(key) and not (val == "" and self.settings.get(key) in (None, "")):
                patch[key] = val
        prompt = self.prompt_text.get("1.0", "end").strip()
        if prompt != (self.settings.get("prompt") or ""):
            patch["prompt"] = prompt
        if not patch:
            self._note({"ok": True, "msg": "没有改动"})
            return
        def done(d: dict) -> None:
            if d.get("settings"):
                self.settings = d["settings"]
                hot = d.get("hotApplied")
                msg = "已保存" + ("，热设置已生效" if hot and not hot.get("error") else "") + ("；冷设置要重开会话" if d.get("needsRestart") else "")
                self._note({"ok": True, "msg": msg, "needsRestart": d.get("needsRestart")})
            else:
                self._note(d)
        self._bg(lambda: http_json("POST", "/settings", patch, timeout=60), done)

    # ---------- 输入 / 额度 ----------
    def send_input(self, path: str) -> None:
        text = self.input_text.get("1.0", "end").strip()
        if not text:
            self._note({"ok": False, "msg": "先写点内容"})
            return
        self._post(path, {"text": text, "role": self.role_var.get()}, note=path.strip("/"))

    def refresh_quota(self) -> None:
        self.quota_label.configure(text="查询中…")
        def done(d: dict) -> None:
            if d.get("runnerDown"):
                self.quota_label.configure(text=d.get("msg", "运行器不可达"))
                return
            rl = d.get("rateLimits") or {}
            pr = rl.get("primary") or {}
            reset = time.strftime("%m-%d %H:%M", time.localtime(pr["resetsAt"])) if pr.get("resetsAt") else "?"
            tok = (d.get("tokens") or {}).get("total") or {}
            lines = [f"套餐 {rl.get('planType', '?')} · 主窗口已用 {pr.get('usedPercent', '?')}%（{(pr.get('windowDurationMins') or 0) // 60} 小时窗口，重置 {reset}）",
                     f"本会话语音模型音频时长 {int((d.get('audioDurationMs') or 0) / 1000)} s"]
            if tok:
                lines.append(f"后台模型本线程累计：输入 {tok.get('inputTokens')}（缓存命中 {tok.get('cachedInputTokens')}）· 输出 {tok.get('outputTokens')}")
            if d.get("today"):
                lines.append(f"今日 token（账号）{d['today'].get('tokens')}")
            self.quota_label.configure(text="\n".join(lines))
        self._bg(lambda: http_json("GET", "/quota", timeout=60), done)

    # ---------- 轮询 ----------
    def _visible(self) -> bool:
        try:
            return self.notebook.select() == str(self.frame) and self.root.winfo_viewable()
        except Exception:
            return False

    def _tick(self) -> None:
        if self.closed:
            return
        if self._visible() and not self._polling:
            self._polling = True
            since = self.since
            def fetch():
                st = http_json("GET", "/status", timeout=5)
                ev = {} if st.get("runnerDown") else http_json("GET", f"/events?since={since}&limit=300", timeout=5)
                return st, ev
            self._bg(fetch, self._apply)
        self.root.after(POLL_MS, self._tick)

    def _apply(self, result: tuple[dict, dict]) -> None:
        self._polling = False
        st, ev = result
        if st.get("runnerDown"):
            self.state_label.configure(text="运行器未启动", foreground="#666")
            cfg = read_runner_config()
            self.detail_label.configure(text=f"点「启动运行器」。用哪个 Python/脚本见 {RUNNER_CONFIG}" + ("" if cfg else "（还没有这个文件）"))
            return
        s = st.get("session") or {}
        r = st.get("runner") or {}
        state = s.get("state", "idle")
        self.state_label.configure(text=STATE_LABEL.get(state, state), foreground="#167347" if state == "connected" else ("#b26a00" if state != "idle" else "#333"))
        parts = [f"运行器 pid {r.get('pid')}", "app-server 已连" if r.get("appServer") else "app-server 未起", f"线程 {str(r.get('threadId') or '（未建）')[:8]}"]
        if state == "connected":
            parts += [f"会话 #{s.get('sessionNo')} · {s.get('seconds')}s", f"重连 {s.get('reconnects')} 次"]
            if s.get("userSpeaking"):
                parts.append("用户说话中")
            if s.get("backendBusy"):
                parts.append("后台工作中")
            if s.get("micLevel") is not None:
                parts.append(f"麦 {s['micLevel']}")
        if s.get("lastError") and state != "connected":
            parts.append("上次错误：" + str(s["lastError"])[:80])
        parts.append("桥标记：" + ("App 语音走本运行器" if st.get("bridgeFlag") else "App 语音走桌面 Codex"))
        self.detail_label.configure(text=" · ".join(parts))
        self._show_needs_restart(s.get("needsRestart") or [])
        if self.settings is None:
            self.load_settings(True)
        tr = "\n".join(("👤 " if x.get("role") == "user" else "🔊 ") + str(x.get("text", "")) for x in st.get("transcripts") or [])
        self._set_text(self.transcript_text, tr or "（还没有字幕）")
        u = st.get("usage") or {}
        pr = ((u.get("rateLimits") or {}).get("primary") or {})
        self.meta_label.configure(text=f"语音音频 {int((u.get('audioDurationMs') or 0) / 1000)} s" + (f" · 主窗口 {pr.get('usedPercent')}%" if pr else ""), foreground="#555")
        events = ev.get("events") or []
        if events:
            verbose = self.verbose_var.get()
            lines = [format_event(e) for e in events if verbose or e.get("kind") not in QUIET_KINDS]
            self.since = ev.get("next", self.since)
            if lines:
                self._append_log("\n".join(lines))

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        widget.see("end")

    def _append_log(self, text: str) -> None:
        w = self.log_text
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n")
        w.insert("end", text)
        # 只留最后 800 行
        n = int(w.index("end-1c").split(".")[0])
        if n > 800:
            w.delete("1.0", f"{n - 800}.0")
        w.configure(state="disabled")
        w.see("end")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def close(self) -> None:
        self.closed = True
