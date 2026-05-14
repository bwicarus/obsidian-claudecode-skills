"""控制面板（替代 Windows 客户端 EXE）

部署位置：/root/webapp/control.py
注册：app.py 末尾 `from control import register_control; register_control(app)`
      （必须在 `if __name__ == "__main__":` 之前）

提供 /control/ 网页 + /control/api/* 后台 API。
鉴权由 app.py 的 @before_request require_login_global 处理
（PROTECTED_PREFIXES 必须含 "/control"）。

源码在 git 仓库 _server_deploy/control.py，部署时 scp 到 /root/webapp/。
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from flask import jsonify, render_template, request

CLAUDE_DIR = Path("/root/claude")

# scripts/ 加入路径，让 config_schema 等业务模块能 import
sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
from config_schema import validate_partial  # noqa: E402
LAST_RUN = CLAUDE_DIR / "state" / "last_run.json"
AI_SETTINGS = CLAUDE_DIR / "state" / "ai-settings.json"
SERVER_CONFIG = CLAUDE_DIR / "state" / "server-config.json"
PYTHON = "/usr/bin/python3"
ANKI_URL = "http://127.0.0.1:8765"


def load_claude_env() -> dict:
    """合并 os.environ 和 /root/claude/.env"""
    env = os.environ.copy()
    env_file = CLAUDE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_systemd_status(unit: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() or "unknown"
    except Exception as e:
        return f"err:{e}"


def ankiconnect_version():
    body = json.dumps({"action": "version", "version": 6}).encode()
    try:
        req = urllib.request.Request(
            ANKI_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            return data.get("result")
    except Exception:
        return None


def get_ai_backend() -> str:
    if not AI_SETTINGS.exists():
        return "auto-claude"
    try:
        data = json.loads(AI_SETTINGS.read_text(encoding="utf-8"))
        return data.get("backend", "auto-claude")
    except Exception:
        return "auto-claude"


def get_last_run():
    if not LAST_RUN.exists():
        return None
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except Exception:
        return None


def _trigger_script(script: str, args=None) -> None:
    """异步 spawn 一个 script，立即返回。"""
    args = args or []
    log_dir = CLAUDE_DIR / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "webapp_trigger.log"
    with log_file.open("a", encoding="utf-8") as logf:
        from datetime import datetime
        logf.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {script} {args} ===\n")
        logf.flush()
        subprocess.Popen(
            [PYTHON, str(CLAUDE_DIR / "scripts" / script), *args],
            cwd=str(CLAUDE_DIR),
            env=load_claude_env(),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def register_control(app):
    """注册 control 相关路由到 Flask app。"""

    @app.route("/control/")
    def control_home():
        return render_template("control.html")

    @app.route("/control/api/status")
    def control_status():
        return jsonify({
            "ankiconnect_version": ankiconnect_version(),
            "anki_headless": get_systemd_status("anki-headless"),
            "xvfb_99": get_systemd_status("xvfb-99"),
            "obsidian_sync": get_systemd_status("obsidian-sync"),
            "qa_server": get_systemd_status("qa-server"),
            "tailscaled": get_systemd_status("tailscaled"),
            "ai_backend": get_ai_backend(),
            "last_run": get_last_run(),
        })

    @app.route("/control/api/config", methods=["GET", "POST"])
    def control_config():
        """GET 返回 server-config.json；POST 写入 + 重启 qa-server 让新 cfg 生效。"""
        if request.method == "GET":
            if not SERVER_CONFIG.exists():
                return jsonify({})
            try:
                return jsonify(json.loads(SERVER_CONFIG.read_text(encoding="utf-8")))
            except Exception as e:
                return jsonify({"_error": str(e)})

        # POST: schema 校验过滤后深度合并请求 JSON 到现有 config
        raw = request.get_json(silent=True) or {}
        if not isinstance(raw, dict):
            return jsonify({"ok": False, "msg": "需要 JSON 对象"}), 400

        # 过滤未声明字段 + 类型不匹配的字段（拼错字段名不再静默生效）
        new_cfg, errors = validate_partial(raw)

        # 加载现有 + deep merge
        existing = {}
        if SERVER_CONFIG.exists():
            try:
                existing = json.loads(SERVER_CONFIG.read_text(encoding="utf-8"))
            except Exception:
                pass

        def deep_merge(a, b):
            out = dict(a)
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = deep_merge(out[k], v)
                else:
                    out[k] = v
            return out

        merged = deep_merge(existing, new_cfg)
        SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        SERVER_CONFIG.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 重启 qa-server 让新 cfg 生效（qa_browser daemon 每次请求都重新 get_cfg() 其实不需要重启，
        # 但是 qa_remote_daemon 控制 bind 行为，所以保险起见重启）
        if "qa_remote_daemon" in new_cfg or "qa_vault_path" in new_cfg:
            subprocess.run(["systemctl", "restart", "qa-server"], check=False)

        msg = "配置已保存"
        if errors:
            msg += f"（{len(errors)} 个字段被拒）"
        return jsonify({
            "ok":      True,
            "msg":     msg,
            "config":  merged,
            "errors":  errors,   # 前端可选展示
        })

    @app.route("/control/api/trigger-log")
    def control_trigger_log():
        """返回 webapp_trigger.log 末 N 行（追踪 trigger 子进程的实时输出）。"""
        log_file = CLAUDE_DIR / "state" / "logs" / "webapp_trigger.log"
        lines = int(request.args.get("lines", 80))
        if not log_file.exists():
            return jsonify({"lines": [], "size": 0})
        try:
            with log_file.open("rb") as f:
                # 读末尾 ~64KB（足够拿到 80 行）
                f.seek(0, 2)
                size = f.tell()
                read_bytes = min(size, 65536)
                f.seek(size - read_bytes)
                data = f.read().decode("utf-8", errors="replace")
            log_lines = data.splitlines()[-lines:]
            return jsonify({"lines": log_lines, "size": size})
        except Exception as e:
            return jsonify({"lines": [f"读 log 失败: {e}"], "size": 0})

    @app.route("/control/api/trigger/<action>", methods=["POST"])
    def control_trigger(action):
        if action == "register":
            _trigger_script("register_notes.py")
            return jsonify({"ok": True, "msg": "register_notes 已启动（后台跑，日志在 state/logs/webapp_trigger.log）"})
        elif action == "daily":
            _trigger_script("daily_anki_status.py")
            return jsonify({"ok": True, "msg": "daily 流程已启动（看 state/last_run.json 实时进度）"})
        elif action == "anki-restart":
            subprocess.run(["systemctl", "restart", "anki-headless"], check=False)
            return jsonify({"ok": True, "msg": "anki-headless 已重启（等 20s AnkiConnect 上线）"})
        elif action == "ankiweb-sync":
            body = json.dumps({"action": "sync", "version": 6}).encode()
            try:
                req = urllib.request.Request(
                    ANKI_URL, data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                    if data.get("error"):
                        return jsonify({"ok": False, "msg": f"sync 失败: {data['error']}"})
                    return jsonify({"ok": True, "msg": "AnkiWeb 同步已触发"})
            except Exception as e:
                return jsonify({"ok": False, "msg": f"sync 异常: {e}"})
        elif action == "switch-ai":
            data = request.get_json(silent=True) or {}
            target = data.get("target", "claude")
            if target not in ("claude", "gpt"):
                return jsonify({"ok": False, "msg": "target 必须是 claude 或 gpt"}), 400
            r = subprocess.run(
                [PYTHON, str(CLAUDE_DIR / "scripts" / "service_switch.py"), "switch", target],
                capture_output=True, text=True, env=load_claude_env(), timeout=10,
            )
            return jsonify({"ok": r.returncode == 0, "msg": (r.stdout or r.stderr).strip()})
        else:
            return jsonify({"ok": False, "msg": f"未知 action: {action}"}), 400
