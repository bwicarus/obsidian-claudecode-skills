from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageDraw
import pystray

from bridge_core import (
    BridgeError,
    BridgePaths,
    CONTEXT_DELIVERY_SNAPSHOT,
    ShortcutBrokerError,
    WindowsShortcutBroker,
    WindowsProcessRunner,
    clear_direct_service_record_if_pid,
    inspect_direct_shutdown_identity,
    load_direct_config,
    read_direct_shutdown_receipt_state,
    read_direct_status,
    set_direct_config_enabled,
    start_direct_service,
    stop_direct_service,
)
from control_plane import (
    ControlPaths,
    SubprocessExactCommandRunner,
    inspect_bootstrap_task,
    remove_bootstrap_task,
)
from readerpc_services import (
    CodexVoiceActivityStatus,
    PRODUCT_NAME,
    PcOcrServiceController,
    ReaderPCPaths,
    ReaderPCServiceError,
    format_pc_progress,
    read_codex_voice_activity,
    read_reader_context_status,
    write_disabled_reader_context_snapshot as write_offline_snapshot,
    write_recovering_reader_context_snapshot as write_recovering_snapshot,
    write_readerpc_status,
)
from voice_history_sidebar_sync import (
    CodexAppServerHistoryClient,
    CaptureBoundHistorySynchronizer,
    history_worker_lease,
    monitor_capture_history,
)


APP_VERSION = "0.1.57"
PREFERENCES_CONTRACT = "readerpc-server-config/1"
CODEX_VOICE_KEEPALIVE_CONTRACT = "reader-codex-voice-keepalive/1"
# 服务意图走独立文件(C# 启动时读取;keepalive/config/runtime-status
# 都是 exact 合同不能加键)。mode 只负责 full/bridge-only 音频路由;
# voiceEnabled 独立决定是否装载 keepalive/F24/语音控制链。非语音 Direct 底座不受它影响。
SERVICE_MODE_CONTRACT = "readerpc-service-mode/1"
SERVICE_MODE_FULL = "full"
SERVICE_MODE_BRIDGE_ONLY = "bridge-only"
SERVICE_MODES = frozenset((SERVICE_MODE_FULL, SERVICE_MODE_BRIDGE_ONLY))
POLL_INTERVAL_MS = 2_500
STATUS_PUBLISH_INTERVAL_SECONDS = 10.0
# 保活退避(2026-08-17 重启风暴修):基础 30s,连败指数升级封顶 15 分钟,成功清零。
# 固定 30s 的旧行为在持续性故障(依赖服务坏死等)下= 24 小时不停冷启动进程,
# 且 UI 只闪成功提示,用户无从知道在空转。
PC_RESTART_BACKOFF_SECONDS = 30.0
VOICE_RESTART_BACKOFF_SECONDS = 30.0
RESTART_BACKOFF_CAP_SECONDS = 900.0


def _escalated_backoff(base: float, streak: int) -> float:
    return min(RESTART_BACKOFF_CAP_SECONDS, base * (2 ** min(max(streak, 0), 5)))
VOICE_START_TIMEOUT_SECONDS = 8.0
VOICE_START_POLL_SECONDS = 0.1
SHORTCUT_BROKER_READY_SECONDS = 2.0
SHORTCUT_BROKER_READY_POLL_SECONDS = 0.1
@dataclass(frozen=True)
class ReaderPCHistoryStatus:
    """The independent service and Codex Voice gates for history sync."""

    service_online: bool
    capture_active: bool
    capture_generation: int | None = None


def write_disabled_reader_context_snapshot(
    bridge_paths: BridgePaths,
    **kwargs: Any,
) -> None:
    """Compatibility wrapper around the shared ReaderPC lifecycle writer."""

    write_offline_snapshot(bridge_paths.root, **kwargs)


def write_recovering_reader_context_snapshot(
    bridge_paths: BridgePaths,
    **kwargs: Any,
) -> None:
    """Publish a non-actionable snapshot while the enabled service recovers."""

    write_recovering_snapshot(bridge_paths.root, **kwargs)


def prepare_readerpc_shortcut_broker(
    *,
    voice_shortcut_enabled: bool = True,
) -> WindowsShortcutBroker | None:
    """Retire the old owner and optionally own F24 for this server lifetime."""

    bridge_paths = BridgePaths.discover()
    control_paths = ControlPaths.discover()
    runner = SubprocessExactCommandRunner()
    inspection = inspect_bootstrap_task(
        bridge_paths,
        control_paths,
        runner,
    )
    if inspection.exists:
        if not inspection.owned:
            raise BridgeError(
                "同名后台引导器不属于 Reader；拒绝删除或共用。"
            )
        remove_bootstrap_task(
            bridge_paths,
            control_paths,
            runner,
        )
    # A Direct generation left by the retired logon bootstrap cannot be
    # adopted: it has no ReaderPC owner PID. Replace it before this process
    # starts its own bounded generation.
    stop_readerpc_voice(
        bridge_paths,
        WindowsProcessRunner(),
        disable_configuration=False,
        force_on_cleanup_failure=False,
    )
    if not voice_shortcut_enabled:
        return None
    broker = WindowsShortcutBroker()
    try:
        broker.start()
    except ShortcutBrokerError:
        # /End is bounded but the retired process can still be unwinding. Wait
        # for its pipe to disappear, then make this ReaderPC process the owner.
        broker.close()
        deadline = time.monotonic() + SHORTCUT_BROKER_READY_SECONDS
        while time.monotonic() < deadline:
            if not WindowsShortcutBroker.probe_available():
                replacement = WindowsShortcutBroker()
                replacement.start()
                return replacement
            time.sleep(SHORTCUT_BROKER_READY_POLL_SECONDS)
        raise
    return broker


