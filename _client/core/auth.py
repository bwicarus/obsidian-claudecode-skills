"""客户端设备授权流程（loopback OAuth-style callback）。

入口 do_device_link_flow(server_url, label) 阻塞返回 token：
    1. 监听 127.0.0.1:0（OS 分配空闲端口）
    2. 浏览器打开 <server>/auth/device-link?cb=...&state=...&label=...
    3. 用户登录 + 点"允许"后，server 跳到 cb_url?token=...&state=...
    4. 本地 HTTPServer 收到，校验 state，返回 token
"""
from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

_HTML_OK = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>客户端已连接</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#f0f2f5;color:#1f2937}
.card{background:#fff;padding:36px;border-radius:14px;box-shadow:0 2px 16px rgba(0,0,0,.08);
max-width:380px;text-align:center}.h{font-size:42px;margin-bottom:8px}
h1{font-size:18px;margin-bottom:10px}p{font-size:13px;color:#6b7280;line-height:1.6}
</style></head><body><div class="card">
<div class="h">✓</div><h1>客户端已成功连接</h1>
<p>你可以关闭此页，回到客户端继续操作。</p></div></body></html>"""

_HTML_ERR = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>授权失败</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#f0f2f5;color:#1f2937}
.card{background:#fff;padding:36px;border-radius:14px;box-shadow:0 2px 16px rgba(0,0,0,.08);
max-width:380px;text-align:center}.h{font-size:42px;margin-bottom:8px;color:#dc2626}
h1{font-size:18px;margin-bottom:10px}p{font-size:13px;color:#6b7280;line-height:1.6}
</style></head><body><div class="card">
<div class="h">✗</div><h1>授权失败</h1>
<p>{msg}</p></div></body></html>"""


def do_device_link_flow(
    server_url: str,
    *,
    label: str = "bwicarus-client",
    timeout_sec: int = 300,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """阻塞调用，成功返回 token；失败抛 RuntimeError。"""
    log = on_status or (lambda _msg: None)
    server_url = server_url.rstrip("/")
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 静音，避免在无控制台环境出问题

        def do_GET(self):
            try:
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                tok = (params.get("token") or [""])[0]
                st  = (params.get("state") or [""])[0]
                err = (params.get("error") or [""])[0]
                if err:
                    result["error"] = err
                    self._send(400, _HTML_ERR.format(msg=f"用户拒绝或出错：{err}"))
                    return
                if not tok or st != state:
                    result["error"] = "state mismatch"
                    self._send(400, _HTML_ERR.format(msg="state 校验失败（可能是过期或被劫持的回调）"))
                    return
                result["token"] = tok
                self._send(200, _HTML_OK)
            except Exception as e:
                result["error"] = str(e)
                self._send(500, _HTML_ERR.format(msg=str(e)))

        def _send(self, code: int, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    cb_url = f"http://127.0.0.1:{port}/auth-cb"

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    log(f"本地回调监听 {cb_url}")

    qs = urllib.parse.urlencode({"cb": cb_url, "state": state, "label": label})
    auth_url = f"{server_url}/auth/device-link?{qs}"
    log(f"打开浏览器：{auth_url}")
    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except Exception as e:
        log(f"浏览器自动打开失败，请手动复制：{auth_url} ({e})")

    deadline = time.time() + timeout_sec
    try:
        while time.time() < deadline:
            if "token" in result or "error" in result:
                break
            time.sleep(0.2)
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    if "token" in result:
        log("已收到 token")
        return result["token"]
    if "error" in result:
        raise RuntimeError(f"授权失败：{result['error']}")
    raise RuntimeError("授权超时（5 分钟内未在浏览器完成）")
