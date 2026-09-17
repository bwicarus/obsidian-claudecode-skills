# -*- coding: utf-8 -*-
"""ReaderPC 的本机界面服务器（127.0.0.1:43132）：静态页面 + /api/* + 语音核心代理 /voice/*。

设计：ReaderPC 主进程仍以 tk 根做调度（隐藏窗口），这里只是一个后台线程里的 stdlib HTTP 服务器。
所有要碰 tk 状态的动作都经 `window.root.after(0, …)` 投递回 UI 线程；读状态只读线程安全快照或落盘的状态文件。
界面本身由 pywebview（Edge WebView2）打开同一地址；iPad 经 Tailscale 也能开。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

UI_HOST = "127.0.0.1"
UI_PORT = int(os.environ.get("BW_READERPC_UI_PORT", "43132"))
VOICE_CORE_URL = os.environ.get("BW_VOICE_CORE_URL", "http://127.0.0.1:43131")


def ui_directory() -> Path:
    """打包后 PyInstaller 把 readerpc_ui/ 放在 _MEIPASS 下；源码运行时就在本文件旁边。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "readerpc_ui"


class ReaderPCApi:
    """window：ReaderPCWindow（可为 None 做自检）。所有回调都必须能在任意线程调用。"""

    def __init__(self, *, window: Any, local_root: Path, bridge_runtime: Path, status_file: Path,
                 preferences_file: Path, log_file: Path, version: str,
                 get_labels: Callable[[], dict[str, Any]],
                 get_prefs: Callable[[], dict[str, Any]], set_prefs: Callable[[dict[str, Any]], dict[str, Any]],
                 run_action: Callable[[str], dict[str, Any]]):
        self.window = window
        self.local_root = local_root
        self.bridge_runtime = bridge_runtime
        self.status_file = status_file
        self.preferences_file = preferences_file
        self.log_file = log_file
        self.version = version
        self.get_labels = get_labels
        self.get_prefs = get_prefs
        self.set_prefs = set_prefs
        self.run_action = run_action
        self.started_at = time.time()
        self.httpd: ThreadingHTTPServer | None = None

    # ---------- 生命周期 ----------
    def start(self) -> str:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                api.handle(self, "GET")

            def do_POST(self):
                api.handle(self, "POST")

        self.httpd = ThreadingHTTPServer((UI_HOST, UI_PORT), Handler)
        threading.Thread(target=self.httpd.serve_forever, name="readerpc-ui-http", daemon=True).start()
        return f"http://{UI_HOST}:{UI_PORT}/"

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    # ---------- 路由 ----------
    def handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        u = urlparse(h.path)
        try:
            if u.path.startswith("/voice/") or u.path == "/voice":
                body, code = self.proxy_voice(u.path[len("/voice"):] or "/", u.query, method, self.read_body(h))
                return self.send_raw(h, code, body)
            if method == "GET":
                if u.path in ("/", "/index.html"):
                    return self.send_file(h, ui_directory() / "index.html")
                if u.path.startswith("/static/"):
                    return self.send_file(h, ui_directory() / u.path[len("/static/"):])
                if u.path == "/api/status":
                    return self.send_json(h, self.status())
                if u.path == "/api/prefs":
                    return self.send_json(h, {"prefs": self.get_prefs()})
                if u.path == "/api/snapshot":
                    return self.send_json(h, self.snapshot())
                if u.path == "/api/log":
                    n = int(parse_qs(u.query).get("lines", ["200"])[0])
                    return self.send_json(h, {"lines": self.tail(self.log_file, n)})
                if u.path == "/api/services":
                    st = self.read_status_file()
                    return self.send_json(h, {"services": st.get("services") or []})
                if u.path == "/api/notifications":
                    return self.send_json(h, self.notifications())
                if u.path == "/api/trace":
                    # 链路：语音侧与文字侧各自被注入/生成/查看/执行了什么（用户 2026-09-16）。
                    # 只读四份落盘文件，不碰执行链路；数据组装在 voice_trace 里。
                    q = parse_qs(u.query)
                    return self.send_json(h, self.trace(
                        limit=int(q.get("limit", ["120"])[0]),
                        days=float(q.get("days", ["7"])[0]),
                        thread=(q.get("thread", [""])[0] or None)))
                if u.path == "/api/ink-image":
                    # 链路页点开「插进那一轮的笔迹图」（用户 2026-09-17）。
                    # 只读、只发 ink-images 目录里的文件；文件名白名单化，
                    # 免得这个口变成任意读文件。
                    name = parse_qs(u.query).get("name", [""])[0]
                    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
                    # ⚠ 2026-09-17：桥改成「笔迹变化即抓、落 ink-standby 待命」之后，
                    #   这里还在读旧的 ink-images —— 链路页的缩略图于是全是 404。
                    #   同一个目录名在本仓库只该有一处写、一处读。
                    f = (Path.home() / "bw-computer-voice-bridge" / "runtime"
                         / "ink-standby" / safe)
                    if not safe or safe != name or not f.is_file():
                        return self.send_json(h, {"ok": False, "msg": "no such image"}, 404)
                    data = f.read_bytes()
                    h.send_response(200)
                    h.send_header("Content-Type",
                                  "image/png" if safe.endswith(".png") else "image/jpeg")
                    h.send_header("Content-Length", str(len(data)))
                    h.send_header("Cache-Control", "max-age=86400")
                    h.end_headers()
                    h.wfile.write(data)
                    return None
                if u.path == "/api/tool":
                    # 点开一个工具/skill 看详情：简介、参数表、流程文件（用户 2026-09-16）
                    return self.send_json(h, self.tool_detail(
                        parse_qs(u.query).get("name", [""])[0]))
                return self.send_json(h, {"ok": False, "msg": "no such path"}, 404)
            body = self.read_body(h)
            data = json.loads(body or b"{}") if body else {}
            if u.path == "/api/prefs":
                return self.send_json(h, self.set_prefs(data if isinstance(data, dict) else {}))
            if u.path.startswith("/api/action/"):
                return self.send_json(h, self.run_action(u.path[len("/api/action/"):]))
            return self.send_json(h, {"ok": False, "msg": "no such path"}, 404)
        except Exception as e:  # 界面服务器绝不能把主程序拖垮
            try:
                self.send_json(h, {"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    # ---------- 数据 ----------
    def trace(self, limit: int = 120, days: float = 7.0, thread: str | None = None) -> dict[str, Any]:
        """链路时间轴 + 工具统计。失败不能让界面白屏 —— 出错也返回结构完整的空壳。"""
        try:
            import voice_trace  # noqa: WPS433 —— 只有这一个入口用它，放函数里免得拖慢启动
            return voice_trace.build(limit=limit, days=days, thread=thread)
        except Exception as e:  # noqa: BLE001
            return {"contract": "voice-trace/1", "rows": [], "tools": [],
                    "error": "%s: %s" % (type(e).__name__, e)}


    def tool_detail(self, name: str) -> dict[str, Any]:
        """一个工具/skill 的详情。出错也返回完整结构 —— 面板不能白屏。"""
        try:
            import voice_trace  # noqa: WPS433
            return voice_trace.tool_detail(name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "name": name, "error": "%s: %s" % (type(e).__name__, e)}

    def status(self) -> dict[str, Any]:
        return {"version": self.version, "uptimeSeconds": round(time.time() - self.started_at), "pid": os.getpid(),
                "labels": self.get_labels(), "status": self.read_status_file(), "prefs": self.get_prefs(),
                "uiUrl": f"http://{UI_HOST}:{UI_PORT}/", "voiceCoreUrl": VOICE_CORE_URL}

    def read_status_file(self) -> dict[str, Any]:
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def snapshot(self) -> dict[str, Any]:
        js: dict[str, Any] = {}
        md = ""
        try:
            js = json.loads((self.bridge_runtime / "reader-context-snapshot.json").read_text(encoding="utf-8"))
        except Exception as e:
            js = {"error": str(e)}
        try:
            md = (self.bridge_runtime / "reader-context-live.md").read_text(encoding="utf-8")
        except Exception:
            md = ""
        return {"json": js, "md": md, "mdHash": hashlib.sha1(md.encode("utf-8")).hexdigest()[:12]}

    def notifications(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, path in (("routing", self.local_root / "notification-routing.json"), ("pushBinding", self.local_root / "codex-push-binding.json"),
                          ("binding", Path.home() / ".codex" / "voice-thread-binding.json")):
            try:
                out[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                out[key] = None
        try:
            import codex_channel  # 本地模块：通知通道选择（真相在桥 runtime 的共享文件里）
            out["channel"] = codex_channel.read_choice()
        except Exception as e:
            out["channel"] = {"error": str(e)}
        return out

    @staticmethod
    def tail(path: Path, n: int) -> list[str]:
        try:
            data = path.read_bytes()[-200_000:]
        except Exception:
            return []
        return data.decode("utf-8", errors="replace").splitlines()[-n:]

    # ---------- 语音核心代理 ----------
    @staticmethod
    def proxy_voice(path: str, query: str, method: str, body: bytes | None) -> tuple[bytes, int]:
        url = VOICE_CORE_URL + path + (("?" + query) if query else "")
        req = urllib.request.Request(url, data=body if method == "POST" else None, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=150) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as e:
            return e.read(), e.code
        except Exception as e:
            return json.dumps({"ok": False, "msg": f"语音核心不可达: {e}", "runnerDown": True}, ensure_ascii=False).encode("utf-8"), 503

    # ---------- 发送 ----------
    @staticmethod
    def read_body(h: BaseHTTPRequestHandler) -> bytes | None:
        n = int(h.headers.get("Content-Length") or 0)
        return h.rfile.read(n) if n else None

    @staticmethod
    def send_raw(h: BaseHTTPRequestHandler, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)

    def send_json(self, h: BaseHTTPRequestHandler, obj: Any, code: int = 200) -> None:
        self.send_raw(h, code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def send_file(self, h: BaseHTTPRequestHandler, path: Path) -> None:
        try:
            data = path.read_bytes()
        except Exception:
            return self.send_json(h, {"ok": False, "msg": f"missing {path.name}"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_raw(h, 200, data, ctype)


def open_ui_window(url: str, title: str = "ReaderPC") -> int:
    """`ReaderPC-Server.exe --ui`：单独一个进程开 WebView2 窗口指向本机界面。主进程不碰 webview 的事件循环。"""
    import webview  # 延迟导入：主进程（托盘）不需要它

    webview.create_window(title, url, width=1180, height=800, min_size=(820, 560))
    webview.start(gui="edgechromium", private_mode=False)
    return 0