def readerpc_history_sync_enabled(paths: BridgePaths) -> bool:
    """Enable Reader chat return while this server owns snapshot mode."""

    config = load_direct_config(paths)
    return bool(
        config is not None
        and config.get("localOptIn") is True
        and config.get("contextDeliveryMode") == CONTEXT_DELIVERY_SNAPSHOT
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_preferences(path: Path) -> dict[str, object]:
    defaults: dict[str, object] = {
        "keepPcPreprocessingOnline": True,
        "serviceMode": SERVICE_MODE_FULL,
        "voiceEnabled": True,
        "snapshotViewerHidden": False,
        "hideVoiceOrb": False,
        "autoStartOnBoot": False,
    }
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return defaults
    if (
        not isinstance(value, dict)
        or value.get("contract") != PREFERENCES_CONTRACT
        or not isinstance(value.get("keepPcPreprocessingOnline"), bool)
    ):
        return defaults
    # serviceMode/snapshotViewerHidden 缺省容忍(旧偏好文件没有)→ 默认;非法值
    # 也回默认,不 bump contract:旧版启动器只读自己认识的键,天然兼容。
    mode = value.get("serviceMode")
    hidden = value.get("snapshotViewerHidden")
    return {
        "keepPcPreprocessingOnline": value["keepPcPreprocessingOnline"],
        "serviceMode": (
            mode
            if mode in SERVICE_MODES
            else SERVICE_MODE_FULL
        ),
        "voiceEnabled": value.get("voiceEnabled") is not False,
        "snapshotViewerHidden": hidden is True,
        "hideVoiceOrb": value.get("hideVoiceOrb") is True,
        "autoStartOnBoot": value.get("autoStartOnBoot") is True,
    }


def save_preferences(
    path: Path,
    *,
    keep_pc_online: bool,
    service_mode: str = SERVICE_MODE_FULL,
    voice_enabled: bool = True,
    snapshot_viewer_hidden: bool = False,
    hide_voice_orb: bool = False,
    auto_start_on_boot: bool = False,
) -> None:
    if service_mode not in SERVICE_MODES:
        raise ReaderPCServiceError(f"未知服务模式 {service_mode}")
    _atomic_json(
        path,
        {
            "contract": PREFERENCES_CONTRACT,
            "keepPcPreprocessingOnline": bool(keep_pc_online),
            "serviceMode": service_mode,
            "voiceEnabled": bool(voice_enabled),
            "snapshotViewerHidden": bool(snapshot_viewer_hidden),
            "hideVoiceOrb": bool(hide_voice_orb),
            "autoStartOnBoot": bool(auto_start_on_boot),
        },
    )


def set_readerpc_service_mode(
    bridge_paths: BridgePaths,
    mode: str,
    *,
    voice_enabled: bool = True,
    snapshot_viewer_hidden: bool = False,
) -> None:
    """写 C# 启动时读取的模式意图文件。改任一项必须随后重启直连服务才生效。
    snapshotViewer 键=静默快照(hidden 时 C# 不开快照查看器窗口,服务照跑)。"""

    if mode not in SERVICE_MODES:
        raise ReaderPCServiceError(f"未知服务模式 {mode}")
    _atomic_json(
        bridge_paths.runtime_status.parent / "readerpc-service-mode.json",
        {
            "contract": SERVICE_MODE_CONTRACT,
            "mode": mode,
            "voiceEnabled": bool(voice_enabled),
            "snapshotViewer": "hidden" if snapshot_viewer_hidden else "visible",
        },
    )


def merge_preferences_with_service_intent(
    preferences: dict[str, Any],
    bridge_paths: BridgePaths,
) -> dict[str, Any]:
    """Make an acknowledged App intent durable before optional helpers start."""

    merged = dict(preferences)
    try:
        value = json.loads(
            (bridge_paths.runtime_status.parent
             / "readerpc-service-mode.json").read_text("utf-8")
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return merged
    if (
        not isinstance(value, dict)
        or not {"contract", "mode"} <= set(value)
        or not set(value) <= {
            "contract",
            "mode",
            "voiceEnabled",
            "snapshotViewer",
        }
        or value.get("contract") != SERVICE_MODE_CONTRACT
        or value.get("mode") not in SERVICE_MODES
        or (
            "voiceEnabled" in value
            and not isinstance(value.get("voiceEnabled"), bool)
        )
        or (
            "snapshotViewer" in value
            and value.get("snapshotViewer") not in {"visible", "hidden"}
        )
    ):
        return merged
    merged["serviceMode"] = value["mode"]
    if "voiceEnabled" in value:
        merged["voiceEnabled"] = value["voiceEnabled"]
    if "snapshotViewer" in value:
        merged["snapshotViewerHidden"] = (
            value["snapshotViewer"] == "hidden"
        )
    return merged


def persist_preferences(
    path: Path,
    preferences: dict[str, Any],
) -> None:
    save_preferences(
        path,
        keep_pc_online=bool(preferences["keepPcPreprocessingOnline"]),
        service_mode=str(preferences["serviceMode"]),
        voice_enabled=bool(preferences["voiceEnabled"]),
        snapshot_viewer_hidden=bool(
            preferences["snapshotViewerHidden"]
        ),
        hide_voice_orb=bool(preferences["hideVoiceOrb"]),
        auto_start_on_boot=bool(preferences["autoStartOnBoot"]),
    )


def set_codex_voice_keep_active(
    bridge_paths: BridgePaths,
    enabled: bool,
) -> None:
    """Publish the ephemeral ReaderPC service intent consumed by Direct."""

    _atomic_json(
        bridge_paths.runtime_status.parent / "codex-voice-keepalive.json",
        {
            "contract": CODEX_VOICE_KEEPALIVE_CONTRACT,
            "enabled": bool(enabled),
        },
    )


# C# 侧是**纯轮询**读 keepalive 文件（DirectBridgeProtocol.cs:519 PeriodicTimer，
# 默认 5 秒；全仓没有任何 FileSystemWatcher）。这个数字决定了下面那个窗口要多宽。
_KEEPALIVE_POLL_SECONDS = 5.0


def rearm_codex_voice_keep_active(
    bridge_paths: BridgePaths,
    *,
    settle_seconds: float = _KEEPALIVE_POLL_SECONDS + 2.5,
    activity_reader=None,
) -> bool:
    """把 keepalive 真正**翻转**一次 false→true，用来解掉 C# 的自动恢复封锁。

    为什么需要翻转：C# 每个"意图代际"只有 2 次自动恢复预算，用尽后
    `_automaticRecoveryBlocked = 1` 永久放弃。解封只有一条路 ——
    `ApplyKeepActiveChange` 里那句 `if (previous == enabled) return false;`：
    只有真正的 false→true 跃迁才会重置失败计数并清掉封锁。ReaderPC 过去每次
    "恢复"都只是再写一遍 true，值没变等于没写。

    ⚠ 这个函数 2026-08-18 第一版是**有害**的，两处都错：
      · 窗口只留 0.35 秒，而对面是 5 秒轮询 —— 被看见的概率约 7%，
        三次预算打完总共约 20%。也就是说"自动解封"九成是空操作；
      · 更糟的是那 7% 里还藏着伤害：ReconcileKeepActiveAsync 读到 false 时，
        若麦克风台账显示语音**正开着**，它会 `SetActiveSerializedAsync(active:false)`
        —— 也就是发 F24 **把用户正在进行的通话关掉**。

    所以现在两条都改：
      1. **只在语音确实没起来时才翻**。伤害的触发条件正是"台账 active"，
         而我们要解决的场景恰好是"服务在线但语音起不来"，两者不重叠 ——
         把它写成前置条件，伤害分支就永远进不去。
      2. 窗口放宽到**超过一个轮询周期**，否则翻了也白翻。等待期间继续盯台账：
         一旦语音自己起来了，立刻把 true 写回去并放弃这次解封 —— 既不必再解，
         也把"C# 可能读到 false"的暴露时间压到最短。

    ⚠ 更根本的问题不在这里：拿一个进程外的文件跃迁去撬另一个进程的内部状态机，
      本身就是个隔着墙按开关的办法。真正的修法在 C# 侧（见语音链路重做）。
      这里只是把一个会伤人的临时手段改成不伤人的。

    返回是否真的做了翻转 —— 悄悄改状态而不留痕迹正是这一带最贵的毛病。
    """

    read_activity = activity_reader or read_codex_voice_activity

    def _voice_active() -> bool:
        try:
            return read_activity().active is True
        except Exception:
            # 读不到就当"可能开着"，宁可不解封也不要去关用户的通话。
            return True

    if _voice_active():
        _boot_log("Codex 语音台账显示正在通话，跳过解封（翻转会把它关掉）")
        return False

    current = read_codex_voice_keep_active(bridge_paths)
    if current is False:
        # 本来就是关的：没有封锁可解，直接置开即可，不必制造一次停机。
        set_codex_voice_keep_active(bridge_paths, True)
        return False

    set_codex_voice_keep_active(bridge_paths, False)
    deadline = time.monotonic() + max(1.0, float(settle_seconds))
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _voice_active():
            # 语音自己起来了 → 解封已无意义，且再停留在 false 就有被关掉的风险。
            set_codex_voice_keep_active(bridge_paths, True)
            _boot_log("解封等待期间语音已恢复，提前收窗")
            return True
    set_codex_voice_keep_active(bridge_paths, True)
    return True


# ⚠ 这张表**注定是不全的**：C# 侧现有 167 个 BW_COMPUTER_VOICE_DIRECT_* 失败码，
#   手工翻译一份必然滞后。真正该做的是把 C# 已经写好的那句人话带出来 ——
#   它在抛异常时就有（例如 VOICE_START_NOT_CONFIRMED 对应"音频链路建立失败；未能确认通话就绪"），
#   但 runtime-status 的 lastError 是 exact 合同 {failureId, code, stage, hresult, atUtc}，
#   **把 message 丢掉了**。补它要动 C# + 合同四处同步，留给语音链路重做那一轮。
#   在那之前：常见的几条给准确措辞，其余一律优雅降级（见 describe_voice_failure），
#   而不是让 167 个码里有 160 个显示成一串大写英文。
_VOICE_FAILURE_TEXT = {
    "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED": "音频链路建立失败；未能确认通话就绪",
    "BW_COMPUTER_VOICE_DIRECT_VOICE_STOP_NOT_CONFIRMED": "未能确认通话已结束",
    "BW_COMPUTER_VOICE_DIRECT_VOICE_CLOSED_LOCALLY": "通话在电脑这侧被结束",
    "BW_COMPUTER_VOICE_DIRECT_VOICE_BASELINE_MISSING": "读不到 Codex 语音状态基线",
    "BW_COMPUTER_VOICE_DIRECT_ACTIVITY_UNAVAILABLE": "读不到麦克风使用记录",
    "BW_COMPUTER_VOICE_DIRECT_ACTIVITY_READ_FAILED": "麦克风使用记录读取失败",
    "BW_COMPUTER_VOICE_DIRECT_APP_READY_TIMEOUT": "等 Codex 就绪超时",
    "BW_COMPUTER_VOICE_DIRECT_APP_START_FAILED": "Codex 启动失败",
    "BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS": "找到多个 Codex，无法确定目标",
    "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID": "电脑语音配置无效",
    "BW_COMPUTER_VOICE_DIRECT_AUTH_REQUIRED": "需要先完成配对",
    "BW_COMPUTER_VOICE_DIRECT_AUTH_TIMEOUT": "配对超时",
    "BW_COMPUTER_VOICE_DIRECT_BUSY": "上一次操作还没结束",
    "BW_COMPUTER_VOICE_DIRECT_BRIDGE_ONLY": "当前是仅桥接模式，语音不接到 App",
    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_BUSY": "音频线路被占用",
    "BW_COMPUTER_VOICE_DIRECT_AUDIO_SERVICE_NOT_READY": "Windows 音频服务未就绪",
    "BW_COMPUTER_VOICE_DIRECT_CAPTURE_ENDPOINT_INACTIVE": "录音设备未启用",
}

# 大类前缀 → 一句概括。表外的码靠它给出"至少说得出是哪一类"的说明。
_VOICE_FAILURE_FAMILY = (
    ("AUDIO_ROUTE", "音频线路"),
    ("AUDIO_SERVICE", "Windows 音频服务"),
    ("AUDIO", "音频"),
    ("CLASSIC_VOICE", "Codex 经典版语音按钮"),
    ("VOICE", "Codex 语音"),
    ("APP", "Codex 应用"),
    ("SHORTCUT", "语音快捷键"),
    ("ACTIVITY", "麦克风使用记录"),
    ("AUTH", "配对"),
    ("CONFIG", "配置"),
)


def describe_voice_failure(last_error: dict | None) -> str:
    """把 C# 写下的失败记录翻成一句人话。

    C# 一直老老实实把失败写进 runtime-status 的 lastError 和 failures.jsonl，
    bridge_core.read_direct_status 也**已经**把它解析进了 DirectStatus.last_error ——
    然后 ReaderPC 一次都没用过这个字段。于是界面上只有"无法确认 Codex 语音"，
    真正的原因就摆在文件里没人读。

    表外的码不显示成一串大写英文：剥掉 BW_COMPUTER_VOICE_DIRECT_ 前缀，按大类给一句
    概括，再把原码附在后面 —— 说不出准话也要说得出是哪一类，并且保留原文供排查。
    """

    if not isinstance(last_error, dict):
        return ""
    code = str(last_error.get("code") or "")
    stage = str(last_error.get("stage") or "")
    # 通配码带回来的**异常类型名**（2026-08-18 起）。不是 message —— message 里
    # 可能有设备/端点标识，桥那边刻意不外传（自测里那条异常就叫
    # secret-endpoint-id-must-never-be-serialized）。类型名是编译期常量，安全，
    # 而且对 INTERNAL_FAILURE 这种本来零信息的码来说是唯一的线索。
    exception_type = (last_error.get("exceptionType") or "").strip()
    # 保活链上的"按了但没确认"几乎只有一个成因，而且有明确的自救动作 —— 说出来。
    #
    # 2026-08-18 在本机用低层键盘钩子验过：注入的 F24 **确实进入了系统输入流**
    # （收到 keydown+keyup，flags 标着 injected），而 Codex 的 keybindings.json 里
    # realtimeVoice 也确实绑着 F24 —— 也就是键送到了、对方不响应。
    # 这正是用户说的"有时需要重启一下 codex 才行"：重启会让它重新注册全局热键。
    # 与其显示一个只有我们看得懂的码，不如直接告诉用户该做什么。
    if (
        code == "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED"
        and stage == "codex-voice-keepalive"
    ):
        return "Codex 没有响应语音快捷键（键已送达）；重启 Codex 通常可恢复"
    text = _VOICE_FAILURE_TEXT.get(code)
    if text and exception_type:
        text = f"{text} · {exception_type}"
    elif not text and exception_type:
        text = exception_type
    if not text:
        bare = code[len("BW_COMPUTER_VOICE_DIRECT_"):] if code.startswith(
            "BW_COMPUTER_VOICE_DIRECT_") else code
        family = next(
            (name for prefix, name in _VOICE_FAILURE_FAMILY if bare.startswith(prefix)),
            "",
        )
        if not bare:
            text = "未知失败"
        elif family:
            text = f"{family}异常 · {bare}"
        else:
            text = bare
    return f"{text}（{stage}）" if stage else text


def read_codex_voice_keep_active(
    bridge_paths: BridgePaths,
) -> bool | None:
    path = bridge_paths.runtime_status.parent / "codex-voice-keepalive.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"contract", "enabled"}
        or value.get("contract") != CODEX_VOICE_KEEPALIVE_CONTRACT
        or not isinstance(value.get("enabled"), bool)
    ):
        return None
    return value["enabled"]


def enable_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    bridge_only: bool = False,
    voice_enabled: bool = True,
    snapshot_viewer_hidden: bool = False,
) -> int:
    """Start the Direct generation owned by this ReaderPC process."""

    previous = load_direct_config(bridge_paths)
    if previous is None:
        message = (
            "现有电脑语音配置无效；拒绝静默覆盖。"
            if bridge_paths.direct_config.exists()
            else "尚未完成电脑语音配置，请先打开详细设置。"
        )
        try:
            write_recovering_reader_context_snapshot(bridge_paths)
        except Exception as exc:
            raise ReaderPCServiceError(
                f"{message} 同时无法撤销旧实时快照：{exc}"
            ) from exc
        raise ReaderPCServiceError(message)

    try:
        # 模式文件必须先于 start:C# 只在启动时读它。桥接模式语义(2026-08-17
        # 用户更正):语音**留在电脑**——keepalive 照常 True(自动拉 Codex+保持
        # 语音),只是 START(音频接到 App)被拒;"不接管"指不接走音频。
        service_mode = (
            SERVICE_MODE_BRIDGE_ONLY if bridge_only else SERVICE_MODE_FULL
        )
        set_readerpc_service_mode(
            bridge_paths,
            service_mode,
            voice_enabled=voice_enabled,
            snapshot_viewer_hidden=snapshot_viewer_hidden,
        )
        # 语音是 Direct 上的独立可选层。关闭时快照/MCP/卡片/视觉
        # 继续启动,但 C# 不装载保活与 F24 链。
        set_codex_voice_keep_active(bridge_paths, voice_enabled)
        set_direct_config_enabled(
            bridge_paths,
            True,
            context_delivery_mode=CONTEXT_DELIVERY_SNAPSHOT,
        )
        return start_readerpc_voice(
            bridge_paths,
            process_runner,
            owner_pid=os.getpid(),
        )
    except Exception as exc:
        try:
            set_codex_voice_keep_active(bridge_paths, False)
        except Exception:
            pass
        try:
            write_recovering_reader_context_snapshot(bridge_paths)
        except Exception as rollback_exc:
            raise ReaderPCServiceError(
                f"电脑语音启动失败：{exc}；同时无法撤销旧实时快照："
                f"{rollback_exc}"
            ) from exc
        raise


