"""voice_keepalive — 语音保活意图的读写（2026-09-09 从 readerpc_launcher 抽出）。

**为什么单独一个模块。**
这份意图现在有两个写入方：ReaderPC 的界面/策略环，以及 Codex 按状态回报去跑的
那个启动脚本。启动脚本是个很小的 CLI，不该为了写三个字段把整套 GUI 依赖
（tkinter / PIL / pystray）拖进来；而把文件格式在两处各写一遍，正是这个仓库
反复吃亏的形态。所以格式只在这里写一份。

**这份意图是什么。**
``{"contract": …, "enabled": bool}``。C# 侧**每 5 秒轮询**一次
（DirectBridgeProtocol 的 PeriodicTimer，全仓没有 FileSystemWatcher），
看到「意图与台账不一致」就去补齐：意图要开而台账没开 → 发快捷键并确认；
意图要关而台账开着 → 发快捷键关掉。

⚠ 所以它是**持续**语义，不是一次性命令。写 true 之后语音若结束，收敛循环还会
把它拉回来 —— 自动关闭因此必须先撤意图再挂断，否则两个循环互相打架。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONTRACT = "reader-codex-voice-keepalive/1"
FILE_NAME = "codex-voice-keepalive.json"

#: C# 侧的轮询周期。写完意图之后要等多久才可能看到动作，由它决定。
POLL_SECONDS = 5.0


def keepalive_path(runtime: Path | None = None) -> Path:
    """意图文件的位置。runtime = 桥的 runtime 目录。"""
    if runtime is not None:
        return runtime / FILE_NAME
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    base = (
        Path(root)
        if root
        else Path.home() / "bw-computer-voice-bridge" / "runtime"
    )
    return base / FILE_NAME


def read_keep_active(runtime: Path | None = None) -> bool | None:
    """读意图。文件缺失或格式不对一律返回 None（**不知道**，不是 False）。

    ⚠ 把"读不到"折成 False 会让调用方以为"意图是关着的"，于是省掉一次本该
    发生的写入。不知道就说不知道。
    """
    path = keepalive_path(runtime)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"contract", "enabled"}
        or value.get("contract") != CONTRACT
        or not isinstance(value.get("enabled"), bool)
    ):
        return None
    return value["enabled"]


def write_keep_active(enabled: bool, runtime: Path | None = None) -> Path:
    """原子写意图。写坏这个文件等于让收敛循环读到"不知道"，所以不就地改。"""
    path = keepalive_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"contract": CONTRACT, "enabled": bool(enabled)},
        ensure_ascii=False,
    )
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(payload)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
