"""voice_ladder — 语音入口的四级梯子（2026-09-09 用户拍板）。

用户原话：「电脑上没有开启服务器、开启服务器但是没有开启语音、语音已经开启
几种情况，但无论哪一种情况都应该逐步打开所有未打开环节并最终连接上语音」。

这里只做**观测与报告**：现在到了第几级、卡在哪一级、为什么。
每一级的执行器都已经在别处，各有各的守卫，不该在这里再造一份：

    1 服务器在跑    看门狗 / 开机自启（**够不到**，见下）
    2 语音链装载    ReaderPC 偏好 voiceEnabled → 服务意图 → 重启直连服务
    3 Codex 就绪    IDirectAppLauncher
    4 语音已连接    voice_start_step.py 写保活意图 → C# 收敛循环补齐并确认

⚠ **第 1 级够不到**（2026-09-09 用户拍板：如实告知，不做远程唤醒）。
桥是 ReaderPC 的子进程，而 tailscale serve 指向的 43128 只有桥在听 ——
ReaderPC 没跑时 App 的请求**没有接收方**。这不是"没实现"，是请求到不了。
所以这一级只报告，不假装在推进；由开机自启和看门狗兜底。

⚠ 每一级都可能是「不知道」。不知道 ≠ 没满足：前者该说出来等人看，
后者才该去推进。把两者混成一个布尔，卡住时就没人说得清卡在哪。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import voice_autoclose
import voice_keepalive

CONTRACT = "reader-voice-ladder/1"
STATUS_FILE_NAME = "voice-ladder-status.json"

#: 心跳超过这么久就当服务器没在跑。与自启脚本里的判据同一个数。
SERVER_HEARTBEAT_STALE_SECONDS = 180.0


def _rung(key: str, label: str, known: bool, satisfied: bool,
          why: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "known": known,
            "satisfied": bool(known and satisfied), "why": why}


def _server_rung(local_root: Path, now: float) -> dict[str, Any]:
    path = local_root / "readerpc-server.status.json"
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return _rung("server", "服务器在跑", True, False,
                     "没有心跳文件，服务器没在跑")
    if age > SERVER_HEARTBEAT_STALE_SECONDS:
        return _rung("server", "服务器在跑", True, False,
                     "心跳已停 %d 秒" % int(age))
    return _rung("server", "服务器在跑", True, True)


def _chain_rung(preferences: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preferences, dict) or "voiceEnabled" not in preferences:
        return _rung("chain", "语音链已装载", False, False,
                     "读不到偏好，不知道语音链装没装")
    enabled = preferences.get("voiceEnabled") is True
    return _rung("chain", "语音链已装载", True, enabled,
                 "" if enabled else "设置里「启用语音功能」是关的")


def _codex_rung(process_lister: Callable[[], list[str]] | None) -> dict[str, Any]:
    if process_lister is None:
        return _rung("codex", "Codex 就绪", False, False,
                     "没有进程视图，不知道 Codex 在不在")
    try:
        names = process_lister()
    except Exception as error:   # noqa: BLE001
        return _rung("codex", "Codex 就绪", False, False,
                     "看不了进程：%s" % str(error)[:80])
    running = any("chatgpt" in name.lower() for name in names)
    return _rung("codex", "Codex 就绪", True, running,
                 "" if running else "Codex 桌面端没在跑")


def _session_rung(ledger: dict[str, Any]) -> dict[str, Any]:
    if not ledger.get("known"):
        return _rung("session", "语音已连接", False, False,
                     ledger.get("why") or "读不到麦克风台账")
    active = bool(ledger.get("active"))
    return _rung("session", "语音已连接", True, active,
                 "" if active else "当前没有进行中的通话")


def ladder(
    *,
    local_root: Path,
    runtime: Path | None = None,
    preferences: dict[str, Any] | None = None,
    ledger_reader: Callable[[], dict[str, Any]] | None = None,
    process_lister: Callable[[], list[str]] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """算出现在到了第几级。

    返回 ``{"contract", "atUtcMs", "rungs": [...], "reached", "total",
    "blockedAt", "label", "reachable"}``。

    - ``reached``：从下往上连续满足了几级。
    - ``blockedAt``：第一个**没满足或不知道**的级；全通时是 None。
    - ``reachable``：这一级能不能由这条链自己推进。第 1 级永远 False。
    """
    now = (clock or time.time)()
    read = ledger_reader or voice_autoclose.read_ledger
    rungs = [
        _server_rung(local_root, now),
        _chain_rung(preferences),
        _codex_rung(process_lister),
        _session_rung(read()),
    ]
    reached = 0
    for item in rungs:
        if not item["satisfied"]:
            break
        reached += 1
    blocked = rungs[reached] if reached < len(rungs) else None
    return {
        "contract": CONTRACT,
        "atUtcMs": int(now * 1000),
        "rungs": rungs,
        "reached": reached,
        "total": len(rungs),
        "blockedAt": blocked["key"] if blocked else None,
        # 给界面直接显示的一句话。全通时说"已连接"，否则说卡在第几级、为什么。
        "label": (
            "语音已连接"
            if blocked is None
            else "正在打开语音（%d/%d）：%s%s" % (
                reached + 1, len(rungs), blocked["label"],
                "" if not blocked["why"] else " —— " + blocked["why"],
            )
        ),
        # ⚠ 第 1 级够不到：桥是 ReaderPC 的子进程，它不在就没有接收方。
        # 这里如实说 False，界面据此显示"电脑上的服务没在跑"而不是转圈。
        "reachable": blocked is None or blocked["key"] != "server",
        "keepActive": voice_keepalive.read_keep_active(runtime),
    }


def publish(status: dict[str, Any], runtime: Path | None = None) -> Path:
    """把梯子状态写给界面看。原子替换，别让读的人看到半个文件。"""
    base = runtime if runtime is not None else (
        Path(os.environ.get("BW_BRIDGE_RUNTIME")
             or Path.home() / "bw-computer-voice-bridge" / "runtime")
    )
    base.mkdir(parents=True, exist_ok=True)
    path = base / STATUS_FILE_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    return path