def stop_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    disable_configuration: bool = True,
    terminate_service: bool = True,
    force_on_cleanup_failure: bool = True,
) -> None:
    """Revoke ReaderPC intent and stop its exact Direct generation."""

    failures: list[str] = []
    lifecycle_writer = (
        write_disabled_reader_context_snapshot
        if disable_configuration
        else write_recovering_reader_context_snapshot
    )
    try:
        set_codex_voice_keep_active(bridge_paths, False)
    except Exception as exc:
        failures.append(f"撤销 ReaderPC 运行意图：{exc}")
    if disable_configuration:
        try:
            set_direct_config_enabled(bridge_paths, False)
        except Exception as exc:
            failures.append(f"关闭派生的电脑语音配置：{exc}")
    try:
        lifecycle_writer(bridge_paths)
    except Exception as exc:
        failures.append(f"撤销旧实时快照：{exc}")
    if terminate_service:
        try:
            status = read_direct_status(bridge_paths, process_runner)
            if status.service_online or bridge_paths.service_record.exists():
                stop_direct_service(
                    bridge_paths,
                    process_runner,
                    graceful=True,
                    force_on_cleanup_failure=force_on_cleanup_failure,
                )
        except Exception as exc:
            failures.append(f"停止电脑语音服务：{exc}")
    try:
        lifecycle_writer(bridge_paths)
    except Exception as exc:
        failures.append(f"确认实时快照已撤销：{exc}")
    if failures:
        raise ReaderPCServiceError("；".join(failures))


# ── Codex 语音球隐藏(2026-08-17 用户需求) ────────────────────────────────────
# 语音球是 Codex 的独立置顶 Electron 窗(class Chrome_WidgetWin_1,标题 "Codex",
# TOPMOST,属主 ChatGPT (Beta).exe),每次语音会话重建 → 周期按签名扫描隐藏。
# 只动显示层:麦克风/播报/F24 保活全不受影响。主应用窗不置顶,签名天然排除。
def find_voice_orb_windows() -> list[int]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x0000_0008
    result: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def on_window(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            title = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, title, 64)
            if title.value != "Codex":
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            if not handle:
                return True
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    return True
                exe = buf.value.rsplit("\\", 1)[-1].lower()
            finally:
                kernel32.CloseHandle(handle)
            if exe not in ("chatgpt (beta).exe", "codex.exe"):
                return True
            result.append(int(hwnd))
        except Exception:
            pass
        return True

    user32.EnumWindows(on_window, 0)
    return result


def set_window_visible(hwnd: int, visible: bool) -> None:
    if os.name != "nt":
        return
    import ctypes

    # SW_HIDE=0;SW_SHOWNA=8(显示但不抢焦点)
    ctypes.WinDLL("user32").ShowWindow(hwnd, 8 if visible else 0)


# ── 开机自启 + 崩溃看门狗(2026-08-17 做成可选项) ────────────────────────────
# 外部三件套由本进程按偏好收敛:start-readerpc.ps1(读偏好+退出标记后决定)、
# start-readerpc.vbs(wscript 包装,无控制台闪烁)、HKCU Run + 5 分钟计划任务。
# 用户主动退出会写退出标记,看门狗见标记不复活;登录自启会清标记。
AUTOSTART_RUN_VALUE = "BWReaderPCServer"
WATCHDOG_TASK_NAME = "BW ReaderPC Watchdog"
USER_EXIT_MARKER_NAME = "readerpc-user-exit.json"
USER_EXIT_MARKER_CONTRACT = "readerpc-user-exit/1"

_AUTOSTART_PS1 = 'param([string]$Reason = "watchdog")\n$root = Join-Path $env:LOCALAPPDATA "BWReader"\n$cfg = Join-Path $root "readerpc-server.config.json"\n$marker = Join-Path $root "readerpc-user-exit.json"\n$log = Join-Path $root "ReaderPC-Server" | Join-Path -ChildPath "autostart.log"\nfunction Log($m) {\n  # 写日志失败绝不能影响主流程:曾因日志路径损坏导致整个自启脚本退出码 1,\n  # ReaderPC 连着几次重启都起不来,而且因为日志本身坏了所以毫无线索。\n  try {\n    if ((Test-Path $log) -and (Get-Item $log).Length -gt 524288) { Clear-Content $log }\n    Add-Content -Path $log -Value "$(Get-Date -Format o) [$Reason] $m" -Encoding UTF8\n  } catch { }\n}\ntry { $prefs = Get-Content $cfg -Raw | ConvertFrom-Json } catch { Log "无法读偏好: $_"; exit 0 }\nif ($prefs.autoStartOnBoot -ne $true) { Log "自启选项未开,不动"; exit 0 }\n$hb = Join-Path $root "readerpc-server.status.json"\nif (Get-Process -Name "ReaderPC-Server" -ErrorAction SilentlyContinue) {\n  # 进程还在 != 服务还活着。ReaderPC 每 10 秒刷一次状态文件;心跳停了 3 分钟\n  # 说明界面循环已经卡死或退化 —— 而这恰恰是最坏的情形:看门狗只看进程存在,\n  # 于是它什么都不做,而程序其实早就不工作了(用户: 明明开着却说通道断开)。\n  # 重新拉起就行:新实例启动时会先清掉同角色的旧进程再接管。\n  $stale = $false\n  try { $stale = (-not (Test-Path $hb)) -or (((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds -gt 180) } catch { $stale = $false }\n  if (-not $stale) { exit 0 }\n  Log "进程在但心跳已停超过 180 秒,按无响应处理并重新拉起"\n}\nif ($Reason -eq "logon") {\n  Remove-Item $marker -ErrorAction SilentlyContinue\n} elseif (Test-Path $marker) {\n  # 标记只在本次开机内有效:关机时系统关闭应用也会写标记,若登录自启项没跑\n  # (Win11 的 Run 键并不可靠),过期标记会永久卡死看门狗 -- 按开机时间判废。\n  $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime\n  if ((Get-Item $marker).LastWriteTime -lt $boot) {\n    Log "退出标记早于本次开机,视为过期并清除"\n    Remove-Item $marker -ErrorAction SilentlyContinue\n  } else {\n    Log "用户本次开机内主动退出过,看门狗不复活"\n    exit 0\n  }\n}\ntry { $cur = Get-Content (Join-Path $root "ReaderPC-Server" | Join-Path -ChildPath "current.json") -Raw | ConvertFrom-Json } catch { Log "无法读 current.json: $_"; exit 0 }\n$exe = Join-Path $cur.release "ReaderPC-Server.exe"\nif (-not (Test-Path $exe)) { Log "找不到 $exe"; exit 0 }\nLog "启动 $exe"\nStart-Process $exe\n'

_AUTOSTART_VBS = 'Dim reason\nIf WScript.Arguments.Count > 0 Then\n    reason = WScript.Arguments(0)\nElse\n    reason = "watchdog"\nEnd If\nCreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""__PS1__"" -Reason " & reason, 0, False\n'


def write_autostart_scripts(local_root: Path) -> Path:
    install_root = local_root / "ReaderPC-Server"
    install_root.mkdir(parents=True, exist_ok=True)
    ps1 = install_root / "start-readerpc.ps1"
    vbs = install_root / "start-readerpc.vbs"
    ps1.write_text(_AUTOSTART_PS1, encoding="utf-8-sig")
    # ⚠ ps1 要 BOM(PowerShell 5.1 靠它认中文),vbs 绝不能有:VBScript 会把
    # BOM 当第一个字符,报 "(1, 1) 无效字符" 直接不执行。而 wscript //B 是静默的,
    # 于是计划任务只留下一个 Last Result: 1 —— 开机自启整条链就这么哑掉了。
    vbs.write_text(
        _AUTOSTART_VBS.replace("__PS1__", str(ps1)), encoding="utf-8"
    )
    return vbs


def set_autostart_enabled(local_root: Path, enabled: bool) -> None:
    """收敛开机自启外部状态。开=写脚本+Run 键+看门狗任务;关=拆 Run 键+删任务。"""
    if os.name != "nt":
        return
    import winreg

    run_key = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
    no_window = 0x08000000
    if enabled:
        vbs = write_autostart_scripts(local_root)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key,
                AUTOSTART_RUN_VALUE,
                0,
                winreg.REG_SZ,
                f'wscript.exe //B "{vbs}" logon',
            )
        proc = subprocess.run(
            [
                "schtasks", "/Create", "/F",
                "/TN", WATCHDOG_TASK_NAME,
                "/SC", "MINUTE", "/MO", "5",
                "/TR", f'wscript.exe //B "{vbs}" watchdog',
            ],
            capture_output=True,
            text=True,
            creationflags=no_window,
        )
        if proc.returncode != 0:
            raise ReaderPCServiceError(
                f"看门狗计划任务创建失败: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    else:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, AUTOSTART_RUN_VALUE)
        except FileNotFoundError:
            pass
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", WATCHDOG_TASK_NAME],
            capture_output=True,
            text=True,
            creationflags=no_window,
        )


def write_user_exit_marker(local_root: Path) -> None:
    _atomic_json(
        local_root / USER_EXIT_MARKER_NAME,
        {"contract": USER_EXIT_MARKER_CONTRACT, "exitedAt": time.time()},
    )


def clear_user_exit_marker(local_root: Path) -> None:
    try:
        (local_root / USER_EXIT_MARKER_NAME).unlink(missing_ok=True)
    except OSError as exc:
        _boot_log(f"[warn] 清除退出标记失败: {exc}")


