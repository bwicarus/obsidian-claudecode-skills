"""客户端内置远程命令 HTTP server（替代旧 cmd_server.exe）。

监听 0.0.0.0:<port>，接收来自 iPad 快捷指令、Tailscale 等的 GET/POST 触发：

    GET  /run/<cmd>?key=...
    POST /run/<cmd>?key=...     (iPad 快捷指令默认 POST)
    GET  /list?key=...
    POST /qa?key=...            (iPad 截图注入；body 含 base64)

命令注册由 GUI 调用方提供 callbacks dict，例如：
    {"newnote": on_newnote, "upload-website": on_upload}

/qa 不自己处理 AI——只把截图转发到 qa_browser daemon (127.0.0.1:9091) 的
/api/inject-image，由 daemon 完成 HEIC→PNG 转换、写文件、注入 state、重置 session。
iPad 浏览器直接访问 daemon 端口（qa_browser 自带 qa_remote_access bind 0.0.0.0）
看完整对话页面。

API key: %LOCALAPPDATA%\\bwicarus-client\\cmd_server_key.txt
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from paths import state_path  # type: ignore

QA_DAEMON_URL = "http://127.0.0.1:9091"   # qa_browser.start_server_daemon 的常驻端口

# 新位置（统一在 %LOCALAPPDATA%\bwicarus-client\）
KEY_FILE = state_path("cmd_server_key.txt")
# 旧位置（兼容 0.9.x 之前 / 旧 cmd_server.exe 的写入位置）
_LEGACY_KEY_FILE = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "截图问答" / "cmd_server_key.txt"
)


def load_or_create_key() -> str:
    """优先读新位置；如果只有旧位置，把它迁移过来；都没有就生成新的。"""
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        try:
            txt = KEY_FILE.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except OSError:
            pass
    if _LEGACY_KEY_FILE.exists():
        try:
            txt = _LEGACY_KEY_FILE.read_text(encoding="utf-8").strip()
            if txt:
                KEY_FILE.write_text(txt, encoding="utf-8")
                return txt
        except OSError:
            pass
    key = secrets.token_hex(16)
    KEY_FILE.write_text(key, encoding="utf-8")
    return key


class CmdServer:
    """HTTPServer 跑在 daemon thread。提供 start/stop。"""

    def __init__(
        self,
        callbacks: dict[str, Callable[[], str]],
        *,
        port: int = 9090,
        bind: str = "0.0.0.0",
        api_key: str | None = None,
        on_log: Callable[[str], None] | None = None,
    ):
        self._callbacks = callbacks
        self.port = int(port)
        self.bind = bind
        self.api_key = api_key or load_or_create_key()
        self._on_log = on_log or (lambda _msg: None)
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            return True, f"已在运行 ({self.bind}:{self.port})"

        cb = self._callbacks
        api_key = self.api_key
        log = self._on_log

        class Handler(BaseHTTPRequestHandler):
            _post_body: bytes = b""

            def log_message(self, fmt, *args):
                try:
                    log(f"[cmd_server] {self.address_string()} {fmt % args}")
                except Exception:
                    pass

            def _parse_qs(self) -> dict:
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                params: dict[str, str] = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k] = urllib.parse.unquote_plus(v)
                return params

            def _auth(self, params: dict) -> bool:
                if self.headers.get("X-API-Key", "") == api_key:
                    return True
                return params.get("key") == api_key

            def _send(self, code: int, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def _handle(self) -> None:
                params = self._parse_qs()
                if not self._auth(params):
                    self._send(403, "forbidden")
                    return
                clean = self.path.split("?")[0].rstrip("/")
                if clean == "/list":
                    self._send(200, "\n".join(sorted(cb.keys())))
                    return
                if clean.startswith("/run/"):
                    name = clean[5:]
                    fn = cb.get(name)
                    if not fn:
                        self._send(404, f"unknown command: {name}\navailable: {', '.join(sorted(cb.keys()))}")
                        return
                    log(f"[cmd_server] /run/{name}")
                    try:
                        msg = fn() or "ok"
                    except Exception as e:
                        msg = f"error: {e}"
                    self._send(200, str(msg))
                    return
                if clean == "/qa":
                    self._handle_qa()
                    return
                self._send(404, "not found")

            def _handle_qa(self) -> None:
                """iPad 截图注入：转发到 qa_browser daemon 的 /api/inject-image。
                iPad 浏览器之后直接打开 daemon 端口看到截图 + 输入问题继续多轮对话。"""
                body_bytes = self._post_body or b""
                if not body_bytes:
                    self._send(400, "缺少 body（image_b64）")
                    return

                # 解析 body 提取 image_b64：JSON {image_b64} / 纯 base64 / data URL
                body_text = body_bytes.decode("utf-8", errors="replace").strip()
                image_b64 = ""
                if body_text.startswith("{"):
                    try:
                        data = json.loads(body_text)
                        image_b64 = (data.get("image_b64") or "").strip()
                    except json.JSONDecodeError:
                        image_b64 = body_text
                else:
                    image_b64 = (
                        body_text.split(",", 1)[1]
                        if body_text.startswith("data:")
                        else body_text
                    )

                if not image_b64:
                    self._send(400, "缺少 image_b64")
                    return

                payload = json.dumps({"image_b64": image_b64}, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    f"{QA_DAEMON_URL}/api/inject-image",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        resp = r.read().decode("utf-8", errors="replace")
                    log(f"[cmd_server] /qa 已注入 daemon: {resp[:120]}")
                except Exception as e:
                    log(f"[cmd_server] /qa daemon 注入失败: {e}")
                    self._send(502, f"qa-daemon 不可达: {e}")
                    return

                self._send(200, "截图已注入。在浏览器打开 qa-daemon 端口开始对话。")

            def do_GET(self):
                self._handle()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                self._post_body = b""
                if length > 0:
                    try:
                        self._post_body = self.rfile.read(length)
                    except Exception:
                        pass
                self._handle()

        try:
            self._server = HTTPServer((self.bind, self.port), Handler)
        except OSError as e:
            return False, f"无法监听 {self.bind}:{self.port}: {e}"

        def serve():
            try:
                self._server.serve_forever()
            except Exception:
                pass

        self._thread = threading.Thread(target=serve, daemon=True, name="cmd_server")
        self._thread.start()
        log(f"[cmd_server] 启动于 http://{self.bind}:{self.port}  key={self.api_key[:6]}…")
        return True, f"http://{self.bind}:{self.port}"

    def stop(self) -> None:
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        self._server = None
        self._thread = None
