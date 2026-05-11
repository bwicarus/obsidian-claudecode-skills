"""
cmd_server.py — 本地命令触发服务

GET  /run/<cmd>?key=...        执行预定义命令，返回输出
POST /qa?key=...&text=问题     接收 base64 截图，Claude 处理后推送到网站
GET  /list?key=...             列出可用命令
"""

import json, os, subprocess, sys, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT     = 9090
API_KEY  = ""

KEY_FILE = Path(os.environ["LOCALAPPDATA"]) / "截图问答" / "cmd_server_key.txt"
PYTHON   = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
REGISTER = r"C:\claude\scripts\register_notes.py"
UPLOAD   = r"C:\claude\launchers\上传网页.py"

# 命令格式：name -> (args, cwd, async)
#   async=True 时立即返回，进程在后台运行（适合长任务，进度看任务监视窗口）
#   async=False 时同步等待并返回输出
COMMANDS = {
    "status":         (["C:\\claude\\bin\\status.bat"],         None,         False),
    "relay-start":    (["C:\\claude\\bin\\relay.bat", "start"], None,         False),
    "relay-stop":     (["C:\\claude\\bin\\relay.bat", "stop"],  None,         False),
    "relay-status":   (["C:\\claude\\bin\\relay.bat", "status"],None,         False),
    "newnote":        ([PYTHON, REGISTER],                       r"C:\claude", True),
    "upload-website": ([PYTHON, UPLOAD],                         r"C:\claude", True),
}


def load_or_create_key() -> str:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    import secrets
    key = secrets.token_hex(16)
    KEY_FILE.write_text(key)
    return key


def run_cmd(name: str) -> str:
    entry = COMMANDS.get(name)
    if not entry:
        return f"unknown command: {name}"
    args, cwd, is_async = entry
    if is_async:
        # 把异步进程的 stdout/stderr 写入日志，便于事后排查
        log_dir = Path(r"C:\claude\state\logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"async_{name}.log"
        try:
            log_f = log_path.open("ab")
            from datetime import datetime as _dt
            log_f.write(f"\n=== {_dt.now().isoformat(timespec='seconds')} START {' '.join(args)} ===\n".encode("utf-8"))
            log_f.flush()
            subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                cwd=cwd,
                stdout=log_f,
                stderr=log_f,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            # 不 close log_f；子进程持有句柄，结束时由系统释放
            return f"已启动 {name}，日志：{log_path}"
        except Exception as e:
            return f"error: {e}"
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=cwd, timeout=300,
        )
        return (r.stdout + r.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"error: {e}"


def parse_qs(path: str) -> dict:
    qs = path.split("?", 1)[1] if "?" in path else ""
    params = {}
    for part in qs.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = urllib.parse.unquote_plus(v)
    return params


def push_to_website(question: str, answer: str, image_b64: str):
    """把对话结果推送到 bwicarus.space/qa/"""
    sys.path.insert(0, str(Path(__file__).parent))
    import ai_client
    s = ai_client.load_settings()
    relay_url = s.get("relay_url", "")
    relay_key = s.get("relay_key", "")
    if not relay_url or not relay_key:
        print("[qa] relay_url/relay_key 未配置，跳过上传")
        return
    # relay_url 形如 https://bwicarus.space/chat/api，取根路径
    base = relay_url.split("/chat/api")[0].split("/qa")[0]
    payload = json.dumps({
        "question":  question,
        "answer":    answer,
        "image_b64": image_b64,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{base}/qa/update",
        data=payload,
        headers={"X-API-Key": relay_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[qa] 已上传到 {base}/qa/")
    except Exception as e:
        print(f"[qa] 上传失败: {e}")


def handle_qa(text: str, image_b64: str) -> str:
    """保存图片 → 调用 Claude → 推送网站 → 返回答案"""
    sys.path.insert(0, str(Path(__file__).parent))
    import ai_client

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    img_path = None
    if image_b64:
        try:
            img_data = base64.b64decode(image_b64)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", dir=TEMP_DIR, delete=False)
            tmp.write(img_data)
            tmp.close()
            img_path = tmp.name
        except Exception as e:
            print(f"[qa] 图片解码失败: {e}")

    prompt = text or "请分析这张截图的内容"
    if img_path:
        prompt = f"请读取图片 {img_path}，然后回答：{text or '请分析这张截图的内容'}"

    print(f"[qa] 开始处理：{prompt[:60]}...")
    answer = ai_client.claude_raw(prompt, first=True)
    print(f"[qa] 完成，回复 {len(answer)} 字")

    if img_path:
        try:
            Path(img_path).unlink(missing_ok=True)
        except Exception:
            pass

    push_to_website(text or "（截图）", answer, image_b64 or "")
    return answer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _auth(self, params: dict) -> bool:
        if self.headers.get("X-API-Key", "") == API_KEY:
            return True
        return params.get("key") == API_KEY

    def _send(self, code: int, body: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _handle_run_or_list(self, clean: str) -> bool:
        """处理 /run/<cmd> 和 /list；返回 True 表示已响应。"""
        if clean.startswith("/run/"):
            name = clean[5:].strip("/")
            print(f"[run] {name}")
            self._send(200, run_cmd(name))
            return True
        if clean == "/list":
            self._send(200, "\n".join(COMMANDS.keys()))
            return True
        return False

    def do_GET(self):
        params = parse_qs(self.path)
        if not self._auth(params):
            self._send(403, "forbidden")
            return
        clean = self.path.split("?")[0]
        if not self._handle_run_or_list(clean):
            self._send(404, "not found")

    def do_POST(self):
        params = parse_qs(self.path)
        if not self._auth(params):
            self._send(403, "forbidden")
            return
        clean = self.path.split("?")[0]
        # /run/<cmd> 和 /list 同时支持 POST（iPad 快捷指令默认是 POST）
        if self._handle_run_or_list(clean):
            return
        if clean == "/qa":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            # 支持 JSON {"image_b64": "..."} 或纯 base64 或 data URL
            if raw.startswith("{"):
                try:
                    body = json.loads(raw).get("image_b64", raw)
                except Exception:
                    body = raw
            else:
                body = raw
            if body.startswith("data:"):
                body = body.split(",", 1)[-1]
            # 转发给本地 remote-qa 服务器（port 5001）
            try:
                payload = json.dumps({"image_b64": body}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:5001/api/inject-image",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    result = r.read().decode()
                self._send(200, f"截图已注入，请打开 https://bwicarus.space/qa/ 查看\n{result}")
            except Exception as e:
                self._send(503, f"remote-qa 服务器未运行，请启动 截图问答.exe --server\n错误: {e}")
        else:
            self._send(404, "not found")


def main():
    global API_KEY
    API_KEY = load_or_create_key()
    print(f"[cmd_server] 启动于 http://0.0.0.0:{PORT}")
    print(f"[cmd_server] API Key: {API_KEY}")
    print(f"[cmd_server] Tailscale: http://100.99.9.124:{PORT}")
    print(f"[cmd_server] 截图问答: POST http://100.99.9.124:{PORT}/qa?key={API_KEY}&text=问题")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[cmd_server] 已停止")


if __name__ == "__main__":
    main()