def _tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=14, fill="#0A84FF")
    draw.rectangle((17, 16, 25, 48), fill="white")
    draw.rectangle((29, 16, 37, 48), fill="white")
    draw.rectangle((41, 16, 49, 48), fill="white")
    draw.ellipse((19, 20, 23, 24), fill="#0A84FF")
    draw.ellipse((31, 32, 35, 36), fill="#0A84FF")
    draw.ellipse((43, 40, 47, 44), fill="#0A84FF")
    return image


def stop_readerpc_services(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    pc_ocr: PcOcrServiceController,
) -> None:
    """退出即全停(2026-08-17 用户定案):桥接/语音/keepalive 链一并终止。

    旧行为 terminate_service=False 留着 C# 直连服务,keepalive 链因此在用户
    退出后还会把 Codex 拉起来。现在退出后不留任何后台;Codex 会话里的
    reader_snapshot MCP 是 Codex 自己拉的独立进程,不受影响。
    """

    failures: list[str] = []
    try:
        pc_ocr.stop()
    except Exception as exc:
        failures.append(f"PC 预处理：{exc}")

    try:
        stop_readerpc_voice(
            bridge_paths,
            process_runner,
            disable_configuration=True,
            terminate_service=True,
            force_on_cleanup_failure=True,
        )
    except Exception as exc:
        failures.append(f"电脑语音与上下文直连：{exc}")

    if failures:
        raise ReaderPCServiceError("；".join(failures))


