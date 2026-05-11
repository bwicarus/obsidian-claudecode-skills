r"""
service_switch.py — AI 后端切换 + 本地服务管理

架构：项目只有一个根目录 C:\claude。AI 后端切换仅改写 settings.json，
ai_client.ask() 每次调用都读取该文件，因此切换不需要重启 cmd_server / 截图问答 / relay。

命令：
    switch <claude|gpt>  写入 settings.json，并在服务未启动时拉起
    status               显示 settings 中的后端 + 服务监听情况
    stop-all             停掉 cmd_server / 截图问答 / relay
    start                启动 cmd_server / 截图问答 / relay（settings 不动）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PYTHON = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(r"C:\claude")

CMD_SERVER_PORT = 9090
REMOTE_QA_PORT  = 5001
TUNNEL_MATCH    = "-R 5001:127.0.0.1:5001 root@bwicarus.space"
SETTINGS_FILE   = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "截图问答" / "settings.json"

BACKEND_SETTINGS = {
    "claude": {"backend": "auto-claude", "model": ""},
    "gpt":    {"backend": "codex",       "model": "gpt-5.5"},
}


# ── PowerShell / Process helpers ─────────────────────────────────────────────

def _ps(script: str) -> subprocess.CompletedProcess:
    utf8_prefix = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", utf8_prefix + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=20,
    )


def _run_hidden(cmd: list[str], cwd: Path | None = None) -> subprocess.Popen:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return subprocess.Popen(
        cmd, cwd=str(cwd) if cwd else None,
        startupinfo=si,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _proc_rows() -> list[dict[str, str]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    r = _ps(script)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    data = json.loads(r.stdout)
    if isinstance(data, dict):
        data = [data]
    return [{k: "" if v is None else str(v) for k, v in row.items()} for row in data]


# ── 进程匹配规则 ─────────────────────────────────────────────────────────────

_PROJECT_LOWER = str(PROJECT).lower()

SERVICE_MARKERS = (
    r"\scripts\cmd_server.py",
    r"\launchers\cmd_server.py",
    r"\launchers\dist\cmd_server.exe",
    r"\launchers\截图问答.py",
    r"\launchers\dist\截图问答.exe",
)

RELAY_MARKERS = (
    r"\launchers\relay.py",
    r"\launchers\dist\relay.exe",
)


def _matches_service(row: dict[str, str]) -> bool:
    hay = (row.get("CommandLine", "") + "\n" + row.get("ExecutablePath", "")).lower()
    if _PROJECT_LOWER not in hay:
        return False
    return any(m.lower() in hay for m in SERVICE_MARKERS)


def _matches_relay(row: dict[str, str]) -> bool:
    name = row.get("Name", "").lower()
    cmd  = row.get("CommandLine", "")
    hay  = (cmd + "\n" + row.get("ExecutablePath", "")).lower()
    if name == "ssh.exe" and TUNNEL_MATCH.lower() in cmd.lower():
        return True
    if _PROJECT_LOWER not in hay:
        return False
    return any(m.lower() in hay for m in RELAY_MARKERS)


def _stop_rows(rows: list[dict[str, str]]) -> None:
    current = str(os.getpid())
    for row in rows:
        pid = row.get("ProcessId", "")
        if not pid or pid == current:
            continue
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def stop_all() -> None:
    rows = _proc_rows()
    _stop_rows([r for r in rows if _matches_service(r) or _matches_relay(r)])


def tunnel_running() -> bool:
    script = (
        "$m = '" + TUNNEL_MATCH.replace("'", "''") + "'; "
        "$p = Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like ('*' + $m + '*') }; "
        "if ($p) { exit 0 } else { exit 1 }"
    )
    return _ps(script).returncode == 0


# ── 设置读写 ─────────────────────────────────────────────────────────────────

def write_ai_settings(backend: str) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(BACKEND_SETTINGS[backend], ensure_ascii=False),
        encoding="utf-8",
    )


def current_backend() -> str:
    """读取 settings.json 判断当前 AI 后端。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if "codex" in data.get("backend", ""):
            return "gpt"
        return "claude"
    except Exception:
        return "claude"


