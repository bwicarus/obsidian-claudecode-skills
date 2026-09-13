#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_restart.py — 重启 Codex Desktop 的标准做法（2026-09-13）。

装 Direct 桥会停掉 Codex 的 MCP 子进程，而 Codex 只在启动时拉 MCP，所以装完要重启它。
用户授权由 Claude 直接重启，但两条纪律：
  · **在通话/正在做事时不重启**（用户 2026-09-13：「暂时不要重启 codex，现在他正在做东西」）
    —— 麦克风台账说在通话就拒绝；还在跑的轮次看不到，只能靠 --force 由人来判断。
  · **起来不算完**：2026-09-13 15:52 实录，重启后 5 分半语音都进不去 —— Codex 冷着，推送
    连连超时。所以这里要等到通道建起来、且一条推送真的送达，才算重启完成。

    codex_restart.py [--force] [--no-warm] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PACKAGE_MATCH = "OpenAI.Codex"
GRACEFUL_SECONDS = 15
CHANNEL_WAIT_SECONDS = 150
WARM_TEXT = "这是状态更新，不是对话：Codex 刚被重启，无需回复。"


def _ps(script: str, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return (proc.stdout or "").strip()


def codex_processes() -> list[int]:
    out = _ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ChatGPT.exe' -and $_.ExecutablePath -match 'OpenAI\\.Codex' } | ForEach-Object { $_.ProcessId }")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def aumid() -> str | None:
    out = _ps("(Get-StartApps | Where-Object { $_.AppID -match 'OpenAI\\.Codex' } | Select-Object -First 1).AppID")
    return out or None


def voice_active() -> bool | None:
    try:
        import readerpc_services as services
        status = services.read_codex_voice_activity()
        return status.active if status.status == "available" else None
    except Exception:  # noqa: BLE001
        return None


def stop(pids: list[int]) -> str:
    if not pids:
        return "not-running"
    root = _ps("$ids = @(%s); Get-CimInstance Win32_Process | Where-Object { $ids -contains $_.ProcessId -and $ids -notcontains $_.ParentProcessId } | Select-Object -First 1 -ExpandProperty ProcessId" % ",".join(map(str, pids)))
    if root.isdigit():
        _ps("$p = Get-Process -Id %s -ErrorAction SilentlyContinue; if ($p) { $null = $p.CloseMainWindow() }" % root)
        deadline = time.time() + GRACEFUL_SECONDS
        while time.time() < deadline:
            if not codex_processes():
                return "graceful"
            time.sleep(0.5)
    _ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ChatGPT.exe' -and $_.ExecutablePath -match 'OpenAI\\.Codex' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    time.sleep(2)
    return "forced" if not codex_processes() else "still-running"


def launch(app_id: str) -> None:
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app_id])


def wait_channel(timeout: float) -> dict:
    """等 codex_channel.py --ensure 成功：枚举管道→tools/list 自证→选线程→登记。成功=Codex 真加载完。"""
    script = Path(os.environ.get("LOCALAPPDATA") or "") / "BWReader" / "codex_channel.py"
    if not script.is_file():
        return {"ok": False, "detail": "codex_channel.py 不在 %s" % script}
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        proc = subprocess.run([sys.executable, str(script), "--ensure"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        last = (proc.stdout or proc.stderr).strip()[-200:]
        if proc.returncode == 0:
            return {"ok": True, "detail": last, "waited": round(timeout - (deadline - time.time()), 1)}
        time.sleep(5)
    return {"ok": False, "detail": last}


def warm() -> dict:
    """一条推送真送达才算热了（它会在线程里显示为一条状态更新）。"""
    try:
        import codex_thread_notify as notify
        result = notify.send(WARM_TEXT, turn_timeout=90)
        return {"ok": bool(result.get("ok")), "threadId": result.get("threadId")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重启 Codex Desktop（拒绝在通话中重启；等它真正热起来）")
    parser.add_argument("--force", action="store_true", help="通话中也重启")
    parser.add_argument("--no-warm", action="store_true", help="起来就返回，不等通道与推送")
    args = parser.parse_args(argv)
    report: dict = {"contract": "codex-restart/1"}
    active = voice_active()
    report["voiceActiveBefore"] = active
    if active and not args.force:
        report.update({"ok": False, "detail": "Codex 正在通话（麦克风台账 active）；不重启。确认要重启用 --force"})
        print(json.dumps(report, ensure_ascii=False))
        return 2
    app_id = aumid()
    if not app_id:
        report.update({"ok": False, "detail": "找不到 Codex 的 AUMID（Get-StartApps）"})
        print(json.dumps(report, ensure_ascii=False))
        return 2
    report["stop"] = stop(codex_processes())
    launch(app_id)
    time.sleep(20)
    report["processes"] = len(codex_processes())
    if args.no_warm:
        report["ok"] = report["processes"] > 0
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    report["channel"] = wait_channel(CHANNEL_WAIT_SECONDS)
    report["warm"] = warm() if report["channel"]["ok"] else {"ok": False, "detail": "通道没建起来，不试推送"}
    report["ok"] = bool(report["warm"].get("ok"))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