def start_readerpc_voice(
    bridge_paths: BridgePaths,
    process_runner: WindowsProcessRunner,
    *,
    owner_pid: int | None = None,
    timeout_seconds: float = VOICE_START_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Start the direct service and require a matching fresh runtime heartbeat."""

    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ReaderPCServiceError("电脑语音启动确认时限无效。")
    current = read_direct_status(bridge_paths, process_runner)
    if not current.service_online and bridge_paths.service_record.exists():
        # A faulted/stale owned generation cannot be healed by starting a
        # second listener.  Stop only the PID authenticated by the strict
        # service record, then start one replacement generation.
        stop_direct_service(bridge_paths, process_runner)
    pid = start_direct_service(
        bridge_paths,
        process_runner,
        readerpc_owner_pid=owner_pid,
    )
    deadline = clock() + timeout_seconds
    last_reason = "runtime-status-offline-or-stale"
    while True:
        status = read_direct_status(bridge_paths, process_runner)
        last_reason = status.reason or last_reason
        if status.service_online and status.pid == pid:
            return pid
        if process_runner.executable_for_pid(pid) is None:
            stop_direct_service(bridge_paths, process_runner)
            raise ReaderPCServiceError(
                "电脑语音进程启动后立即退出"
                f"（{last_reason}）。电脑语音组件与当前配置可能不兼容。"
            )
        if clock() >= deadline:
            stop_direct_service(bridge_paths, process_runner)
            raise ReaderPCServiceError(
                "电脑语音进程已启动，但未在限定时间内写出有效状态"
                f"（{last_reason}）。"
            )
        sleeper(VOICE_START_POLL_SECONDS)


class ReaderPCWindow:
    # 类级默认:保活退避计数在部分构造(测试/早期启动)下也存在
    pc_fail_streak = 0
    voice_fail_streak = 0

    def _auto_start_enabled(self) -> bool:
        var = getattr(self, "auto_start", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _converge_autostart(self) -> None:
        try:
            set_autostart_enabled(self.readerpc_paths.local_root, True)
        except Exception as exc:
            _boot_log(f"[warn] 自启收敛失败: {exc}")

    def on_auto_start_changed(self) -> None:
        self._save_current_preferences()
        enabled = self._auto_start_enabled()

        def worker() -> None:
            try:
                set_autostart_enabled(self.readerpc_paths.local_root, enabled)
            except Exception as exc:
                self.events.put(("task-error", ("开机自启设置", exc)))

        threading.Thread(
            target=worker, name="readerpc-autostart", daemon=True
        ).start()

    def _hide_orb_enabled(self) -> bool:
        var = getattr(self, "hide_voice_orb", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _voice_orb_tick(self) -> None:
        """开着就按签名隐藏语音球(每次语音会话重建,须周期扫);关掉恢复已藏的。"""
        hidden = getattr(self, "_orb_hidden_hwnds", None)
        if hidden is None:
            return
        try:
            if self._hide_orb_enabled():
                for hwnd in find_voice_orb_windows():
                    set_window_visible(hwnd, False)
                    hidden.add(hwnd)
            elif hidden:
                for hwnd in list(hidden):
                    set_window_visible(hwnd, True)
                hidden.clear()
        except Exception:
            pass

    def on_hide_orb_changed(self) -> None:
        self._save_current_preferences()
        self._voice_orb_tick()

    def _snapshot_hidden_enabled(self) -> bool:
        var = getattr(self, "snapshot_hidden", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _current_intent_kwargs(self) -> dict:
        """模式开关共用的启动意图(enable_readerpc_voice 的 kwargs)。"""
        return {
            "bridge_only": self._bridge_only_enabled(),
            "voice_enabled": self._voice_enabled(),
            "snapshot_viewer_hidden": self._snapshot_hidden_enabled(),
        }

    def _current_service_mode(self) -> str:
        if self._bridge_only_enabled():
            return SERVICE_MODE_BRIDGE_ONLY
        return SERVICE_MODE_FULL

    def _save_current_preferences(self) -> None:
        save_preferences(
            self.readerpc_paths.preferences_file,
            keep_pc_online=bool(self.keep_pc_online.get()),
            service_mode=self._current_service_mode(),
            voice_enabled=self._voice_enabled(),
            snapshot_viewer_hidden=self._snapshot_hidden_enabled(),
            hide_voice_orb=self._hide_orb_enabled(),
            auto_start_on_boot=self._auto_start_enabled(),
        )

    def _restart_voice_with_intent(self, busy: str, done: str) -> None:
        """模式类开关共用:停旧代际 → 按当前意图重启(C# 只在启动时读意图文件)。"""
        if self.busy or self.closing:
            return
        intent = self._current_intent_kwargs()
        previous_applied = {
            "service_mode": getattr(
                self,
                "_applied_service_mode",
                self._current_service_mode(),
            ),
            "voice_enabled": getattr(
                self,
                "_applied_voice_enabled",
                self._voice_enabled(),
            ),
            "snapshot_hidden": getattr(
                self,
                "_applied_snapshot_hidden",
                self._snapshot_hidden_enabled(),
            ),
        }

        def switch() -> int:
            try:
                stop_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    disable_configuration=False,
                    force_on_cleanup_failure=False,
                )
                # A normally absent old process already returns from the stop
                # function.  Receipt/identity/cleanup failures must propagate:
                # closing F24 or starting another generation would otherwise
                # turn an unconfirmed teardown into a false applied state.
                self._converge_shortcut_broker(
                    voice_shortcut_enabled=intent["voice_enabled"]
                )
                self.voice_start_in_progress = True
                try:
                    pid = enable_readerpc_voice(
                        self.bridge_paths,
                        self.process_runner,
                        **intent,
                    )
                    self.voice_snapshot_offline_marked = False
                finally:
                    self.voice_start_in_progress = False
            except Exception:
                self.events.put(("intent-rollback", previous_applied))
                raise
            self._applied_service_mode = (
                SERVICE_MODE_BRIDGE_ONLY
                if intent["bridge_only"]
                else SERVICE_MODE_FULL
            )
            self._applied_voice_enabled = intent["voice_enabled"]
            self._applied_snapshot_hidden = intent[
                "snapshot_viewer_hidden"
            ]
            self._converge_history_monitor(intent["voice_enabled"])
            return pid

        self.last_voice_start_attempt = time.monotonic()
        self._run_task(busy, switch, done)

    def __init__(
        self,
        root: tk.Tk,
        *,
        bridge_paths: BridgePaths | None = None,
        process_runner: WindowsProcessRunner | None = None,
        pc_ocr: PcOcrServiceController | None = None,
        readerpc_paths: ReaderPCPaths | None = None,
        shortcut_broker: WindowsShortcutBroker | None = None,
    ) -> None:
        self.root = root
        self.bridge_paths = bridge_paths or BridgePaths.discover()
        self.process_runner = process_runner or WindowsProcessRunner()
        self.pc_ocr = pc_ocr or PcOcrServiceController()
        self.readerpc_paths = readerpc_paths or ReaderPCPaths.discover()
        self._shortcut_broker = shortcut_broker
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.closed = False
        self.closing = False
        self.service_lock = threading.Lock()
        self.last_pc_start_attempt = 0.0
        self.last_voice_start_attempt = 0.0
        self.pc_fail_streak = 0
        self.voice_fail_streak = 0
        self.voice_recovery_in_progress = False
        self.voice_start_in_progress = False
        self.voice_stop_in_progress = False
        self.voice_snapshot_offline_marked = False
        self.last_status_publish = 0.0
        self.history_stop_event = threading.Event()
        self.history_synchronizer = CaptureBoundHistorySynchronizer(
            root=self.bridge_paths.root,
            structured_history_client=CodexAppServerHistoryClient(),
        )
        self.history_thread: threading.Thread | None = None
        preferences = load_preferences(self.readerpc_paths.preferences_file)
        self.keep_pc_online = tk.BooleanVar(
            value=preferences["keepPcPreprocessingOnline"]
        )
        self.bridge_only = tk.BooleanVar(
            value=preferences["serviceMode"] == SERVICE_MODE_BRIDGE_ONLY
        )
        self.voice_enabled = tk.BooleanVar(
            value=bool(preferences["voiceEnabled"])
        )
        self.snapshot_hidden = tk.BooleanVar(
            value=bool(preferences["snapshotViewerHidden"])
        )
        self.hide_voice_orb = tk.BooleanVar(
            value=bool(preferences["hideVoiceOrb"])
        )
        self.auto_start = tk.BooleanVar(
            value=bool(preferences["autoStartOnBoot"])
        )
        self._applied_service_mode = str(preferences["serviceMode"])
        self._applied_voice_enabled = bool(preferences["voiceEnabled"])
        self._applied_snapshot_hidden = bool(
            preferences["snapshotViewerHidden"]
        )
        self._orb_hidden_hwnds: set[int] = set()
        # 手动启动 = 用户要它跑:清退出标记,看门狗恢复看护;偏好开着就把
        # 外部自启三件套收敛到最新(脚本内容随版本升级)。
        clear_user_exit_marker(self.readerpc_paths.local_root)
        if bool(preferences["autoStartOnBoot"]):
            threading.Thread(
                target=self._converge_autostart,
                name="readerpc-autostart-converge",
                daemon=True,
            ).start()

        root.title(PRODUCT_NAME)
        # 高度要装下 3 个服务行 + 6 行选项 + 页脚。
        root.geometry("620x650")
        root.minsize(560, 590)
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        root.bind("<Unmap>", self._on_unmap, add="+")

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("ReaderPC.Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("ReaderPC.Heading.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=PRODUCT_NAME, style="ReaderPC.Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "统一显示电脑语音、Reader 上下文与 PC 预处理。"
                "最小化后驻留托盘；关闭时停止全部相关服务。"
            ),
            foreground="#596579",
            wraplength=570,
        ).pack(anchor="w", pady=(4, 14))

        self.voice_status, self.voice_detail, self.voice_button = self._service_row(
            outer,
            "电脑语音",
            "随 ReaderPC 服务器启动、保活并在服务器关闭时停止",
            None,
        )
        self.context_status, self.context_detail, _ = self._service_row(
            outer,
            "Reader 上下文",
            "网页与 App 的实时快照通道",
            None,
        )
        self.pc_status, self.pc_detail, self.pc_button = self._service_row(
            outer,
            "PC 预处理",
            "文字、分词与公式 · quality-first-v5",
            self.toggle_pc,
        )

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(8, 2))
        ttk.Checkbutton(
            options,
            text="ReaderPC 运行期间保持 PC 预处理在线",
            variable=self.keep_pc_online,
            command=self.on_keep_pc_changed,
        ).pack(side="left")
        ttk.Button(
            options,
            text="打开音频与连接配置",
            command=self.open_legacy_voice_settings,
        ).pack(side="right")
        mode_row = ttk.Frame(outer)
        mode_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            mode_row,
            text="仅桥接模式：语音留在电脑（用电脑音频设备），通话不接到 App",
            variable=self.bridge_only,
            command=self.on_bridge_only_changed,
        ).pack(side="left")
        voice_mode_row = ttk.Frame(outer)
        voice_mode_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            voice_mode_row,
            text="启用语音功能（自动拉起 Codex、F24 保活与音频桥接）",
            variable=self.voice_enabled,
            command=self.on_voice_enabled_changed,
        ).pack(side="left")
        orb_row = ttk.Frame(outer)
        orb_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            orb_row,
            text="隐藏 Codex 语音球（只藏显示层，语音功能不受影响）",
            variable=self.hide_voice_orb,
            command=self.on_hide_orb_changed,
        ).pack(side="left")
        viewer_row = ttk.Frame(outer)
        viewer_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            viewer_row,
            text="静默快照：后台运行快照服务，不显示查看器窗口",
            variable=self.snapshot_hidden,
            command=self.on_snapshot_hidden_changed,
        ).pack(side="left")
        boot_row = ttk.Frame(outer)
        boot_row.pack(fill="x", pady=(2, 2))
        ttk.Checkbutton(
            boot_row,
            text="开机后自动启动（含 5 分钟崩溃自愈看门狗；主动退出不会被复活）",
            variable=self.auto_start,
            command=self.on_auto_start_changed,
        ).pack(side="left")

        ttk.Separator(outer).pack(fill="x", pady=(12, 10))
        self.footer = ttk.Label(
            outer,
            text="正在读取本机状态…",
            foreground="#596579",
            wraplength=570,
        )
        self.footer.pack(anchor="w")

        self.tray = pystray.Icon(
            "ReaderPCServer",
            _tray_image(),
            PRODUCT_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("显示主窗口", self._tray_show, default=True),
                pystray.MenuItem("启动 PC 预处理", self._tray_start_pc),
                pystray.MenuItem("停止 PC 预处理", self._tray_stop_pc),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出并停止全部服务", self._tray_exit),
            ),
        )
        threading.Thread(target=self._run_tray, name="readerpc-tray", daemon=True).start()
        self._converge_history_monitor(self._voice_enabled())
        root.after(100, self._drain_events)
        root.after(250, self.refresh)
        root.after(600, self._ensure_pc_online)
        root.after(800, self._ensure_voice_online)

    def _service_row(
        self,
        parent: ttk.Frame,
        title: str,
        subtitle: str,
        command: Callable[[], None] | None,
    ) -> tuple[ttk.Label, ttk.Label, ttk.Button | None]:
        frame = ttk.LabelFrame(parent, padding=(12, 10))
        frame.pack(fill="x", pady=(0, 9))
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=title, style="ReaderPC.Heading.TLabel").pack(side="left")
        status = ttk.Label(top, text="正在读取", foreground="#596579")
        status.pack(side="left", padx=(12, 0))
        button = None
        if command is not None:
            button = ttk.Button(top, text="…", command=command, width=10)
            button.pack(side="right")
        detail = ttk.Label(frame, text=subtitle, foreground="#6b7280", wraplength=520)
        detail.pack(anchor="w", pady=(5, 0))
        return status, detail, button

    def _run_tray(self) -> None:
        try:
            self.tray.run()
        except Exception as exc:
            self.events.put(("tray-error", str(exc)))

    def _run_history_sync(self) -> None:
        """Keep capture-bound voice turns beside ReaderPC's owned service."""

        with history_worker_lease(self.bridge_paths.root) as owned:
            if not owned:
                return
            monitor_capture_history(
                stop_event=self.history_stop_event,
                status_provider=self._history_status,
                enabled_provider=lambda: readerpc_history_sync_enabled(
                    self.bridge_paths
                ),
                synchronizer=self.history_synchronizer,
            )

    def _converge_history_monitor(self, voice_enabled: bool) -> None:
        """The voice ledger/history watcher belongs to the optional layer."""

        current = getattr(self, "history_thread", None)
        if not voice_enabled:
            self.history_stop_event.set()
            if current is not None and current.is_alive():
                current.join(timeout=3)
            if current is None or not current.is_alive():
                self.history_thread = None
            return
        if current is not None and current.is_alive():
            return
        self.history_stop_event = threading.Event()
        self.history_thread = threading.Thread(
            target=self._run_history_sync,
            name="readerpc-voice-history",
            daemon=True,
        )
        self.history_thread.start()

    def _tray_show(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.show_window)

    def _tray_start_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(True))

    def _tray_stop_pc(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self._set_pc_running(False))

    def _tray_exit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.request_exit)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_window(self) -> None:
        self.root.withdraw()

    def _on_unmap(self, event=None) -> None:
        if self.closed or self.closing:
            return
        if event is not None and getattr(event, "widget", self.root) is not self.root:
            return
        self.root.after(0, self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        if self.closed or self.closing:
            return
        try:
            if self.root.state() == "iconic":
                self.hide_window()
        except tk.TclError:
            pass

    def request_exit(self) -> None:
        if self.closed or self.closing:
            return
        self.closing = True
        self.busy = True
        self.show_window()
        self._set_buttons_enabled(False)
        self.footer.configure(
            text="正在停止全部服务（桥接、电脑语音、PC 预处理）…",
            foreground="#596579",
        )

        def worker() -> None:
            try:
                # ⚠ 不依赖 self.readerpc_paths：退出可能发生在 __init__ 走完之前
                #   （被别的实例清理、启动早期异常、用户秒关窗），那时这个属性还不存在，
                #   于是"写退出标记"整个失败 —— 而标记没写成意味着**看门狗会把用户
                #   主动关掉的服务再拉起来**，语义直接反了。
                #   这条错一直在发生，只是此前 print 到 --noconsole 的虚空里没人看见；
                #   加了文件日志的第一次启动就抓到了它。
                #   路径本来就可以独立求出，不必经过实例。
                root = getattr(
                    getattr(self, "readerpc_paths", None), "local_root", None
                ) or ReaderPCPaths.discover().local_root
                write_user_exit_marker(root)
            except Exception as exc:
                _boot_log(f"[warn] 写退出标记失败(看门狗可能复活): {exc}")
            try:
                with self.service_lock:
                    stop_readerpc_services(
                        self.bridge_paths,
                        self.process_runner,
                        self.pc_ocr,
                    )
            except Exception as exc:
                self.events.put(("shutdown-error", exc))
            else:
                self.events.put(("shutdown-success", None))

        threading.Thread(
            target=worker,
            name="readerpc-shutdown",
            daemon=False,
        ).start()

    def _finish_exit(self) -> None:
        self.closed = True
        history_stop_event = getattr(self, "history_stop_event", None)
        if history_stop_event is not None:
            history_stop_event.set()
        history_thread = getattr(self, "history_thread", None)
        if history_thread is not None and history_thread.is_alive():
            history_thread.join(timeout=3)
        try:
            self.tray.stop()
        finally:
            self.root.destroy()

    def _run_task(
        self,
        label: str,
        action: Callable[[], Any],
        success: str,
    ) -> None:
        if self.busy or self.closing:
            return
        self.busy = True
        self.footer.configure(text=label)
        self._set_buttons_enabled(False)

        def worker() -> None:
            try:
                with self.service_lock:
                    if self.closing:
                        raise ReaderPCServiceError("ReaderPC 正在退出。")
                    result = action()
            except Exception as exc:
                self.events.put(("task-error", exc))
            else:
                self.events.put(("task-success", (success, result)))

        threading.Thread(target=worker, name="readerpc-action", daemon=True).start()

    def _drain_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "shutdown-success":
                    self._finish_exit()
                    return
                if kind == "shutdown-error":
                    self.closing = False
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.show_window()
                    detail = f"无法安全退出：{value}"
                    self.footer.configure(text=detail, foreground="#c62828")
                    messagebox.showerror(PRODUCT_NAME, detail)
                    continue
                if self.closing and kind in {"task-error", "task-success"}:
                    continue
                if kind == "intent-rollback":
                    self.bridge_only.set(
                        value["service_mode"] == SERVICE_MODE_BRIDGE_ONLY
                    )
                    self.voice_enabled.set(value["voice_enabled"])
                    self.snapshot_hidden.set(value["snapshot_hidden"])
                    self._converge_shortcut_broker(
                        voice_shortcut_enabled=value["voice_enabled"]
                    )
                    self._converge_history_monitor(
                        value["voice_enabled"]
                    )
                    self._save_current_preferences()
                    continue
                if kind == "task-error":
                    self.voice_recovery_in_progress = False
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.footer.configure(text=f"操作失败：{value}", foreground="#c62828")
                    self.refresh()
                elif kind == "task-success":
                    self.voice_recovery_in_progress = False
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.footer.configure(text=value[0], foreground="#167347")
                    self.refresh()
                elif kind == "tray-error":
                    self.footer.configure(
                        text=f"托盘不可用，主窗口仍可使用：{value}",
                        foreground="#c62828",
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (self.voice_button, self.pc_button):
            if button is not None:
                button.configure(state=state)

    def _voice_status(self):
        return read_direct_status(self.bridge_paths, self.process_runner)

    # 自动解封的节流：服务在线但语音始终不活时，每 45 秒最多翻转一次 keepalive，
    # 且一轮最多 3 次。C# 那 2 次恢复预算是"每个意图代际"的，翻转 = 换一个代际，
    # 所以这是有效的；但如果根因是配置/设备缺失，翻多少次都不会好 ——
    # 那种情况下界面已经把 lastError 的原因写出来了，交给人处理，而不是无限重试。
    _VOICE_REARM_INTERVAL_S = 45.0
    _VOICE_REARM_MAX = 3

    def _maybe_rearm_codex_voice(self) -> None:
        if (
            not self._voice_enabled()
            or self.busy
            or self.closing
            or self.voice_start_in_progress
        ):
            return
        if getattr(self, "_voice_rearm_count", 0) >= self._VOICE_REARM_MAX:
            return
        now = time.monotonic()
        last = getattr(self, "_voice_rearm_at", 0.0)
        if last and now - last < self._VOICE_REARM_INTERVAL_S:
            return
        # 服务刚起来时先给它一点时间自己连上，别抢在正常启动流程前面翻转。
        started = getattr(self, "last_voice_start_attempt", 0.0)
        if started and now - started < self._VOICE_REARM_INTERVAL_S:
            return
        self._voice_rearm_at = now
        self._voice_rearm_count = getattr(self, "_voice_rearm_count", 0) + 1
        attempt = self._voice_rearm_count

        def run() -> None:
            try:
                flipped = rearm_codex_voice_keep_active(self.bridge_paths)
            except Exception as exc:   # 解封失败不该打断界面，但必须留痕
                _boot_log(f"[warn] Codex 语音解封失败（第 {attempt} 次）: {str(exc)[:160]}")
                return
            _boot_log(
                f"Codex 语音自动解封（第 {attempt}/{self._VOICE_REARM_MAX} 次，"
                f"{'已翻转 keepalive' if flipped else 'keepalive 原本为关，直接置开'}）"
            )

        threading.Thread(target=run, daemon=True).start()

    def _history_status(self) -> ReaderPCHistoryStatus:
        voice = self._voice_status()
        if not self._voice_enabled():
            return ReaderPCHistoryStatus(
                service_online=voice.service_online is True,
                capture_active=False,
                capture_generation=None,
            )
        codex_voice = read_codex_voice_activity()
        return ReaderPCHistoryStatus(
            service_online=voice.service_online is True,
            capture_active=(
                self._voice_enabled() and codex_voice.active is True
            ),
            capture_generation=codex_voice.generation,
        )

    def _bridge_only_enabled(self) -> bool:
        """偏好里的桥接模式;部分构造(测试/早期启动)时按完整模式处理。"""
        var = getattr(self, "bridge_only", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _voice_enabled(self) -> bool:
        """语音可选层开关;旧偏好/旧测试实例默认保持现有开启语义。"""
        var = getattr(self, "voice_enabled", None)
        try:
            return bool(var.get()) if var is not None else True
        except Exception:
            return True

    def _converge_shortcut_broker(
        self,
        *,
        voice_shortcut_enabled: bool,
    ) -> None:
        """语音关闭时不持有 F24;重新开启时再建立唯一 broker。"""

        broker = getattr(self, "_shortcut_broker", None)
        if not voice_shortcut_enabled:
            if broker is not None:
                broker.close()
                self._shortcut_broker = None
            return
        if broker is None:
            self._shortcut_broker = prepare_readerpc_shortcut_broker(
                voice_shortcut_enabled=True
            )

    def close_shortcut_broker(self) -> None:
        broker = getattr(self, "_shortcut_broker", None)
        self._shortcut_broker = None
        if broker is not None:
            broker.close()

    def toggle_voice(self) -> None:
        current = self._voice_status()
        if not current.service_online:
            self._start_voice_task()

    def _start_voice_task(self) -> None:
        if self.busy or self.closing:
            return
        self.last_voice_start_attempt = time.monotonic()
        self.voice_recovery_in_progress = True
        self.voice_start_in_progress = True

        bridge_only = self._bridge_only_enabled()
        voice_enabled = self._voice_enabled()
        intent = self._current_intent_kwargs()

        def start() -> int:
            try:
                self._converge_shortcut_broker(
                    voice_shortcut_enabled=voice_enabled
                )
                pid = enable_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    **intent,
                )
                self.voice_snapshot_offline_marked = False
                return pid
            finally:
                self.voice_start_in_progress = False

        self._run_task(
            "正在恢复 Reader 非语音服务…"
            if not voice_enabled
            else "正在恢复桥接与实时快照服务…"
            if bridge_only
            else "正在恢复电脑语音与实时快照服务…",
            start,
            "Reader 非语音服务已恢复：快照与工具可用，语音保持关闭。"
            if not voice_enabled
            else "桥接与实时快照已恢复（语音未接管）。"
            if bridge_only
            else "电脑语音与实时快照服务已恢复；正在确认 Codex 语音。",
        )

    def _stop_voice_task(self) -> None:
        if self.busy or self.closing or self.voice_stop_in_progress:
            return
        self.voice_stop_in_progress = True

        def stop() -> None:
            try:
                stop_readerpc_voice(
                    self.bridge_paths,
                    self.process_runner,
                    disable_configuration=True,
                )
                self.voice_snapshot_offline_marked = True
            finally:
                self.voice_stop_in_progress = False

        self._run_task(
            "ReaderPC 正在停止电脑语音与实时快照…",
            stop,
            "电脑语音与实时快照已停止。",
        )

    def toggle_pc(self) -> None:
        self._set_pc_running(not self.pc_ocr.status().running)

    def _set_pc_running(self, running: bool) -> None:
        self.keep_pc_online.set(running)
        self._save_current_preferences()
        if running:
            self.last_pc_start_attempt = time.monotonic()
            self._run_task(
                "正在启动 PC 预处理…",
                self.pc_ocr.start,
                "PC 预处理已在线，App 最迟约 20 秒后显示可用。",
            )
        else:
            self._run_task(
                "正在停止 PC 预处理…",
                self.pc_ocr.stop,
                "PC 预处理已停止。",
            )

    def on_keep_pc_changed(self) -> None:
        self._set_pc_running(bool(self.keep_pc_online.get()))

    def on_bridge_only_changed(self) -> None:
        """切模式 = 存偏好 + 停当前直连代际 + 按新模式重启(C# 只在启动时读模式文件)。"""
        bridge_only = bool(self.bridge_only.get())
        self._save_current_preferences()
        self._restart_voice_with_intent(
            "正在切换到仅桥接模式…" if bridge_only else "正在切换到完整模式…",
            "已切到仅桥接模式：语音留在电脑本机，通话不接到 App。"
            if bridge_only
            else "已切回完整模式：通话可接到 App。",
        )

    def on_voice_enabled_changed(self) -> None:
        """只切换语音层;非语音 Direct 底座始终按同一启用意图重新收敛。"""
        enabled = bool(self.voice_enabled.get())
        self._save_current_preferences()
        self._restart_voice_with_intent(
            "正在启用语音功能…"
            if enabled
            else "正在关闭语音功能，Reader 工具保持在线…",
            "语音功能已启用；非语音 Reader 服务未改变。"
            if enabled
            else "语音功能已关闭；快照、视觉、卡片和其它 Reader 工具继续可用。",
        )

    def on_snapshot_hidden_changed(self) -> None:
        hidden = self._snapshot_hidden_enabled()
        self._save_current_preferences()
        self._restart_voice_with_intent(
            "正在应用静默快照设置…",
            "静默快照已开启：后台服务照常，不再显示快照查看器。"
            if hidden
            else "静默快照已关闭：快照查看器恢复显示。",
        )

    def _ensure_pc_online(self) -> None:
        if self.closed or self.closing:
            return
        try:
            pc_running = self.pc_ocr.status().running
            if pc_running:
                self.pc_fail_streak = 0
            backoff = _escalated_backoff(
                PC_RESTART_BACKOFF_SECONDS, self.pc_fail_streak
            )
            if (
                self.keep_pc_online.get()
                and not self.busy
                and not pc_running
                and time.monotonic() - self.last_pc_start_attempt >= backoff
            ):
                self.last_pc_start_attempt = time.monotonic()
                self.pc_fail_streak += 1
                if self.pc_fail_streak >= 3:
                    self.footer.configure(
                        text=(
                            "PC 预处理已连续 %d 次未能保持在线,退避 %d 秒重试。"
                            % (
                                self.pc_fail_streak,
                                int(_escalated_backoff(
                                    PC_RESTART_BACKOFF_SECONDS,
                                    self.pc_fail_streak,
                                )),
                            )
                        ),
                        foreground="#b26a00",
                    )
                self._run_task(
                    "正在让 PC 预处理保持在线…",
                    self.pc_ocr.start,
                    "PC 预处理已恢复在线。",
                )
        except Exception:
            pass
        self.root.after(5_000, self._ensure_pc_online)

    def _reconcile_service_mode_intent(self) -> None:
        """App 经桥请求换模式(C# 只写意图文件);ReaderPC 是唯一的服务生命周期
        所有者,在这里收敛:文件模式 ≠ 当前模式 → 同步 UI/偏好并按新模式重启。
        重启后文件==已应用模式,循环自然静止。"""
        try:
            value = json.loads(
                (self.bridge_paths.runtime_status.parent
                 / "readerpc-service-mode.json").read_text("utf-8")
            )
        except (OSError, ValueError, TypeError):
            return   # TypeError:部分构造(测试 Mock 路径)——按无意图处理
        if not (isinstance(value, dict)
                and value.get("contract") == SERVICE_MODE_CONTRACT):
            return
        mode = value.get("mode")
        if mode not in SERVICE_MODES:
            return
        voice_enabled = value.get("voiceEnabled")
        if not isinstance(voice_enabled, bool):
            # 旧 C# 只会写 mode:不借此改变语音轴。
            voice_enabled = getattr(
                self,
                "_applied_voice_enabled",
                self._voice_enabled(),
            )
        wanted_bridge = mode == SERVICE_MODE_BRIDGE_ONLY
        applied_mode = getattr(
            self,
            "_applied_service_mode",
            self._current_service_mode(),
        )
        applied_voice = getattr(
            self,
            "_applied_voice_enabled",
            self._voice_enabled(),
        )
        if (
            mode == applied_mode
            and voice_enabled == applied_voice
        ):
            return
        if self.busy or self.closing or self.voice_start_in_progress:
            return
        mode_var = getattr(self, "bridge_only", None)
        voice_var = getattr(self, "voice_enabled", None)
        if mode_var is None or voice_var is None:
            return
        mode_var.set(wanted_bridge)
        voice_var.set(voice_enabled)
        self._save_current_preferences()
        self._restart_voice_with_intent(
            "正在应用 App 请求的 ReaderPC 连接/语音设置…",
            "ReaderPC 连接与语音设置已按 App 请求更新。",
        )

    def _ensure_voice_online(self) -> None:
        if self.closed or self.closing:
            return
        try:
            if self._voice_enabled():
                self._voice_orb_tick()
            self._reconcile_service_mode_intent()
            # Do not race the known start transaction: it owns config, process
            # and the first fresh runtime heartbeat. Other busy work (for
            # example PC preprocessing) must not leave a stale ready snapshot.
            if not self.voice_start_in_progress:
                status = self._voice_status()
                if status.service_online:
                    self.voice_snapshot_offline_marked = False
                    self.voice_fail_streak = 0
                else:
                    if not self.voice_snapshot_offline_marked:
                        write_recovering_reader_context_snapshot(
                            self.bridge_paths
                        )
                        self.voice_snapshot_offline_marked = True
                    voice_backoff = _escalated_backoff(
                        VOICE_RESTART_BACKOFF_SECONDS,
                        self.voice_fail_streak,
                    )
                    if (
                        not self.busy
                        and time.monotonic() - self.last_voice_start_attempt
                        >= voice_backoff
                    ):
                        self.voice_fail_streak += 1
                        if self.voice_fail_streak >= 3:
                            self.footer.configure(
                                text=(
                                    "直连服务已连续 %d 次恢复失败(%s),退避 %d 秒重试。"
                                    % (
                                        self.voice_fail_streak,
                                        status.reason or "?",
                                        int(_escalated_backoff(
                                            VOICE_RESTART_BACKOFF_SECONDS,
                                            self.voice_fail_streak,
                                        )),
                                    )
                                ),
                                foreground="#b26a00",
                            )
                        self._start_voice_task()
        except (BridgeError, ReaderPCServiceError, OSError, ValueError) as exc:
            self.footer.configure(
                text=f"电脑语音离线状态确认失败：{exc}",
                foreground="#c62828",
            )
        finally:
            self.root.after(5_000, self._ensure_voice_online)

    def open_legacy_voice_settings(self) -> None:
        executable = self.bridge_paths.desktop_launcher
        if not executable.is_file():
            messagebox.showerror(PRODUCT_NAME, "未找到已安装的电脑语音详细设置。")
            return
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )

    def refresh(self) -> None:
        if self.closed:
            return
        try:
            voice = self._voice_status()
            voice_enabled = self._voice_enabled()
            codex_voice = (
                read_codex_voice_activity()
                if voice_enabled
                else CodexVoiceActivityStatus(
                    "disabled",
                    False,
                    None,
                )
            )
            pc = self.pc_ocr.status()
            context = read_reader_context_status(
                self.bridge_paths.root / "runtime" / "reader-context-snapshot.json"
            )
            bridge_only = self._bridge_only_enabled()
            if not voice.service_online:
                voice_label = (
                    "正在恢复"
                    if self.voice_recovery_in_progress
                    else "离线 · 等待重试"
                )
                voice_color = "#b26a00"
            elif not voice_enabled:
                voice_label = "Reader 服务在线 · 语音功能已关闭"
                voice_color = "#167347"
            elif bridge_only:
                # 桥接模式:语音在本机跑(保活照常),通话不接到 App。
                if codex_voice.active is True:
                    voice_label = "桥接模式 · Codex 语音本机运行中"
                    voice_color = "#167347"
                else:
                    voice_label = "桥接模式 · 正在确认本机语音"
                    voice_color = "#b26a00"
            elif codex_voice.active is True:
                voice_label = "在线 · Codex 语音工作中"
                voice_color = "#167347"
                self._voice_rearm_count = 0   # 起来了就把解封预算还回去
            else:
                # 失败原因本来就写在 runtime-status 里，只是从来没人读（见 describe_voice_failure）。
                # last_error 是 DirectStatus 的可选字段(默认 None);读不到就当没有,
                # 一个诊断字段缺席不该把整个状态面板打挂。
                reason = describe_voice_failure(getattr(voice, "last_error", None))
                voice_label = (
                    "直连在线 · 等待 Codex 语音"
                    if codex_voice.status == "available"
                    else "直连在线 · 无法确认 Codex 语音"
                )
                if reason:
                    voice_label += f"：{reason}"
                voice_color = "#b26a00"
                # 服务在线却始终等不到语音 = 多半是 C# 的自动恢复已被封锁；
                # 只有 keepalive 的 false→true 跃迁能解开它（见 rearm_codex_voice_keep_active）。
                self._maybe_rearm_codex_voice()
            self.voice_status.configure(text=voice_label, foreground=voice_color)
            self.voice_detail.configure(
                text=(
                    f"Reader 已连接 · PID {voice.pid}"
                    if voice.reader_connected
                    else f"等待 Reader 连接 · {voice.reason}"
                )
            )
            if self.voice_button is not None:
                self.voice_button.configure(text="立即重试")

            if not voice.service_online:
                context_label = "等待电脑语音服务恢复"
                context_color = "#b26a00"
                context_detail = "服务恢复后会自动重新接收 Reader 快照。"
            else:
                context_label = context.state_label
                context_color = "#167347" if context.fresh else "#6b7280"
                context_detail = (
                    f"{context.kind or '内容'} · {context.title}"
                    if context.title
                    else "打开 Reader App 或启用扩展后会在这里显示。"
                )
            self.context_status.configure(
                text=context_label,
                foreground=context_color,
            )
            self.context_detail.configure(text=context_detail)

            self.pc_status.configure(
                text=pc.state_label,
                foreground="#167347" if pc.running else "#6b7280",
            )
            details = format_pc_progress(pc)
            if pc.error:
                details = pc.error
            elif not details:
                details = (
                    "环境已就绪；空闲时不加载模型和显存。"
                    if pc.source_ready
                    else "未找到签发运行文件或 PC OCR Python 环境。"
                )
            self.pc_detail.configure(text=details)
            self.pc_button.configure(text="停止" if pc.running else "启动")

            now = time.monotonic()
            if now - self.last_status_publish >= STATUS_PUBLISH_INTERVAL_SECONDS:
                write_readerpc_status(
                    self.readerpc_paths.status_file,
                    voice={
                        "online": voice.service_online,
                        "configured": voice.configuration_enabled,
                        # 保留历史 full/bridge-only 投影;语音关闭时两者都为 false。
                        "intentEnabled": voice_enabled and not bridge_only,
                        "codexVoiceStatus": codex_voice.status,
                        "codexVoiceActive": codex_voice.active,
                        "readerConnected": voice.reader_connected,
                        "captureActive": voice.capture_active,
                        "reason": voice.reason,
                    },
                    context=context,
                    pc_ocr=pc,
                )
                self.last_status_publish = now
        except (BridgeError, ReaderPCServiceError, OSError, ValueError) as exc:
            self.footer.configure(text=f"状态读取失败：{exc}", foreground="#c62828")
        self.root.after(POLL_INTERVAL_MS, self.refresh)


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\BWReader.ReaderPC.Server")
        if not handle:
            return False
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True


def _boot_log(message: str) -> None:
    """把故障写进**文件**，而不是 print 到虚空。

    ReaderPC-Server 是 PyInstaller `--noconsole` 单文件打包，运行时
    `sys.stdout is None` —— 代码里那几处 `_boot_log("[warn] …")` 是彻底的空操作。
    于是"自启收敛失败""写退出标记失败""清退出标记失败"这类事永远无人知晓，
    而看门狗每 5 分钟原样重来一次。2026-08-18 查自启那两个 bug 时，最贵的
    一段时间正是花在"没有任何线索"上。

    与 autostart.log 同目录、同格式，一眼能按时间对上。
    写日志失败一律吞掉：诊断通道绝不能反过来打断被诊断的程序。
    """
    try:
        path = Path(ReaderPCPaths.discover().local_root) / "readerpc-server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.write_text("", encoding="utf-8")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(stamp + " " + message + "\n")
    except Exception:
        pass


def terminate_stale_instances(
    bridge_paths: BridgePaths | None = None,
    process_runner: WindowsProcessRunner | None = None,
    *,
    process_rows: list[tuple[int, int, str, str]] | None = None,
    close_process: Callable[[int], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = 60.0,
) -> list[int]:
    """Hand over from an older ReaderPC without bypassing Direct cleanup.

    The old `/F` takeover could leave Codex Voice, per-app routes or media
    leases behind.  Ask only the exact old ReaderPC GUI generations to close,
    wait for them, then route any remaining exact Direct generation through
    the instance-bound shutdown receipt.  Any unconfirmed step aborts the new
    startup before it creates F24 or another Direct generation.
    """

    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ReaderPCServiceError("ReaderPC 接管等待参数无效。")
    if process_rows is None:
        if os.name != "nt":
            return []
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { "
                    "$_.Name -eq 'ReaderPC-Server.exe' -or "
                    "$_.Name -eq 'bw-computer-voice-audio.exe' "
                    "} | ForEach-Object { "
                    "\"$($_.ProcessId)|$($_.ParentProcessId)|"
                    "$($_.Name)|$($_.CommandLine)\" }",
                ],
                capture_output=True,
                text=True,
                timeout=25,
                creationflags=0x08000000,
            )
        except Exception as exc:
            raise ReaderPCServiceError(
                "无法枚举旧 ReaderPC 与 Direct；拒绝盲目接管。"
            ) from exc
        if completed.returncode != 0:
            raise ReaderPCServiceError(
                "无法枚举旧 ReaderPC 与 Direct；拒绝盲目接管。"
            )
        rows: list[tuple[int, int, str, str]] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.strip().split("|", 3)
            if len(parts) < 3:
                continue
            try:
                rows.append(
                    (
                        int(parts[0]),
                        int(parts[1]),
                        parts[2],
                        parts[3] if len(parts) > 3 else "",
                    )
                )
            except ValueError:
                continue
    else:
        rows = list(process_rows)

    bridge_paths = bridge_paths or BridgePaths.discover()
    process_runner = process_runner or WindowsProcessRunner()

    def same_executable(left: Path, right: Path) -> bool:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(
            str(right.resolve())
        )

    try:
        captured_direct = inspect_direct_shutdown_identity(
            bridge_paths,
            process_runner,
        )
    except BridgeError as exc:
        raise ReaderPCServiceError(str(exc)) from exc
    if (
        captured_direct is not None
        and captured_direct.process_live
        and captured_direct.service_instance_id is None
    ):
        raise ReaderPCServiceError(
            "旧 Direct 仍在运行，但无法认证服务代际；拒绝接管。"
        )

    # A service record is the only authority for a live Direct generation.
    # CIM still enumerates exact --direct-serve processes so an orphan cannot
    # be silently ignored and collide with the replacement listener later.
    for pid, _ppid, name, cmdline in rows:
        if (
            (name or "").lower() != "bw-computer-voice-audio.exe"
            or "--direct-serve" not in (cmdline or "").lower()
        ):
            continue
        observed = process_runner.executable_for_pid(pid)
        if observed is None:
            continue
        if not same_executable(observed, bridge_paths.native_host):
            raise ReaderPCServiceError(
                f"Direct 候选 {pid} 的程序路径不符；拒绝接管。"
            )
        if captured_direct is None or pid != captured_direct.pid:
            raise ReaderPCServiceError(
                f"发现未被严格服务记录认证的 Direct {pid}；拒绝强杀或接管。"
            )

    by_pid = {row[0]: row for row in rows}
    mine = {os.getpid()}
    cursor = os.getpid()
    for _ in range(8):
        row = by_pid.get(cursor)
        if row is None:
            break
        mine.add(row[0])
        cursor = row[1]
    # PyInstaller onefile has a bootloader parent and an app child.  Mark the
    # whole current generation, even if CIM returned rows out of order.
    changed = True
    while changed:
        changed = False
        for pid, ppid, _name, _cmd in rows:
            if ppid in mine and pid not in mine:
                mine.add(pid)
                changed = True

    candidates = [
        pid
        for pid, _ppid, name, _cmdline in rows
        if pid not in mine and (name or "").lower() == "readerpc-server.exe"
    ]
    candidate_set = set(candidates)
    parent_pids = {
        ppid
        for pid, ppid, name, _cmdline in rows
        if pid in candidate_set
        and ppid in candidate_set
        and (name or "").lower() == "readerpc-server.exe"
    }
    # Only the inner/leaf process owns the GUI window.  Sending taskkill to the
    # onefile bootloader parent would fail despite a healthy graceful exit.
    close_targets = [pid for pid in candidates if pid not in parent_pids]
    original_gui_executables = {
        pid: process_runner.executable_for_pid(pid) for pid in candidates
    }

    def original_gui_live(pid: int) -> bool:
        expected = original_gui_executables.get(pid)
        observed = process_runner.executable_for_pid(pid)
        return bool(
            expected is not None
            and observed is not None
            and same_executable(observed, expected)
        )

    def request_close(pid: int) -> bool:
        if close_process is not None:
            return bool(close_process(pid))
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                creationflags=0x08000000,
            )
            return result.returncode == 0
        except Exception:
            return False

    for pid in close_targets:
        if original_gui_live(pid) and not request_close(pid):
            if original_gui_live(pid):
                raise ReaderPCServiceError(
                    f"旧 ReaderPC {pid} 未接受正常退出请求；拒绝强制接管。"
                )

    deadline = monotonic() + timeout_seconds
    while any(original_gui_live(pid) for pid in candidates):
        if monotonic() >= deadline:
            raise ReaderPCServiceError(
                "旧 ReaderPC 未完成正常退出；拒绝强制接管。"
            )
        sleeper(0.2)

    # The service record may disappear while the old GUI is completing its
    # own stop path.  Always inspect the captured original PID itself; never
    # interpret record disappearance as cleanup success.
    try:
        current_direct = inspect_direct_shutdown_identity(
            bridge_paths,
            process_runner,
        )
    except BridgeError as exc:
        raise ReaderPCServiceError(str(exc)) from exc
    if captured_direct is None:
        if current_direct is not None and current_direct.process_live:
            raise ReaderPCServiceError(
                "接管期间出现了未认证的新 Direct 代际；拒绝继续启动。"
            )
        if current_direct is not None:
            clear_direct_service_record_if_pid(
                bridge_paths,
                current_direct.pid,
            )
    elif not captured_direct.process_live:
        observed = process_runner.executable_for_pid(captured_direct.pid)
        if observed is not None:
            raise ReaderPCServiceError(
                "已退出的 Direct PID 在接管期间被复用；拒绝继续启动。"
            )
        clear_direct_service_record_if_pid(
            bridge_paths,
            captured_direct.pid,
        )
    else:
        instance_id = captured_direct.service_instance_id
        assert instance_id is not None
        if current_direct is not None and current_direct.pid != captured_direct.pid:
            raise ReaderPCServiceError(
                "接管期间 Direct 服务记录切换了代际；拒绝继续启动。"
            )
        observed = process_runner.executable_for_pid(captured_direct.pid)
        captured_live = bool(
            observed is not None
            and same_executable(observed, bridge_paths.native_host)
        )
        if observed is not None and not captured_live:
            # PID reuse means the old process is gone, but receipt proof is
            # still mandatory before accepting its resource cleanup.
            captured_live = False
        if captured_live and current_direct is not None:
            try:
                stop_direct_service(
                    bridge_paths,
                    process_runner,
                    graceful=True,
                    force_on_cleanup_failure=False,
                )
            except BridgeError as exc:
                raise ReaderPCServiceError(str(exc)) from exc
        elif captured_live:
            # The old owner removed its record while Direct was still
            # unwinding.  Do not issue an unauthenticated second stop and do
            # not return until the captured PID exits with its own success.
            direct_deadline = monotonic() + timeout_seconds
            while True:
                receipt_state = read_direct_shutdown_receipt_state(
                    bridge_paths,
                    instance_id,
                )
                if receipt_state == "failed":
                    raise ReaderPCServiceError(
                        "旧 Direct 报告退出清理失败；拒绝接管。"
                    )
                observed = process_runner.executable_for_pid(
                    captured_direct.pid
                )
                if observed is None or not same_executable(
                    observed,
                    bridge_paths.native_host,
                ):
                    break
                if monotonic() >= direct_deadline:
                    raise ReaderPCServiceError(
                        "旧 Direct 未完成退出清理；拒绝强制接管。"
                    )
                sleeper(0.2)
        if process_runner.executable_for_pid(captured_direct.pid) is not None:
            observed = process_runner.executable_for_pid(captured_direct.pid)
            if observed is not None and same_executable(
                observed,
                bridge_paths.native_host,
            ):
                raise ReaderPCServiceError(
                    "旧 Direct 仍在运行；拒绝启动新代际。"
                )
        if read_direct_shutdown_receipt_state(
            bridge_paths,
            instance_id,
        ) != "success":
            raise ReaderPCServiceError(
                "旧 Direct 已退出，但没有可验证的清理成功回执；拒绝接管。"
            )
        clear_direct_service_record_if_pid(
            bridge_paths,
            captured_direct.pid,
        )

    if candidates:
        _boot_log("启动接管：旧 ReaderPC 已正常退出 " + repr(candidates))
    return candidates


