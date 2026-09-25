# -*- coding: utf-8 -*-
"""Mac 服务器的后台守护 —— 替代 Windows 上 ReaderPC 的「无界面那一半」。

2026-09-25 迁移到 Mac（用户拍板：服务器界面只保留网页；电脑语音不再需要）。
Windows 上 ReaderPC 是一个 tk 桌面程序，里面既有窗口 / 托盘，也有一批后台循环。
Mac 上把两件事拆开：

  · **各个服务进程**（网页后端、语音中继、远程浏览器、MCP、桥、CLI 语音核心）
    交给 launchd 常驻（KeepAlive，崩了自动拉起），不再由这里拉。
  · **这个守护进程**只做 ReaderPC 里剩下的、与平台无关的那几样：
      ① 网页界面 127.0.0.1:43132（同一份 readerpc_ui + readerpc_api，原样复用）；
      ② 设备间同步的账本应用（replication_apply.worker_loop，与 Windows 同一份代码）；
      ③ 展示板卡片渲染（看板存储变了才跑，渲染脚本同 Windows）；
      ④ 服务状态落盘（readerpc-server.status.json，网页的服务列表读它）。

没搬过来的：电脑语音（驱动 Codex 桌面版、虚拟声卡）与它的语音记录回写 ——
用户已确认不再使用；网页上对应的按钮会明确回答「Mac 上不适用」，不假装成功。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent / "windows" / "computer-voice-desktop"
sys.path.insert(0, str(DESKTOP))

import replication_apply  # noqa: E402
from readerpc_api import ReaderPCApi  # noqa: E402
from readerpc_services import ReaderPCPaths  # noqa: E402

VERSION = "mac-1"
LABEL_PREFIX = "space.bwicarus."
BRIDGE_ROOT = Path.home() / "BW" / "data" / "bridge"
BRIDGE_RUNTIME = BRIDGE_ROOT / "runtime"

# launchd 里的服务：名字 = plist 标签后缀，端口用来判断"真的在服务"。
SERVICES: list[tuple[str, str, int]] = [
    ("webapp", "网页后端(5000)", 5000),
    ("voice-rt", "实时语音中继(8767)", 8767),
    ("rbi", "远程浏览器(8769)", 8769),
    ("mcp", "MCP 门面(8766)", 8766),
    ("bridge", "阅读器桥(43128)", 43128),
    ("voice-core", "CLI 语音核心(43131)", 43131),
    ("anki", "Anki / AnkiConnect(8765)", 8765),
]

PATHS = ReaderPCPaths.discover()
LOG_FILE = PATHS.local_root / "readerpc-server.log"
_log_lock = threading.Lock()


def log(message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    print(line, flush=True)
    try:
        with _log_lock, LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def launchd_state(name: str) -> dict[str, Any]:
    """launchctl print 的 pid / 运行次数 / 上次退出码。服务没装载时返回空。"""
    target = f"gui/{os.getuid()}/{LABEL_PREFIX}{name}"
    try:
        out = subprocess.run(["launchctl", "print", target], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    state: dict[str, Any] = {"loaded": bool(out)}
    for raw in out.splitlines():
        line = raw.strip()
        for key, field in (("pid = ", "pid"), ("runs = ", "runs"),
                           ("last exit code = ", "lastExit")):
            if line.startswith(key):
                state[field] = line[len(key):].strip()
    return state


def kickstart(name: str) -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{LABEL_PREFIX}{name}"
    done = subprocess.run(["launchctl", "kickstart", "-k", target],
                          capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "launchctl 失败").strip()[:200]
    return True, "已让 launchd 重启"


# ---------- 状态落盘（网页的服务列表读它） ----------

def service_statuses() -> list[dict[str, Any]]:
    rows = []
    for name, label, port in SERVICES:
        state = launchd_state(name)
        reachable = port_open(port)
        error = None
        if not state.get("loaded"):
            error = "launchd 没有装载这个服务"
        elif not reachable:
            last = state.get("lastExit")
            error = "端口不通" + (f"（上次退出：{last}）" if last else "")
        runs = state.get("runs")
        rows.append({
            "name": name, "label": label, "port": port, "reachable": reachable,
            "owned": "pid" in state,
            "restarts": max(0, int(runs) - 1) if runs and runs.isdigit() else 0,
            "error": error, "halted": False,
        })
    return rows


def context_fresh() -> tuple[bool, str]:
    snapshot = BRIDGE_RUNTIME / "reader-context-snapshot.json"
    try:
        age = time.time() - snapshot.stat().st_mtime
    except OSError:
        return False, "还没有收到阅读上下文快照"
    return age < 300, f"快照 {int(age)} 秒前更新"


_labels: dict[str, Any] = {}


def refresh_status() -> None:
    rows = service_statuses()
    voice_up = port_open(43131)
    fresh, detail = context_fresh()
    _labels.update({
        "voice": "CLI 语音核心在线" if voice_up else "CLI 语音核心未运行",
        "voiceColor": "green" if voice_up else "grey",
        "voiceDetail": "Mac 服务器只用 CLI 语音；电脑语音已停用。",
        "context": "上下文新鲜" if fresh else "上下文陈旧", "contextFresh": fresh,
        "contextDetail": detail,
        "pc": "Mac 上暂未启用", "pcDetail": "PC 端 OCR 预处理还没有迁到 Mac。",
        "pcRunning": False,
    })
    payload = {
        "contract": "readerpc-server-status/1",
        "updatedAtEpochMs": int(time.time() * 1000),
        "services": rows,
        "host": "mac",
    }
    tmp = PATHS.status_file.with_name(PATHS.status_file.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATHS.status_file)


# ---------- 偏好与网页动作 ----------

def load_prefs() -> dict[str, Any]:
    try:
        prefs = json.loads(PATHS.preferences_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    prefs["voiceCoreManaged"] = True   # Mac 上语音核心由 launchd 常驻
    prefs["manageServerServices"] = True
    return prefs


def set_prefs(patch: dict[str, Any]) -> dict[str, Any]:
    """只持久化；Mac 上没有需要当场套用的处理器（服务都在 launchd 手里）。"""
    prefs = load_prefs()
    applied = []
    for key, value in (patch or {}).items():
        if key in ("voiceCoreManaged", "manageServerServices"):
            continue   # 固定开：由 launchd 管，不给关（关了只会显示假状态）
        prefs[key] = value
        applied.append(key)
    tmp = PATHS.preferences_file.with_name(PATHS.preferences_file.name + ".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PATHS.preferences_file)
    return {"ok": True, "applied": applied}


NOT_ON_MAC = {
    "toggle_pc": "PC 端 OCR 预处理还没有迁到 Mac。",
    "toggle_voice": "电脑语音已停用（Mac 服务器只用 CLI 语音）。",
    "open_legacy_window": "Mac 服务器没有桌面窗口，界面就是这个网页。",
    "open_legacy_settings": "Mac 服务器没有桌面窗口，界面就是这个网页。",
    "exit": "服务由 launchd 常驻；要停请在 Mac 上用 launchctl bootout。",
}


def run_action(name: str) -> dict[str, Any]:
    if name == "voice_core_start":
        ok, msg = kickstart("voice-core")
        return {"ok": ok, "msg": msg}
    if name.startswith("restart_service:"):
        target = name.split(":", 1)[1]
        if target not in {row[0] for row in SERVICES}:
            return {"ok": False, "msg": f"没有叫 {target} 的服务"}
        ok, msg = kickstart(target)
        return {"ok": ok, "msg": msg}
    if name in NOT_ON_MAC:
        return {"ok": False, "msg": NOT_ON_MAC[name]}
    return {"ok": False, "msg": f"未知动作 {name}"}


# ---------- 展示板卡片渲染 ----------

def board_render_loop(stop: threading.Event) -> None:
    store = PATHS.local_root / "reader-display-boards.json"
    script = PATHS.local_root / "board_card_render.py"
    seen = None
    while not stop.wait(20):
        try:
            stat = store.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        if signature == seen:
            continue
        seen = signature
        if not script.is_file():
            log("展示板卡片渲染：脚本缺失 " + str(script))
            continue
        try:
            done = subprocess.run([sys.executable, str(script)], capture_output=True,
                                  text=True, timeout=90)
            last = (done.stdout or "").strip().splitlines()[-1:] or [""]
            if done.returncode != 0:
                log(f"展示板卡片渲染失败（{done.returncode}）：{(done.stderr or last[0])[:200]}")
            elif '"rendered": 0' not in last[0]:
                log("展示板卡片渲染：" + last[0][:200])
        except subprocess.TimeoutExpired:
            log("展示板卡片渲染超时（>90s）")


# ---------- Anki 保活 ----------

ANKI_APP = "/Applications/Anki.app"


def anki_watch_loop(stop: threading.Event) -> None:
    """AnkiConnect 连不上、且 Anki 进程也不在 → 隐藏拉起（-g 不抢焦点，-j 隐藏窗口）。

    Anki 在跑但 8765 不通（比如正在同步、弹了对话框）时不动它：再开一次只会把窗口叫到前台。
    """
    while not stop.wait(60):
        if port_open(8765) or not Path(ANKI_APP).exists():
            continue
        running = subprocess.run(["pgrep", "-f", ANKI_APP + "/Contents/MacOS/"],
                                 capture_output=True).returncode == 0
        if running:
            continue
        subprocess.run(["/usr/bin/open", "-g", "-j", "-a", ANKI_APP], capture_output=True)
        log("Anki 不在运行，已在后台隐藏启动")


# ---------- 主循环 ----------

def main() -> int:
    PATHS.local_root.mkdir(parents=True, exist_ok=True)
    log(f"Mac 后台守护启动（{VERSION}），数据根 {PATHS.local_root}")
    stop = threading.Event()

    # ② 设备间同步：spool 在桥的 runtime，账本与数据副本在 BWReader（与 Windows 相同的分工）
    threading.Thread(target=replication_apply.worker_loop,
                     args=(PATHS.local_root, BRIDGE_RUNTIME / "replication-spool"),
                     name="replication-apply", daemon=True).start()
    # ③ 展示板卡片
    threading.Thread(target=board_render_loop, args=(stop,), name="board-cards",
                     daemon=True).start()

    # ⑤ Anki 保活
    threading.Thread(target=anki_watch_loop, args=(stop,), name="anki-watch",
                     daemon=True).start()

    # ① 网页界面
    refresh_status()
    api = ReaderPCApi(window=None, local_root=PATHS.local_root, bridge_runtime=BRIDGE_RUNTIME,
                      status_file=PATHS.status_file, preferences_file=PATHS.preferences_file,
                      log_file=LOG_FILE, version=VERSION, get_labels=lambda: dict(_labels),
                      get_prefs=load_prefs, set_prefs=set_prefs, run_action=run_action)
    url = api.start()
    log("网页界面：" + str(url))

    # ④ 状态每 5 秒刷新一次
    while True:
        try:
            refresh_status()
        except Exception as exc:   # noqa: BLE001 —— 刷新失败不能让守护退出
            log(f"状态刷新出错：{type(exc).__name__}: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
