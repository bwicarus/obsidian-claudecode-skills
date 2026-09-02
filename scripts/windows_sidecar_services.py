# -*- coding: utf-8 -*-
"""Windows 侧的独立服务守护（2026-09-02，Pi 整体退出）。

Pi 上除 webapp(Flask) 之外还有几个 systemd 独立服务，App/手表/iPad 直连它们：
  voice-rt      voice_realtime_relay.py   127.0.0.1:8767   豆包实时语音中继(App wss /voice-rt)
  watch-voice   watch_voice_relay.py      127.0.0.1:8768   Apple Watch ↔ Windows 桥的语音中继
  rbi           rbi_server.py             127.0.0.1:8769   远程浏览器(iPad 看 PC 上的真 Chrome)
  mcp           mcp_server.py --http 8766 127.0.0.1:8766   MCP 门面(tailscale serve /mcp)
Flask 本身由 local_supervisor.pyw 托管；这里只管这三个。每个子进程崩了 5s 后拉起，
日志落 webapp-data/sidecar-<name>.log。环境从工作树根的 .env.local 读（与 Flask 同一份）。

用法：pythonw scripts/windows_sidecar_services.py   （开机自启：HKCU Run "BwicarusSidecars"）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT") or Path(__file__).resolve().parents[1])
DEPLOY = ROOT / "_server_deploy"
ENV_FILE = ROOT / ".env.local"
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")

SERVICES = {
    "voice-rt": [PYTHON, str(DEPLOY / "voice_realtime_relay.py")],
    "watch-voice": [PYTHON, str(DEPLOY / "watch_voice_relay.py")],
    "rbi": [PYTHON, str(DEPLOY / "rbi_server.py")],
    # MCP 门面(外部 agent 控制整个 App;认 ~/.config/mcp-webapp-token → webapp api_tokens)
    "mcp": [PYTHON, str(DEPLOY / "mcp_server.py"), "--http", "8766"],
}


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    env.setdefault("CLAUDE_PROJECT", str(ROOT))
    env.setdefault("WEBAPP_DATA", str(ROOT / "webapp-data"))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> None:
    env = load_env()
    data_dir = Path(env["WEBAPP_DATA"])
    data_dir.mkdir(parents=True, exist_ok=True)
    procs: dict[str, subprocess.Popen | None] = {name: None for name in SERVICES}
    logs: dict[str, object] = {}
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    while True:
        for name, cmd in SERVICES.items():
            proc = procs[name]
            if proc is not None and proc.poll() is None:
                continue
            log = logs.get(name)
            if log is None:
                log = open(data_dir / f"sidecar-{name}.log", "a", encoding="utf-8", buffering=1)
                logs[name] = log
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {name}"
                      + (f"（上次退出码 {proc.returncode}）" if proc is not None else "") + " =====\n")
            try:
                procs[name] = subprocess.Popen(
                    cmd, cwd=str(DEPLOY), env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            except Exception as exc:  # noqa: BLE001
                log.write(f"[sidecar] 启动失败: {exc}\n")
                procs[name] = None
        time.sleep(5)


if __name__ == "__main__":
    main()