def autostart_script_checks() -> dict[str, bool]:
    """把自启脚本真写一遍，然后**让解释器自己说它认不认**。

    2026-08-18 一晚上两个自启 bug 都是这一层漏的，而且都不是逻辑错，是编码错：
      · ps1 少了 BOM → PowerShell 5.1 按 GBK 解中文 → 解析失败；
      · vbs 多了 BOM → VBScript 把 BOM 当第一个字符 → "(1,1) 无效字符"，整个不执行。
    两者都"生成成功"，文件也都在，自测全绿 —— 因为此前只检查了路径存不存在。
    文件存在从来不等于解释器能读；这里补的就是这一步。

    写进临时目录，绝不碰已安装的那两个脚本。
    """

    import tempfile

    checks = {
        "autostart-ps1-has-bom": False,
        "autostart-vbs-no-bom": False,
        "autostart-vbs-ascii": False,
        "autostart-ps1-parses": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="readerpc-selftest-") as temp:
            root = Path(temp)
            vbs = write_autostart_scripts(root)
            # 两个脚本落在 write_autostart_scripts 自己建的子目录里，
            # 从返回的 vbs 反推同目录，别再自己拼一遍路径。
            ps1 = vbs.parent / "start-readerpc.ps1"
            ps1_bytes = ps1.read_bytes()
            vbs_bytes = vbs.read_bytes()
            checks["autostart-ps1-has-bom"] = ps1_bytes.startswith(b"\xef\xbb\xbf")
            checks["autostart-vbs-no-bom"] = not vbs_bytes.startswith(b"\xef\xbb\xbf")
            # VBScript 对非 ASCII 的处理跟代码页绑死；自启脚本里本来就不该有中文。
            try:
                vbs_bytes.decode("ascii")
                checks["autostart-vbs-ascii"] = True
            except UnicodeDecodeError:
                checks["autostart-vbs-ascii"] = False
            if os.name == "nt":
                # 让 PowerShell 自己解析：这比任何我们写的规则都权威。
                probe = (
                    "$e=$null;"
                    "[void][System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{ps1}',[ref]$null,[ref]$e);"
                    "if($e -and $e.Count -gt 0){exit 1}else{exit 0}"
                )
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
                    capture_output=True,
                    timeout=60,
                    creationflags=0x08000000,
                )
                checks["autostart-ps1-parses"] = completed.returncode == 0
            else:
                checks["autostart-ps1-parses"] = True   # 非 Windows 上无从校验
    except Exception as exc:
        _boot_log("[warn] 自启脚本自测失败: " + str(exc)[:160])
    return checks


