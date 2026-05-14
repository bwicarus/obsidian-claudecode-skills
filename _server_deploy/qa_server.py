#!/usr/bin/env python3
"""服务器侧 iPad 截图问答 daemon + cmd_server 入口。

复用 `_client/core/qa_browser.py` + `cmd_server_thread.py`，加 server entry：

- qa_browser daemon 监听 0.0.0.0:9091 提供完整对话页 HTML/API（跟本地 ctrl+shift+q 一样）
- cmd_server 监听 0.0.0.0:9090 接 iPad POST `/qa` 注入截图 + `/run/<cmd>` 触发动作
- 两个端口受 Tailscale 网段限制（公网不开放，由 nginx 不反代这两个端口保护）
- API key 在 BWICARUS_APP_DIR/cmd_server_key.txt（首次启动自动生成）

服务器侧配置 `/root/claude/state/server-config.json` 覆盖默认值。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path("/root/claude")

# 让客户端 module 可 import
sys.path.insert(0, str(CLAUDE_DIR / "_client" / "core"))
sys.path.insert(0, str(CLAUDE_DIR / "scripts"))

# 让 paths.app_dir() 返回服务器侧位置（cmd_server_key.txt 等都放这）
os.environ.setdefault("BWICARUS_APP_DIR", str(CLAUDE_DIR / "state" / "qa-server-data"))

SERVER_CONFIG = CLAUDE_DIR / "state" / "server-config.json"

# 服务器端默认配置（首次启动会写到 server-config.json，之后用户改那个文件就行）
DEFAULT_CONFIG: dict = {
    "qa_vault_path": "/root/obsidian",
    "qa_index_dir": "/root/claude/index",
    "qa_anki_records_dir": "/root/claude/anki/records",
    "qa_exercises_subdir": "习题",
    "qa_wrong_subdir": "错题",
    "qa_remote_daemon": True,        # iPad 远程截图问答总开关
    "qa_remote_access": True,
    "ai_backend": "claude_cli",
    "ai": {
        "claude_cli": {"command": "/usr/bin/claude"},
        "codex_cli":  {"command": "/usr/bin/codex"},
        "ollama":     {"api_key": "", "model": "", "base_url": ""},
    },
    "anki": {
        "exe_path":     "/opt/anki-venv/bin/anki",
        "connect_url":  "http://127.0.0.1:8765",
        "auto_restart": False,        # AnkiConnect 不可达时重启 systemctl restart anki-headless
    },
    "auto_upload_after_register": False,
    "scheduled_register": {
        "enabled":      True,
        "time":         "04:00",
        "wake_anki":    True,
        "upload_after": True,
    },
}


def _deep_merge(a: dict, b: dict) -> dict:
    """把 b 深度合进 a（不修改原对象）。"""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_server_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if SERVER_CONFIG.exists():
        try:
            saved = json.loads(SERVER_CONFIG.read_text(encoding="utf-8"))
            cfg = _deep_merge(cfg, saved)
        except Exception as e:
            print(f"[qa_server] 读 config 失败: {e}", file=sys.stderr)
    return cfg


def save_server_config(cfg: dict) -> None:
    SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SERVER_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_cfg() -> dict:
    """qa_browser daemon 每次请求都通过它拿最新 cfg。"""
    return load_server_config()


# ── cmd_server 回调（iPad /run/<cmd> 触发）─────────────────────────────────

def _run_async(cmd: list[str]) -> str:
    """spawn 子进程，立即返回。"""
    log_dir = CLAUDE_DIR / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "qa_server_trigger.log"
    with log_file.open("a", encoding="utf-8") as logf:
        from datetime import datetime
        logf.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {cmd} ===\n")
        logf.flush()
        subprocess.Popen(
            cmd,
            cwd=str(CLAUDE_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return " ".join(cmd[:3]) + " 已启动"


def cb_register() -> str:
    return _run_async(["/usr/bin/python3", str(CLAUDE_DIR / "scripts" / "register_notes.py")])


def cb_daily() -> str:
    return _run_async(["/usr/bin/python3", str(CLAUDE_DIR / "scripts" / "daily_anki_status.py")])


def cb_upload() -> str:
    """触发 daily（包含 register + 仪表板部署 + AnkiWeb sync）。"""
    return cb_daily()


def cb_ankiweb_sync() -> str:
    """直接调 AnkiConnect sync。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8765",
            data=json.dumps({"action": "sync", "version": 6}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
            if data.get("error"):
                return f"sync 失败: {data['error']}"
            return "AnkiWeb 同步已触发"
    except Exception as e:
        return f"sync 异常: {e}"


CALLBACKS = {
    "register":     cb_register,
    "newnote":      cb_register,        # 兼容旧名
    "daily":        cb_daily,
    "upload":       cb_upload,
    "upload-website": cb_upload,        # 兼容旧名
    "ankiweb-sync": cb_ankiweb_sync,
}


def main() -> int:
    # 确保 config 文件存在
    if not SERVER_CONFIG.exists():
        save_server_config(DEFAULT_CONFIG)
        print(f"[qa_server] 已建默认 config: {SERVER_CONFIG}")

    import qa_browser  # noqa: E402
    import cmd_server_thread  # noqa: E402

    cfg = load_server_config()

    # 启 qa_browser daemon
    server = qa_browser.start_server_daemon(get_cfg, port=9091, bind="0.0.0.0")
    if not server:
        print("[qa_server] qa_browser daemon 启动失败", file=sys.stderr)
        return 1
    print("[qa_server] qa_browser daemon http://0.0.0.0:9091")

    # 启 cmd_server
    cs = cmd_server_thread.CmdServer(
        CALLBACKS,
        port=9090,
        bind="0.0.0.0",
        on_log=lambda m: print(f"[cmd_server] {m}"),
    )
    cs.start()
    print(f"[qa_server] cmd_server http://0.0.0.0:9090 (key={cs.api_key[:8]}...)")
    print(f"[qa_server] config: {SERVER_CONFIG}")
    print(f"[qa_server] vault: {cfg.get('qa_vault_path')}")
    print(f"[qa_server] callbacks: {list(CALLBACKS.keys())}")

    # 主线程长 sleep，子线程跑 server
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[qa_server] 退出")
        cs.stop()
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