# ── 服务启停 ─────────────────────────────────────────────────────────────────

def ensure_tunnel() -> None:
    if tunnel_running():
        return
    relay = PROJECT / "launchers" / "dist" / "relay.exe"
    if relay.exists():
        _run_hidden([str(relay)], cwd=relay.parent)


def _is_running(markers: tuple[str, ...]) -> bool:
    """检查是否有进程匹配给定的 marker（路径片段）。"""
    rows = _proc_rows()
    for row in rows:
        hay = (row.get("CommandLine", "") + "\n" + row.get("ExecutablePath", "")).lower()
        if _PROJECT_LOWER not in hay:
            continue
        if any(m.lower() in hay for m in markers):
            return True
    return False


def start_services() -> None:
    """启动 cmd_server、截图问答、relay。每个独立判断是否已在跑。"""
    cmd_py  = PROJECT / "scripts" / "cmd_server.py"
    cmd_exe = PROJECT / "launchers" / "dist" / "cmd_server.exe"
    qa_exe  = PROJECT / "launchers" / "dist" / "截图问答.exe"
    qa_py   = PROJECT / "launchers" / "截图问答.py"

    if not _is_running((r"\scripts\cmd_server.py", r"\launchers\cmd_server.py",
                        r"\launchers\dist\cmd_server.exe")):
        # cmd_server.exe 由 launchers/cmd_server.py 编译而来，存在与 scripts/cmd_server.py
        # 命名冲突的隐患，因此优先用 .py 直接运行。
        if cmd_py.exists():
            _run_hidden([PYTHON, str(cmd_py)], cwd=PROJECT)
        elif cmd_exe.exists():
            _run_hidden([str(cmd_exe)], cwd=cmd_exe.parent)

    if not _is_running((r"\launchers\截图问答.py", r"\launchers\dist\截图问答.exe")):
        if qa_exe.exists():
            _run_hidden([str(qa_exe), "--server"], cwd=qa_exe.parent)
        elif qa_py.exists():
            _run_hidden([PYTHON, "-u", str(qa_py), "--server"], cwd=PROJECT)

    ensure_tunnel()


def switch_backend(backend: str) -> None:
    """切换 AI 后端：写 settings.json，并在服务未启动时拉起。
    服务无需重启，因为 ai_client.ask() 每次调用都重新读取 settings.json。"""
    if backend not in BACKEND_SETTINGS:
        raise ValueError(f"unknown backend: {backend}")
    write_ai_settings(backend)
    start_services()


# ── 状态 ─────────────────────────────────────────────────────────────────────

def _port_owner(port: int) -> str:
    script = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        "if ($c) { "
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\"; "
        "'{0}|{1}|{2}' -f $c.OwningProcess,$p.ExecutablePath,$p.CommandLine "
        "}"
    )
    return _ps(script).stdout.strip()


def service_status() -> str:
    backend = current_backend()
    cmd     = _port_owner(CMD_SERVER_PORT) or "not listening"
    qa      = _port_owner(REMOTE_QA_PORT)  or "not listening"
    tunnel  = "running" if tunnel_running() else "stopped"
    return f"backend={backend}\ncmd_server={cmd}\nremote_qa={qa}\nrelay={tunnel}"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 后端切换 + 本地服务管理")
    sub = parser.add_subparsers(dest="command", required=True)

    switch = sub.add_parser("switch")
    switch.add_argument("backend", choices=sorted(BACKEND_SETTINGS))

    sub.add_parser("status")
    sub.add_parser("stop-all")
    sub.add_parser("start")

    args = parser.parse_args(argv)

    if args.command == "switch":
        switch_backend(args.backend)
        time.sleep(0.5)
        print(service_status())
    elif args.command == "status":
        print(service_status())
    elif args.command == "stop-all":
        stop_all()
        time.sleep(0.5)
        print(service_status())
    elif args.command == "start":
        start_services()
        time.sleep(0.5)
        print(service_status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