def self_test_report() -> dict[str, Any]:
    paths = ReaderPCPaths.discover()
    pc_paths = PcOcrServiceController().paths
    checks = {
        "preferences-path-absolute": paths.preferences_file.is_absolute(),
        "status-path-absolute": paths.status_file.is_absolute(),
        "pc-runtime-discovered": pc_paths.project_root is not None,
        "pc-python-present": pc_paths.python_exe.is_file(),
        "voice-install-present": BridgePaths.discover().native_host.is_file(),
    }
    checks.update(autostart_script_checks())
    return {
        "contract": "readerpc-server-self-test/1",
        "ok": all(checks.values()),
        "version": APP_VERSION,
        "checks": checks,
        "servicesStarted": False,
        "audioOpened": False,
        "modelsLoaded": False,
        "gpuAllocated": False,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--self-test"]:
        report = self_test_report()
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if arguments:
        return 2
    # 单实例：**接管**而不是退让。先清掉同角色旧进程，再拿互斥体。
    # 旧行为是拿不到锁就 return 0 静默退出，旧实例继续占着 F24 broker、
    # runtime 状态文件与 Direct 服务代际 —— 用户反复要求的正是反过来。
    bridge_paths = BridgePaths.discover()
    process_runner = WindowsProcessRunner()
    try:
        stale = terminate_stale_instances(bridge_paths, process_runner)
    except Exception as exc:
        _boot_log(
            "启动接管失败: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        )
        raise
    instance = SingleInstance()
    if not instance.acquire():
        # 清理过仍拿不到 = 旧进程句柄还没释放。等一下再试；仍失败就退出，
        # 但**留下痕迹** —— 这一步静默失败会让人以为"双击没反应"。
        time.sleep(1.5)
        if not instance.acquire():
            _boot_log("互斥体仍被占用（已清理 " + repr(stale) + "），本次放弃启动")
            return 0
    # ReaderPC is the sole lifecycle owner. Retire the old logon bootstrap and
    # replace any ownerless Direct generation. F24 belongs only to the optional
    # voice layer, so persisted voice-off must be read before creating it.
    readerpc_paths = ReaderPCPaths.discover()
    preferences = load_preferences(readerpc_paths.preferences_file)
    applied_preferences = merge_preferences_with_service_intent(
        preferences,
        bridge_paths,
    )
    if applied_preferences != preferences:
        persist_preferences(
            readerpc_paths.preferences_file,
            applied_preferences,
        )
        preferences = applied_preferences
    broker = prepare_readerpc_shortcut_broker(
        voice_shortcut_enabled=bool(preferences["voiceEnabled"])
    )
    window: ReaderPCWindow | None = None
    try:
        root = tk.Tk()
        window = ReaderPCWindow(
            root,
            bridge_paths=bridge_paths,
            process_runner=process_runner,
            readerpc_paths=readerpc_paths,
            shortcut_broker=broker,
        )
        broker = None  # ownership transferred to the window
        root.mainloop()
    finally:
        if window is not None and not window.closed:
            stop_readerpc_services(
                window.bridge_paths,
                window.process_runner,
                window.pc_ocr,
            )
        if window is not None:
            window.close_shortcut_broker()
        elif broker is not None:
            broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
