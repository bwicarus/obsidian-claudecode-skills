"""首次启动配置向导（onboarding wizard）。

四步：
  1. 连接服务器（一键登录 / 已有 token 跳过）
  2. Obsidian Vault 路径（探测 + 让用户确认或自选）
  3. Anki（exe 路径探测 + 测 AnkiConnect）
  4. AI 后端（选 + 填 key + 测）

通过 done_callback(cfg_updates: dict) 把改动一次性回写到主 GUI 的 cfg。
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from typing import Callable

import customtkinter as ctk
from tkinter import filedialog, messagebox

from ai_backends import (  # type: ignore
    list_backends, make_backend, backend_default_settings, backend_setting_fields,
)
from anki import AnkiClient  # type: ignore
from auth import do_device_link_flow  # type: ignore
from paths import guess_vault  # type: ignore


STEPS = ("连接服务器", "Obsidian", "Anki", "AI 后端")


class OnboardingWizard:
    def __init__(
        self,
        master: ctk.CTk,
        cfg: dict,
        *,
        done_callback: Callable[[dict], None],
        on_log: Callable[[str], None] | None = None,
    ):
        self.master = master
        self.cfg = cfg
        self.done = done_callback
        self.log = on_log or (lambda _msg: None)
        self.updates: dict = {}
        self.current_step = 0
        self._build_window()
        self._render_step()

    def _build_window(self) -> None:
        w = ctk.CTkToplevel(self.master)
        w.title("欢迎使用 bwicarus-client")
        w.geometry("560x460")
        w.minsize(520, 420)
        w.transient(self.master)
        w.lift()
        w.protocol("WM_DELETE_WINDOW", self._on_close)
        self.win = w

        # 顶部步骤指示器
        self.header = ctk.CTkFrame(w, fg_color="transparent")
        self.header.pack(fill="x", padx=20, pady=(20, 6))
        self.title_lbl = ctk.CTkLabel(
            self.header, text="", font=ctk.CTkFont(size=18, weight="bold"))
        self.title_lbl.pack(anchor="w")
        self.step_lbl = ctk.CTkLabel(
            self.header, text="", text_color="gray60", font=ctk.CTkFont(size=12))
        self.step_lbl.pack(anchor="w", pady=(2, 0))

        sep = ctk.CTkFrame(w, height=1, fg_color="gray25")
        sep.pack(fill="x", padx=20, pady=(8, 12))

        # 中部内容区（每步重建）
        self.body = ctk.CTkFrame(w, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # 底部状态行
        self.status_lbl = ctk.CTkLabel(
            w, text="", text_color="gray60",
            font=ctk.CTkFont(size=11), wraplength=520, justify="left",
        )
        self.status_lbl.pack(fill="x", padx=20, pady=(0, 6))

        # 底部按钮行
        bot = ctk.CTkFrame(w, fg_color="transparent")
        bot.pack(fill="x", padx=20, pady=(0, 16))
        self.skip_btn = ctk.CTkButton(
            bot, text="跳过此步", command=self._next_step,
            fg_color="gray30", width=100,
        )
        self.skip_btn.pack(side="left")
        self.next_btn = ctk.CTkButton(
            bot, text="下一步", command=self._validate_and_next, width=140,
            fg_color="#6366f1", hover_color="#4f46e5",
        )
        self.next_btn.pack(side="right")
        self.back_btn = ctk.CTkButton(
            bot, text="上一步", command=self._prev_step,
            fg_color="gray30", width=100,
        )
        self.back_btn.pack(side="right", padx=(0, 8))

    # ── 渲染 ────────────────────────────────────────────────
    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.status_lbl.configure(text="")

    def _render_step(self) -> None:
        self._clear_body()
        self.title_lbl.configure(text=STEPS[self.current_step])
        self.step_lbl.configure(text=f"步骤 {self.current_step + 1} / {len(STEPS)}")
        self.back_btn.configure(state="disabled" if self.current_step == 0 else "normal")
        if self.current_step == len(STEPS) - 1:
            self.next_btn.configure(text="完成")
        else:
            self.next_btn.configure(text="下一步")

        # 派给具体步骤
        [
            self._render_login,
            self._render_vault,
            self._render_anki,
            self._render_ai,
        ][self.current_step]()

    # ── 第 1 步：登录 ──────────────────────────────────────
    def _render_login(self) -> None:
        cfg = self._effective_cfg()
        token = (cfg.get("api_token") or "").strip()
        ctk.CTkLabel(
            self.body,
            text="客户端需要连接到 bwicarus.space 才能上传你的 dashboard 与历史。",
            wraplength=500, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        url_row = ctk.CTkFrame(self.body, fg_color="transparent")
        url_row.pack(fill="x", pady=4)
        ctk.CTkLabel(url_row, text="服务端地址", width=110, anchor="w").pack(side="left")
        self.url_entry = ctk.CTkEntry(url_row)
        self.url_entry.insert(0, cfg.get("server_url") or "https://bwicarus.space")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        if token:
            self.status_lbl.configure(
                text=f"✓ 已连接（token 末 4 位 …{token[-4:]}）。如需换账号点「重新登录」。",
                text_color="#22c55e",
            )
            ctk.CTkButton(
                self.body, text="重新登录（浏览器）",
                command=self._do_device_link, fg_color="#6366f1",
            ).pack(anchor="w", pady=(14, 0))
        else:
            self.status_lbl.configure(
                text="点「打开浏览器登录」会自动配置 token，无需手动复制。",
                text_color="gray70",
            )
            ctk.CTkButton(
                self.body, text="打开浏览器登录",
                command=self._do_device_link, fg_color="#6366f1",
                width=200, height=38,
            ).pack(anchor="w", pady=(14, 0))

    def _do_device_link(self) -> None:
        url = self.url_entry.get().strip().rstrip("/") or "https://bwicarus.space"
        self.updates["server_url"] = url
        self.status_lbl.configure(text="正在等浏览器授权…", text_color="gray70")
        self.next_btn.configure(state="disabled")
        self.skip_btn.configure(state="disabled")

        def task():
            try:
                token = do_device_link_flow(url, label="bwicarus-client", on_status=self.log)
            except Exception as e:
                self._after(lambda: self.status_lbl.configure(
                    text=f"✗ 登录失败：{e}", text_color="#f87171"))
                self._after(lambda: self.next_btn.configure(state="normal"))
                self._after(lambda: self.skip_btn.configure(state="normal"))
                return
            self.updates["api_token"] = token
            self._after(lambda: self.status_lbl.configure(
                text=f"✓ 已连接。token 已保存，可点「下一步」。", text_color="#22c55e"))
            self._after(lambda: self.next_btn.configure(state="normal"))
            self._after(lambda: self.skip_btn.configure(state="normal"))

        threading.Thread(target=task, daemon=True).start()

    # ── 第 2 步：vault ───────────────────────────────────────
    def _render_vault(self) -> None:
        cfg = self._effective_cfg()
        ctk.CTkLabel(
            self.body,
            text="选择你的 Obsidian Vault 根目录（含 .obsidian 子目录的那个）。",
            wraplength=500, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        guess = cfg.get("vault_path") or guess_vault() or ""
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text="Vault 路径", width=110, anchor="w").pack(side="left")
        self.vault_entry = ctk.CTkEntry(row)
        self.vault_entry.insert(0, guess)
        self.vault_entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        ctk.CTkButton(
            row, text="…", width=34, fg_color="gray30",
            command=self._pick_vault,
        ).pack(side="right")

        ctk.CTkButton(
            self.body, text="检测", command=self._check_vault, width=120,
        ).pack(anchor="w", pady=(14, 0))

        if guess:
            self.status_lbl.configure(
                text=f"已自动探测到候选路径，可直接点「检测」确认。",
                text_color="gray70",
            )

    def _pick_vault(self) -> None:
        d = filedialog.askdirectory(title="选择 Obsidian Vault 目录")
        if d:
            self.vault_entry.delete(0, "end")
            self.vault_entry.insert(0, d)

    def _check_vault(self) -> None:
        p = Path(self.vault_entry.get().strip())
        if not p.exists() or not p.is_dir():
            self.status_lbl.configure(text="✗ 路径不存在或不是目录。", text_color="#f87171")
            return
        if not (p / ".obsidian").exists():
            self.status_lbl.configure(
                text="⚠ 该目录里没有 .obsidian 子目录，可能不是 vault。仍可继续。",
                text_color="#fbbf24",
            )
        else:
            self.status_lbl.configure(
                text=f"✓ 找到 Obsidian Vault（{sum(1 for _ in p.glob('*.md'))} 个 .md）。",
                text_color="#22c55e",
            )

    # ── 第 3 步：Anki ────────────────────────────────────────
    def _render_anki(self) -> None:
        cfg = self._effective_cfg()
        anki = cfg.get("anki") or {}
        ctk.CTkLabel(
            self.body,
            text="客户端通过 AnkiConnect 读取 Anki 卡片状态。请填 anki.exe 路径并测试连接。",
            wraplength=500, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        row1 = ctk.CTkFrame(self.body, fg_color="transparent")
        row1.pack(fill="x", pady=4)
        ctk.CTkLabel(row1, text="anki.exe", width=110, anchor="w").pack(side="left")
        self.anki_entry = ctk.CTkEntry(row1)
        self.anki_entry.insert(0, anki.get("exe_path", ""))
        self.anki_entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        ctk.CTkButton(
            row1, text="…", width=34, fg_color="gray30",
            command=self._pick_anki_exe,
        ).pack(side="right")

        row2 = ctk.CTkFrame(self.body, fg_color="transparent")
        row2.pack(fill="x", pady=4)
        ctk.CTkLabel(row2, text="AnkiConnect", width=110, anchor="w").pack(side="left")
        self.anki_url_entry = ctk.CTkEntry(row2)
        self.anki_url_entry.insert(0, anki.get("connect_url", "http://localhost:8765"))
        self.anki_url_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        btn_row = ctk.CTkFrame(self.body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(btn_row, text="启动 Anki", command=self._launch_anki,
                      width=120, fg_color="gray30").pack(side="left")
        ctk.CTkButton(btn_row, text="测试连接", command=self._test_anki,
                      width=120).pack(side="left", padx=(8, 0))

    def _pick_anki_exe(self) -> None:
        f = filedialog.askopenfilename(
            title="选择 anki.exe",
            filetypes=[("可执行", "*.exe"), ("所有文件", "*.*")],
        )
        if f:
            self.anki_entry.delete(0, "end")
            self.anki_entry.insert(0, f)

    def _launch_anki(self) -> None:
        from runner import open_program  # type: ignore
        path = self.anki_entry.get().strip()
        if not path:
            self.status_lbl.configure(text="✗ 先填 anki.exe 路径。", text_color="#f87171")
            return
        ok, msg = open_program(path)
        self.status_lbl.configure(
            text=("✓ " if ok else "✗ ") + msg,
            text_color="#22c55e" if ok else "#f87171",
        )

    def _test_anki(self) -> None:
        url = self.anki_url_entry.get().strip()
        ok, msg = AnkiClient(url).ping()
        self.status_lbl.configure(
            text=("✓ " if ok else "✗ ") + msg,
            text_color="#22c55e" if ok else "#f87171",
        )

    # ── 第 4 步：AI ──────────────────────────────────────────
    def _render_ai(self) -> None:
        cfg = self._effective_cfg()
        ctk.CTkLabel(
            self.body,
            text="AI 用于分析笔记 / 截图问答。选一个后端并填 key（CLI 类后端不需要 key，会调本机命令）。",
            wraplength=500, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        backend = cfg.get("ai_backend") or "claude_cli"
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text="AI 后端", width=110, anchor="w").pack(side="left")
        self.ai_backend_var = ctk.StringVar(value=backend)
        ctk.CTkOptionMenu(
            row, values=list_backends(), variable=self.ai_backend_var,
            command=lambda _v: self._render_ai_settings(),
        ).pack(side="left", padx=(8, 0))

        self.ai_settings_frame = ctk.CTkFrame(self.body, fg_color="transparent")
        self.ai_settings_frame.pack(fill="x", pady=(8, 0))
        self._render_ai_settings()

        ctk.CTkButton(
            self.body, text="测试 AI", command=self._test_ai, width=120,
        ).pack(anchor="w", pady=(14, 0))

    def _render_ai_settings(self) -> None:
        for w in self.ai_settings_frame.winfo_children():
            w.destroy()
        self._ai_setting_entries: dict = {}
        backend = self.ai_backend_var.get()
        cfg = self._effective_cfg()
        cur = (cfg.get("ai") or {}).get(backend) or backend_default_settings(backend)
        for key, label, secret in backend_setting_fields(backend):
            row = ctk.CTkFrame(self.ai_settings_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=label, width=110, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, show="•" if secret else None)
            entry.insert(0, str(cur.get(key, "")))
            entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self._ai_setting_entries[key] = entry

    def _test_ai(self) -> None:
        backend = self.ai_backend_var.get()
        settings = {k: e.get().strip() for k, e in self._ai_setting_entries.items()}
        try:
            ad = make_backend(backend, settings)
        except Exception as e:
            self.status_lbl.configure(text=f"✗ 创建 backend 失败：{e}", text_color="#f87171")
            return
        self.status_lbl.configure(text="测试中…", text_color="gray70")

        def task():
            ok, msg = ad.ping()
            self._after(lambda: self.status_lbl.configure(
                text=("✓ " if ok else "✗ ") + msg,
                text_color="#22c55e" if ok else "#f87171",
            ))

        threading.Thread(target=task, daemon=True).start()

    # ── 校验 + 翻页 ──────────────────────────────────────────
    def _validate_and_next(self) -> None:
        # 校验当前步：抓输入到 self.updates
        if self.current_step == 0:
            url = self.url_entry.get().strip().rstrip("/")
            if url:
                self.updates["server_url"] = url
            # token 已在登录回调里写入；不强制
        elif self.current_step == 1:
            v = self.vault_entry.get().strip()
            if v:
                self.updates["vault_path"] = v
        elif self.current_step == 2:
            anki_cfg = dict(self._effective_cfg().get("anki") or {})
            ev = self.anki_entry.get().strip()
            url = self.anki_url_entry.get().strip()
            if ev:
                anki_cfg["exe_path"] = ev
            if url:
                anki_cfg["connect_url"] = url
            self.updates["anki"] = anki_cfg
        elif self.current_step == 3:
            backend = self.ai_backend_var.get()
            ai_all = dict(self._effective_cfg().get("ai") or {})
            ai_all[backend] = {k: e.get().strip() for k, e in self._ai_setting_entries.items()}
            self.updates["ai_backend"] = backend
            self.updates["ai"] = ai_all

        self._next_step()

    def _next_step(self) -> None:
        if self.current_step >= len(STEPS) - 1:
            self._finish()
            return
        self.current_step += 1
        self._render_step()

    def _prev_step(self) -> None:
        if self.current_step > 0:
            self.current_step -= 1
            self._render_step()

    def _finish(self) -> None:
        self.updates["onboarding_completed"] = True
        try:
            self.done(self.updates)
        finally:
            try:
                self.win.destroy()
            except Exception:
                pass

    def _on_close(self) -> None:
        if messagebox.askyesno(
            "退出向导",
            "确定要退出？已填的字段不会保存（除已经登录拿到的 token 外）。\n"
            "可以在主窗口的「基础」tab 单独完成各项配置。",
            parent=self.win,
        ):
            # 保留已经拿到的 token / server_url，其他丢弃
            partial = {k: v for k, v in self.updates.items() if k in ("server_url", "api_token")}
            partial["onboarding_completed"] = True
            try:
                self.done(partial)
            finally:
                self.win.destroy()

    # ── 工具 ─────────────────────────────────────────────────
    def _effective_cfg(self) -> dict:
        merged = dict(self.cfg)
        merged.update(self.updates)
        return merged

    def _after(self, fn: Callable) -> None:
        try:
            self.master.after(0, fn)
        except Exception:
            pass


def run_wizard(master: ctk.CTk, cfg: dict, *, on_log=None) -> dict:
    """阻塞调用：返回 cfg 改动 dict，应直接 cfg.update(返回值) 并 save。"""
    result: dict = {}

    def done(updates: dict) -> None:
        result.update(updates)

    OnboardingWizard(master, cfg, done_callback=done, on_log=on_log)
    # 不阻塞：wizard 是 modeless，让主程序继续；返回时空字典，由 done 异步填入
    return result
