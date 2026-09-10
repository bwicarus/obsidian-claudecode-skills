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

#: 启动方式（2026-09-09 用户：「应该把现在的启动方式作为一个可选项放在里面」）。
#:
#: keep-alive —— **原有行为**：打开语音功能就把意图置开，收敛循环随即按快捷键
#:   起一通，且此后语音一结束就再拉起来（那正是"保活"的意思）。
#: one-shot   —— 语音链照常装载，但**不**自动开：意图保持关，由 App 按钮或
#:   主动通知触发的一次性尝试来开。语音结束后不会自己重开。
#:
#: ⚠ 默认仍是 keep-alive：不因为多了个新选项就悄悄改掉别人已经习惯的行为。
#:
#: ⚠⚠ one-shot 能成立，靠的是 C# 那条**挂断**分支被 `intentChanged ||
#: initialReconcile` 门控着（DirectBridgeProtocol.ReconcileKeepActiveAsync）：
#: 意图稳定在 false 时，每 5 秒的收敛什么都不做，所以一次性按开的通话**不会**
#: 在几秒后被收敛掉。要是哪天把那条分支改成每 tick 都执行，one-shot 就会变成
#: "开三秒就被挂"，而这边一行代码都没动 —— 所以那条门是 one-shot 的前置条件，
#: 不是可有可无的优化。
START_MODE_KEEP_ALIVE = "keep-alive"
START_MODE_ONE_SHOT = "one-shot"
START_MODES = (START_MODE_KEEP_ALIVE, START_MODE_ONE_SHOT)
DEFAULT_START_MODE = START_MODE_KEEP_ALIVE


def normalize_start_mode(value: object) -> str:
    """认不出来的一律回默认 —— 封闭词汇表不做"就近取整"，但也不该让一个
    坏掉的偏好把语音整个卡死。"""
    return value if value in START_MODES else DEFAULT_START_MODE


def should_keep_alive(voice_enabled: bool, start_mode: object) -> bool:
    """打开语音功能时要不要顺手把意图置开。

    这就是用户看到的那句「怎么还是旧的快捷键启动方式」的出处：
    enable_readerpc_voice 里原本无条件 `set(..., voice_enabled)`。
    """
    return bool(voice_enabled) and (
        normalize_start_mode(start_mode) == START_MODE_KEEP_ALIVE)


#: F24 兜底开关（2026-09-10 用户：「把 f24 兜底作为一个可选开关」）。
#:
#: 推送送不出去时，桥要不要自己按一次快捷键把语音开起来。
#:   开（默认）= 现有行为：通道断着也能开语音，代价是"到底走了哪条路"要看账本
#:   关         = 只走通知通道；通道不通就如实报失败，不按任何键
#:
#: ⚠ 默认保持**开** —— 多一个开关不该悄悄改掉现在能用的行为。
#: ⚠ 跟"启用语音功能"是两件事：那个决定语音链装不装载，这个只决定
#:   推送失败之后要不要退而求其次。
FALLBACK_FILE = "voice-shortcut-fallback.json"
FALLBACK_CONTRACT = "reader-voice-shortcut-fallback/1"
DEFAULT_FALLBACK = True


def fallback_path(runtime: Path | None = None) -> Path:
    if runtime is not None:
        return runtime / FALLBACK_FILE
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    base = (Path(root) if root
            else Path.home() / "bw-computer-voice-bridge" / "runtime")
    return base / FALLBACK_FILE


def read_shortcut_fallback(runtime: Path | None = None) -> bool:
    """兜底开着吗。读不到一律回默认（开）—— 一个坏掉的偏好不该让语音开不了。"""
    try:
        value = json.loads(
            fallback_path(runtime).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_FALLBACK
    if (not isinstance(value, dict)
            or value.get("contract") != FALLBACK_CONTRACT
            or not isinstance(value.get("enabled"), bool)):
        return DEFAULT_FALLBACK
    return value["enabled"]


def write_shortcut_fallback(enabled: bool,
                            runtime: Path | None = None) -> Path:
    """原子写。跟保活意图同一套写法 —— 半个文件等于让读的人拿到默认值。"""
    path = fallback_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"contract": FALLBACK_CONTRACT,
                          "enabled": bool(enabled)}, ensure_ascii=False)
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
