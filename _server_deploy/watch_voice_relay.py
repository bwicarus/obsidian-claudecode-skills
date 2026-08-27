#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手表 ↔ Windows 语音桥的 **Pi 侧进程入口**（独立 asyncio，监听 127.0.0.1:8768）。

    Apple Watch（自己的界面 + 活动音频会话解禁 WebSocket）
       ── WSS（Funnel，公网 + OAuth）──→ **本进程**（Pi）
       ── WSS（tailnet）──→ Windows 语音桥

**为什么**长这样，权威在 `references/watch-voice-bridge.md`；手表侧的取舍在
`references/watch-companion.md`；线格式与节拍的纯函数层在 `watch_voice_wire.py`。
本文件只负责两侧 socket 与控制面，不重复那三份里的论证。

⚠ 本进程**不 import app.py、不持 DB handle、不碰 vault、不认 session cookie**。
它只做一件事：把手表那条腿和 Windows 那条腿接起来。

⚠ 与 `watch_voice_wire.py` **必须同批部署**：只上其中一个，表现不是报错，是通话建立
后立刻被对端掐掉（Windows 对上行序号 fail-closed）。清单已把两者钉成 release 不变量。

────────────────────────────────────────────────────────────────────────
铁律一：Pi 是「减震器」，不是「转发器」
────────────────────────────────────────────────────────────────────────
上行序号与时间戳由 Pi 自己产生（`wire.UplinkSequencer`，藏在 `WatchVoiceUplink`
里），按 50 Hz **恒定节拍**推给 Windows，与手表在不在线无关。手表的帧只决定「这一
拍填什么」，没来就填静音（尾部淡出防咔哒），迟到即丢。

    严格的一侧永不断，容错的一侧随便断。

本文件在这条铁律上只负责一件事：**把节拍打准**。`tick()` 必须每 20 ms 恰好被调用
一次，多调一次就是把音频加速、少调一次就是一个可闻的断续。所以节拍器**按绝对时刻
排程**（`loop.time()` 基准 + 目标时刻），不是 `sleep(20ms)` 累加 —— 手表侧实测过后
者会漂到 1/3 速率。追不上时**出声**（`pacer.slip`）再重锚，绝不默默变慢。

────────────────────────────────────────────────────────────────────────
铁律二：零转发（结构性的，不是白名单）
────────────────────────────────────────────────────────────────────────
手表能发的只有一个**枚举**（`start` / `stop` / `ping`）和 PCM 帧；发往 Windows 的每
一条消息由本文件自己拼字面量；两者之间**不共享任何数据结构**。

落地点在代码里有三处，都标了「铁律二落地点」：

  一、`_parse_watch_command()` 的返回类型是 `WatchCommand` **枚举** —— 枚举成员装不
      下数据，所以「把手表来的 JSON 塞给 Windows」这条路径在**类型上**不存在。手表
      的 JSON 对象恰好只允许一个键 `op`，连一个自由字段都没有。
  二、`_hello_message` / `_start_message` / `_stop_message` / `_heartbeat_message`
      四个字面量构造器，入参全是 `str` / `int` 标量；`_WindowsLeg._send_json()` 是
      唯一的文本发送口，且断言消息类型与键集合（对应 C# 侧 RequireExactKeys）。
  三、音频也不透传：手表帧的 seq/ts/session 三个字段全部被 wire 丢弃，只有 1920 字
      节载荷过河，再由 Pi 自己的序号重新编码。

  启动自检 `_verify_isolation()` 会真的喂进 `{"op":"start","extra":1}` 和
  `{"op":"anki-card-operation-local"}`，**没被拒绝就拒绝启动**。日后谁把解析器放宽，
  进程当场起不来 —— 这正是 CLAUDE.md「白名单往往有多份副本」那条教训的正面版本：
  **不存在的代码路径不会被顺手加一条绕过。**

────────────────────────────────────────────────────────────────────────
手表腿线格式（本文件定义；手表侧照此实现）
────────────────────────────────────────────────────────────────────────
URL      : wss://<pi-funnel>/watch-voice   （`tailscale serve --set-path=/watch-voice 8768`）
鉴权     : `Authorization: Bearer <token>`（首选）或 `?token=`（受限客户端）
文本上行 : {"op":"start"} / {"op":"stop"} / {"op":"ping"}   —— **精确单键**，多一个即拒
二进制上行: 一帧 `wire.encode_watch_frame(streamId, …)`（1956 字节，magic `BWWV`）。
           `streamId` 由 Pi 在 `hello` 事件里下发，**每条连接一个新的**；用旧的会被
           判 FOREIGN 丢掉。手表自己的 seq 只用于本地排序去重，过不了河。
文本下行 : {"ev":"hello",…} / {"ev":"state",…} / {"ev":"pong",…}
           {"ev":"stats",…} / {"ev":"warning",…} / {"ev":"error","code":…,"message":…}
二进制下行: **裸 1920 字节 PCM 载荷**（s16le / 48 kHz / 单声道 / 20 ms）。朝手表这一
           侧允许丢帧，不带序号——它是容错的一侧，给它序号只会诱使它去做 fail-closed。
关闭码   : 4401 未授权 / 4403 应急闸 / 4409 被新连接顶替 / 4400 协议违规
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import enum
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

wire: Any = None                       # 由 _load_wire() 填上（单测可直接赋桩）


class RelayStartupError(RuntimeError):
    """启动前的致命配置/依赖错误。**只在 main() 抛**，永远不在会话中途抛。"""


# ══════════════════════════════════════════════════════════════════════════════
# Windows 直连合同
#
# ⚠ 地址与 Origin 是**常量，写死在代码里，不从环境变量 / 命令行读**。
#   可配置的目标地址是一个能被改指向的攻击面：Windows 那头认的是「你在 tailnet
#   里」，一旦 Pi 能被指向别处，等于把那条未鉴权的命令入口交出去。这条链路用不上
#   那点灵活性。（`references/watch-voice-bridge.md`「安全边界」节）
# ══════════════════════════════════════════════════════════════════════════════
CONTRACT = "reader-computer-voice-direct/1"
PROTOCOL_VERSION = 3
WINDOWS_ENDPOINT = "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1"
WINDOWS_ORIGIN = "https://bwicarus.taile44d0c.ts.net"   # 已实测在桥的 Origin 白名单内

FRAME_PERIOD_SECONDS = 0.020
HEARTBEAT_INTERVAL_SECONDS = 5.0                        # ClientHeartbeatIntervalMilliseconds
HEARTBEAT_REQUEST_TIMEOUT = 7.0                         # requestTimeoutNanoseconds
HEARTBEAT_STRIKES = 2                                   # 单次超时不判死，连续两次才判
REQUEST_TIMEOUT = 7.0
START_REQUEST_TIMEOUT = 45.0                            # START 会拉起 Codex 并等就绪窗口
OPEN_TIMEOUT = 6.0
MAX_MESSAGE_BYTES = 256 * 1024                          # DirectBridgeContract.MaximumMessageBytes
MAX_PENDING_REQUESTS = 16
# ⚠ hello 之后 30 s 内必须发出 START，且那个期限一旦设定**永不重置**
#   （DirectConnectionPhaseDeadline.cs:20-31）。所以**一通电话 = 一条连接**，绝不复用。
START_DEADLINE_SECONDS = 30.0

# ── 节拍器 ───────────────────────────────────────────────────────────────────
PACER_CATCHUP_LIMIT = 0.060      # 落后 ≤3 帧：不睡，直接补上
PACER_SLIP_LOG_INTERVAL = 1.0    # 打滑日志限流（**计数不限流**）

# ── 手表腿 ───────────────────────────────────────────────────────────────────
WATCH_PROTOCOL = 1
WATCH_MAX_MESSAGE_BYTES = 4096   # 一帧 1956 + 控制帧；再大就是噪声，早点拒
WATCH_PING_INTERVAL = 20
WATCH_DOWNLINK_QUEUE = 25        # 500 ms；满了丢最老的（容错的一侧随便断）
WATCH_BAD_MESSAGE_LIMIT = 8      # 连续协议违规到这个数就断开，防噪声连接常驻
WATCH_COMMAND_MAX_CHARS = 256
# 手表掉线后通话的宽限期：实测自愈约 5 秒，45 秒足够覆盖，又不至于把一支活麦
# 无限期留在用户 PC 上。
WATCH_GRACE_SECONDS = 45.0

CLOSE_UNAUTHORIZED = 4401
CLOSE_KILLED = 4403
CLOSE_DISPLACED = 4409
CLOSE_PROTOCOL = 4400

STATS_INTERVAL_SECONDS = 10.0
KILL_POLL_SECONDS = 1.0

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768                                     # deploy_reader.sh 的 WATCH_VOICE_PORT
CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
DEFAULT_TOKEN_FILE = Path("~/.config/watch-voice-token").expanduser()
DEFAULT_KILL_FILE = Path("~/.config/watch-voice.disabled").expanduser()
DEFAULT_LOG_FILE = CLAUDE_DIR / "state" / "logs" / "watch-voice.jsonl"
MIN_TOKEN_LENGTH = 32


# ══════════════════════════════════════════════════════════════════════════════
# 结构化日志
#
# `references/silent-failure-lessons.md` 规则一：**每个提前退出都要出声**；规则二：
# 折成布尔前先报原始值。所以下面每个 `return` 前都有一行日志，拒绝理由带上原始观测
# 值（长度、类型、字节数、状态字）。手表上没有控制台，不上报等于不可诊断。
#
# ⚠ **永不记录音频内容**。音频只以「多少帧 / 丢了几帧 / 缓冲多深」的形式出现。
# ══════════════════════════════════════════════════════════════════════════════
_LOG_PATH: Optional[Path] = None
_LOG_FILE_BROKEN = False


def _log(name: str, /, **fields: Any) -> None:
    # ⚠ `name` 是**仅位置参数**（那个 `/`）：日志字段里天然会出现 `event=` / `name=`
    #   这种键，普通形参会被它撞成 TypeError —— 而这个 TypeError 会从读循环里冒出来，
    #   表现成「Windows 连接莫名断开」，跟真断线一模一样。别去掉那个斜杠。
    record = {"ts": int(time.time() * 1000), "ev": name}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    print(line, file=sys.stderr, flush=True)          # journald 那条出口
    global _LOG_FILE_BROKEN
    if _LOG_PATH is None or _LOG_FILE_BROKEN:
        return
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as error:                        # noqa: BLE001
        _LOG_FILE_BROKEN = True                       # 只喊一次，别把 stderr 淹了
        print(json.dumps({
            "ts": int(time.time() * 1000), "ev": "log.file_unavailable",
            "path": str(_LOG_PATH), "detail": "%s: %s" % (type(error).__name__, error),
        }, ensure_ascii=False), file=sys.stderr, flush=True)


def _close_info(error: ConnectionClosed) -> tuple:
    """从 `ConnectionClosed` 里取关闭码/原因，**跨 websockets 版本都不炸**。

    ⚠ `error.code` / `error.reason` 自 websockets 13.1 起已弃用（本仓库实测会打
    DeprecationWarning），迟早会被删。删掉那天，这两行会在 `except ConnectionClosed`
    的**处理体内部**抛 AttributeError —— 也就是说，负责「把断线说出来」的那段代码
    自己成了新的沉默源：日志不出，`_fail()` 也走不到。诊断路径本身不能是会炸的
    那一条（`silent-failure-lessons.md` 规则三）。
    """
    for attr in ("rcvd", "sent"):
        frame = getattr(error, attr, None)
        if frame is not None:
            return getattr(frame, "code", None), str(getattr(frame, "reason", ""))
    try:                                              # 老版本只有这两个属性
        return error.code, str(error.reason)
    except Exception:                                 # noqa: BLE001
        return None, ""


def _fingerprint(secret: str) -> str:
    """给日志用的不可逆指纹：能比对两个 token 是不是同一个，但泄露不出内容。"""
    import hashlib                                    # noqa: PLC0415（只在这里用）
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


REQUIRED_WIRE_SYMBOLS = (
    "WatchVoiceUplink", "DownlinkSequenceGuard", "decode_downlink_frame",
    "new_session_id", "WireError", "PCM_FRAME_BYTES", "PCM_PAYLOAD_BYTES",
    "FRAME_DURATION_US", "SAMPLE_RATE_HZ", "MALFORMED", "FOREIGN",
)


def _load_wire() -> None:
    """把 wire 模块装进来并核对常量。装不上 / 对不上就**拒绝启动**并说清楚缺什么。"""
    global wire
    if wire is not None:                              # 单测注入的桩：尊重它，不覆盖
        return
    try:
        import watch_voice_wire as _wire              # noqa: PLC0415
    except Exception as error:                        # noqa: BLE001
        raise RelayStartupError(
            "watch_voice_wire 导入失败（%s: %s）—— 本进程只做接线，帧编解码与节拍在那个"
            "模块里。缺了它宁可不起来，也不给一个「连上了但一句话都传不过去」的假活。"
            % (type(error).__name__, error)
        ) from error
    # ⚠ 这张表必须覆盖本文件用到的**每一个** `wire.X`。漏一个的表现不是启动报错，
    #   而是运行到那一行才 AttributeError —— 而那一行在 handler 里，只会变成
    #   「手表连上了，然后什么都不发生」。`tests/test_watch_voice_relay.py` 的
    #   `test_every_wire_symbol_used_is_declared_required` 会从本文件源码把 `wire.X`
    #   全抓出来跟这张表比对，所以它不会再悄悄漂移。
    missing = [name for name in REQUIRED_WIRE_SYMBOLS if not hasattr(_wire, name)]
    if missing:
        raise RelayStartupError("watch_voice_wire 缺少必需符号：%s" % ", ".join(missing))
    # 漂移闸：本文件的节拍常量与 wire 的帧时长必须是同一个 20 ms。两边各自改一个数
    # 而没人发现，表现是音频快放/慢放，没有任何报错。
    if _wire.FRAME_DURATION_US != int(FRAME_PERIOD_SECONDS * 1_000_000):
        raise RelayStartupError(
            "节拍常量漂移：wire.FRAME_DURATION_US=%d 与本文件 FRAME_PERIOD_SECONDS=%s 不一致"
            % (_wire.FRAME_DURATION_US, FRAME_PERIOD_SECONDS))
    if _wire.PCM_FRAME_BYTES >= WATCH_MAX_MESSAGE_BYTES:
        raise RelayStartupError(
            "手表帧 %d 字节放不进 %d 的接收上限" % (_wire.PCM_FRAME_BYTES,
                                                    WATCH_MAX_MESSAGE_BYTES))
    wire = _wire


# ══════════════════════════════════════════════════════════════════════════════
# ⚠ 铁律二落地点（一）：手表命令
#
# 解析器的**返回类型是枚举**。枚举成员装不下数据，所以从这里往后，手表送来的任何
# 字节都不可能变成发往 Windows 的字段。控制帧只允许一个键 `op` —— 不是「过滤掉危险
# 字段」，是**根本没有字段**。
# ══════════════════════════════════════════════════════════════════════════════
class WatchCommand(enum.Enum):
    START = "start"
    STOP = "stop"
    PING = "ping"


class WatchProtocolError(ValueError):
    """手表侧协议违规。code/message 给手表看，detail 给日志看。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _parse_watch_command(raw: str) -> WatchCommand:
    """手表文本帧 → `WatchCommand`。**这是手表数据在本进程里能走到的最远处。**"""
    if len(raw) > WATCH_COMMAND_MAX_CHARS:
        raise WatchProtocolError("BW_WATCH_VOICE_MESSAGE_TOO_LARGE",
                                 "控制帧过大", chars=len(raw))
    try:
        parsed = json.loads(raw)
    except Exception as error:                        # noqa: BLE001
        raise WatchProtocolError("BW_WATCH_VOICE_JSON_INVALID", "控制帧不是合法 JSON",
                                 detail="%s: %s" % (type(error).__name__, error)) from error
    if not isinstance(parsed, dict):
        raise WatchProtocolError("BW_WATCH_VOICE_SHAPE_INVALID", "控制帧必须是对象",
                                 got=type(parsed).__name__)
    keys = set(parsed)
    if keys != {"op"}:
        # 精确单键。多一个字段就拒 —— 这一条是铁律二的地基，别放宽。
        raise WatchProtocolError("BW_WATCH_VOICE_FIELDS_INVALID",
                                 "控制帧只允许 op 一个字段",
                                 keys=sorted(keys)[:8], count=len(keys))
    op = parsed["op"]
    if not isinstance(op, str):
        raise WatchProtocolError("BW_WATCH_VOICE_OP_INVALID", "op 必须是字符串",
                                 got=type(op).__name__)
    try:
        return WatchCommand(op)
    except ValueError as error:
        raise WatchProtocolError("BW_WATCH_VOICE_OP_UNKNOWN",
                                 "不支持的 op（只有 start / stop / ping）",
                                 op=op[:48]) from error


# ══════════════════════════════════════════════════════════════════════════════
# ⚠ 铁律二落地点（二）：发往 Windows 的消息
#
# 四个字面量构造器，入参全是标量。没有任何一个接受 dict / JSON / 手表来的对象。
# ══════════════════════════════════════════════════════════════════════════════
WINDOWS_MESSAGE_KEYS = {
    "hello": {"contract", "type", "requestId", "protocolVersion"},
    # ⚠ appKind / takeover **故意不发**：appKind 默认就是 codex-desktop（发了反而要进
    #   动态键集合校验），takeover 会抢占 iPad 正在进行的通话 —— 那是产品决定，没拍板
    #   之前让 BW_COMPUTER_VOICE_DIRECT_BUSY 冒到手表 UI 上。
    "start": {"contract", "type", "requestId", "sessionId"},
    "stop": {"contract", "type", "requestId", "sessionId"},
    "heartbeat": {"contract", "type", "requestId", "sessionId", "sequence"},
}


def _hello_message(request_id: str) -> dict:
    return {"contract": CONTRACT, "type": "hello",
            "requestId": request_id, "protocolVersion": PROTOCOL_VERSION}


def _start_message(request_id: str, session_id: str) -> dict:
    return {"contract": CONTRACT, "type": "start",
            "requestId": request_id, "sessionId": session_id}


def _stop_message(request_id: str, session_id: str) -> dict:
    return {"contract": CONTRACT, "type": "stop",
            "requestId": request_id, "sessionId": session_id}


def _heartbeat_message(request_id: str, session_id: str, sequence: int) -> dict:
    return {"contract": CONTRACT, "type": "heartbeat", "requestId": request_id,
            "sessionId": session_id, "sequence": sequence}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _new_request_id() -> str:
    """safe-id 字符集 `[A-Za-z0-9._:-]`，长度 1..160（DirectBridgeContract.IsSafeId）。"""
    return "request-" + _b64url(os.urandom(16)) + "-" + _b64url(os.urandom(9))


class WindowsBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.retryable = retryable


# ══════════════════════════════════════════════════════════════════════════════
# Windows 腿：一通电话 = 一条 WS 连接
# ══════════════════════════════════════════════════════════════════════════════
class _WindowsLeg:
    def __init__(self, session_id: str, on_downlink, on_failure) -> None:
        self.session_id = session_id
        self._on_downlink = on_downlink        # (payload: bytes) -> None
        self._on_failure = on_failure          # (code: str, message: str) -> None
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._guard = wire.DownlinkSequenceGuard()
        self._closed = False
        self._failed = False
        self.downlink_frames = 0
        self.downlink_bad = 0

    # ── 连接 / 拆除 ────────────────────────────────────────────────────────
    async def connect(self) -> None:
        _log("windows.connecting", endpoint=WINDOWS_ENDPOINT, origin=WINDOWS_ORIGIN,
             session=self.session_id)
        try:
            self._ws = await websockets.connect(
                WINDOWS_ENDPOINT,
                origin=WINDOWS_ORIGIN,
                max_size=MAX_MESSAGE_BYTES,
                # 应用层 5 s 心跳是远比 WS ping 更好的存活信号；服务端自己每 20 s ping
                # 我们，标准库自动回 pong。关掉客户端 ping，少一条能误杀通话的路。
                ping_interval=None,
                open_timeout=OPEN_TIMEOUT,
                # ⚠ Tailscale-User-Login **不由我们发**：那个头由 `tailscale serve` 注入，
                #   客户端伪造不了，这正是它能当闸的原因。自己发只会被判无效。
            )
        except Exception as error:                    # noqa: BLE001
            _log("windows.connect_failed", session=self.session_id,
                 detail=("%s: %s" % (type(error).__name__, error))[:200])
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_OFFLINE",
                                     "Windows 桥接器离线或 WSS 连接失败",
                                     retryable=True) from error
        self._reader = asyncio.create_task(self._read_loop(), name="windows-reader")
        _log("windows.connected", session=self.session_id)

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(WindowsBridgeError(
                    "BW_COMPUTER_VOICE_DIRECT_CANCELLED", "Windows 桥接连接已关闭"))
        self._pending.clear()

    # ── 收 ────────────────────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, (bytes, bytearray)):
                    self._handle_downlink(bytes(message))
                else:
                    self._handle_text(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as error:
            if not self._closed:
                closed_code, closed_reason = _close_info(error)
                _log("windows.closed", code=closed_code, reason=closed_reason[:120],
                     session=self.session_id)
                self._fail("BW_COMPUTER_VOICE_DIRECT_DISCONNECTED", "Windows 桥接器连接已断开")
        except Exception as error:                    # noqa: BLE001
            _log("windows.read_error", session=self.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            self._fail("BW_COMPUTER_VOICE_DIRECT_DISCONNECTED", "Windows 桥接读取失败")

    def _handle_downlink(self, data: bytes) -> None:
        # 下行走的是 Pi↔Windows 同一局域网那一跳，丢包不是常态，所以这一侧照 wire 的
        # 判断 fail-closed（`decode_downlink_frame` + `DownlinkSequenceGuard` 的注释）。
        # ⚠ 但这套语义**到此为止**，不往手表那一侧传：那边允许丢帧。
        try:
            frame = self._guard.check(
                wire.decode_downlink_frame(data, session_id=self.session_id))
        except wire.WireError as error:
            self.downlink_bad += 1
            _log("windows.downlink_invalid", session=self.session_id, code=error.code,
                 message=error.message[:120], bytes=len(data))
            self._fail(error.code, error.message)
            return
        except Exception as error:                    # noqa: BLE001
            self.downlink_bad += 1
            _log("windows.downlink_crashed", session=self.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            self._fail("BW_COMPUTER_VOICE_DIRECT_PCM_UNEXPECTED", "下行帧处理异常")
            return
        self.downlink_frames += 1
        self._on_downlink(frame.payload)

    def _handle_text(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except Exception as error:                    # noqa: BLE001
            _log("windows.text_unparsable", session=self.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            self._fail("BW_COMPUTER_VOICE_DIRECT_SCHEMA", "Windows 桥接器响应无法解析")
            return
        if not isinstance(message, dict):
            _log("windows.text_shape", got=type(message).__name__, session=self.session_id)
            self._fail("BW_COMPUTER_VOICE_DIRECT_SCHEMA", "Windows 桥接器响应必须是对象")
            return
        if message.get("contract") != CONTRACT:
            _log("windows.contract_mismatch", session=self.session_id,
                 got=str(message.get("contract"))[:64])
            self._fail("BW_COMPUTER_VOICE_DIRECT_CONTRACT", "Windows 桥接器合同版本不匹配")
            return
        kind = message.get("type")
        if kind == "event":
            self._handle_event(message)
            return
        if kind != "result":
            _log("windows.type_invalid", got=str(kind)[:32], session=self.session_id)
            self._fail("BW_COMPUTER_VOICE_DIRECT_SCHEMA", "Windows 桥接器消息类型无效")
            return
        request_id = message.get("requestId")
        if not isinstance(request_id, str) or request_id not in self._pending:
            # 迟到的回执。不致命，但也别静默丢。
            _log("windows.request_unknown", session=self.session_id,
                 request_id=str(request_id)[:80], pending=len(self._pending))
            return
        future = self._pending.pop(request_id)
        if future.done():
            _log("windows.request_late", session=self.session_id, request_id=request_id[:80])
            return
        if message.get("ok") is True:
            payload = message.get("payload")
            future.set_result(payload if isinstance(payload, dict) else {})
            return
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        code = str(error.get("code") or "BW_COMPUTER_VOICE_DIRECT_SCHEMA")
        detail = str(error.get("message") or "Windows 桥接器返回失败但没给原因")
        _log("windows.result_error", session=self.session_id, code=code,
             action=str(message.get("action"))[:32], message=detail[:160],
             retryable=bool(error.get("retryable")))
        future.set_exception(WindowsBridgeError(code, detail, bool(error.get("retryable"))))

    def _handle_event(self, message: dict) -> None:
        payload = message.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        state = str(payload.get("state") or "")
        reason = str(payload.get("reason") or "")
        # START 途中会先到 starting-app / waiting-app-ready / starting-capture 三条。
        _log("windows.event", session=self.session_id, kind=str(message.get("event"))[:32],
             state=state[:48], reason=reason[:96])
        if state == "error":
            # 服务端发完 status:error 就会关连接，这是通话的死亡通知。
            self._fail(reason or "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
                       "Windows 桥接器报告错误状态")

    def _fail(self, code: str, message: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        self._on_failure(code, message)

    # ── 发 ────────────────────────────────────────────────────────────────
    async def _send_json(self, message: dict, action: str) -> None:
        """**唯一**的文本发送口。断言消息形状 —— 对应 C# 侧的 RequireExactKeys。"""
        expected = WINDOWS_MESSAGE_KEYS.get(action)
        if expected is None or message.get("type") != action or set(message) != expected:
            # 走到这里说明代码被改坏了（不是运行时输入问题），当场炸掉比发出去强。
            raise AssertionError(
                "拒绝发送：action=%r 不在允许集合内，或键集合不匹配（got=%s expected=%s）"
                % (action, sorted(set(message)), sorted(expected or ())))
        if self._ws is None:
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
                                     "Windows 桥接请求发送失败")
        await self._ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _request(self, message: dict, action: str, timeout: float) -> dict:
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_CAPACITY",
                                     "Windows 桥接器连接不可用或待处理请求过多")
        request_id = message["requestId"]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_json(message, action)
        except AssertionError:
            self._pending.pop(request_id, None)
            raise
        except WindowsBridgeError:
            self._pending.pop(request_id, None)
            raise
        except Exception as error:                    # noqa: BLE001
            self._pending.pop(request_id, None)
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
                                     "Windows 桥接请求发送失败") from error
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            self._pending.pop(request_id, None)
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
                                     "Windows 桥接器请求超时", retryable=True) from error

    async def send_pcm(self, frame: bytes) -> None:
        if self._ws is None:
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
                                     "Reader 麦克风 PCM 发送失败")
        await self._ws.send(frame)

    # ── 握手三段 ──────────────────────────────────────────────────────────
    async def hello(self) -> None:
        payload = await self._request(_hello_message(_new_request_id()), "hello",
                                      REQUEST_TIMEOUT)
        if payload.get("protocolVersion") != PROTOCOL_VERSION:
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_CONTRACT",
                                     "Windows 桥接器直连协议版本不匹配")
        # ⚠ payload 内部**刻意不做 exact-keys 断言**：iOS 那份对 status 就漂移过一次
        #   （服务端多返回了一个键，客户端整条判 SCHEMA）。只校验真正依赖的量。
        limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
        frame_bytes = limits.get("pcmFrameBytes")
        if isinstance(frame_bytes, int) and frame_bytes != wire.PCM_FRAME_BYTES:
            raise WindowsBridgeError(
                "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
                "Windows 桥接器容量合同不匹配（pcmFrameBytes=%s）" % frame_bytes)
        _log("windows.hello_ok", session=self.session_id, frame_bytes=frame_bytes,
             limits_keys=sorted(limits)[:12])

    async def start(self) -> None:
        payload = await self._request(_start_message(_new_request_id(), self.session_id),
                                      "start", START_REQUEST_TIMEOUT)
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        if (payload.get("sessionId") != self.session_id
                or payload.get("state") != "active"
                or media.get("hostReady") is not True
                or media.get("captureActive") is not True):
            _log("windows.start_unconfirmed", session=self.session_id,
                 state=str(payload.get("state"))[:32], host_ready=media.get("hostReady"),
                 capture=media.get("captureActive"))
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_START",
                                     "Windows 桥接器未确认本次 START")
        _log("windows.start_ok", session=self.session_id)

    async def stop(self) -> None:
        payload = await self._request(_stop_message(_new_request_id(), self.session_id),
                                      "stop", REQUEST_TIMEOUT)
        if payload.get("sessionId") != self.session_id or payload.get("state") != "idle":
            _log("windows.stop_unconfirmed", session=self.session_id,
                 state=str(payload.get("state"))[:32])
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_STOP",
                                     "Windows 桥接器 STOP 回执无效")
        _log("windows.stop_ok", session=self.session_id)

    async def heartbeat(self, sequence: int) -> None:
        payload = await self._request(
            _heartbeat_message(_new_request_id(), self.session_id, sequence),
            "heartbeat", HEARTBEAT_REQUEST_TIMEOUT)
        if (payload.get("sessionId") != self.session_id
                or payload.get("sequence") != sequence
                or payload.get("state") != "active"):
            raise WindowsBridgeError("BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
                                     "Windows 桥接器 HEARTBEAT 回执无效")


# ══════════════════════════════════════════════════════════════════════════════
# 通话：50 Hz 节拍器 + 心跳
# ══════════════════════════════════════════════════════════════════════════════
class CallState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"


class _Call:
    """一通电话。持有唯一的 Windows 腿、上行节拍、心跳。"""

    def __init__(self, relay: "Relay") -> None:
        self._relay = relay
        self.session_id = wire.new_session_id()
        self.uplink = wire.WatchVoiceUplink(self.session_id)
        self.leg = _WindowsLeg(self.session_id,
                               on_downlink=relay.on_downlink_payload,
                               on_failure=self._on_leg_failure)
        self.started_at = time.time()
        self.stop_reason: Optional[tuple[str, str]] = None
        self._pacer: Optional[asyncio.Task] = None
        self._heart: Optional[asyncio.Task] = None
        self._slip_logged_at = 0.0
        # 计数器：只有「多少帧 / 丢了几帧」，**没有任何音频内容**
        self.frames_sent = 0
        self.pacer_slips = 0
        self.pacer_missed = 0
        self.watch_malformed = 0
        self.watch_foreign = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def open(self) -> None:
        """连接 → hello → START。**一通电话一条连接**，30 s START 期限因此踩不到。"""
        await self.leg.connect()
        opened_at = time.monotonic()
        await self.leg.hello()
        await self.leg.start()
        elapsed = time.monotonic() - opened_at
        if elapsed > START_DEADLINE_SECONDS:
            # 理论上不该发生（服务端自己会先超时）。真发生了要出声，别让下一次心跳去猜。
            _log("call.start_deadline_exceeded", session=self.session_id,
                 elapsed_ms=int(elapsed * 1000))
        self._pacer = asyncio.create_task(self._pace_loop(), name="uplink-pacer")
        self._heart = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

    async def close(self, code: str, message: str, *, graceful: bool) -> None:
        if self.stop_reason is None:
            self.stop_reason = (code, message)
        for task in (self._pacer, self._heart):
            if task is not None:
                task.cancel()
        for name, task in (("pacer", self._pacer), ("heart", self._heart)):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass                                  # 我们自己取消的,预期之内
            except Exception as error:                # noqa: BLE001
                # 以前这里跟 CancelledError 一起被 suppress 掉了：节拍器/心跳带着
                # 真异常死掉，唯一的痕迹是通话结束，查不出为什么。
                _log("call.task_failed", session=self.session_id, task=name,
                     detail="%s: %s" % (type(error).__name__, error))
        self._pacer = self._heart = None
        if graceful:
            try:
                await self.leg.stop()
            except Exception as error:                # noqa: BLE001
                _log("call.stop_failed", session=self.session_id, detail=str(error)[:160])
        await self.leg.close()
        _log("call.closed", session=self.session_id, code=code, message=message[:120],
             graceful=graceful, seconds=round(time.time() - self.started_at, 1),
             **self.counters())

    def counters(self) -> dict:
        merged = {
            "frames_sent": self.frames_sent,
            "pacer_slips": self.pacer_slips,
            "pacer_missed": self.pacer_missed,
            "watch_malformed": self.watch_malformed,
            "watch_foreign": self.watch_foreign,
            "downlink_frames": self.leg.downlink_frames,
            "downlink_bad": self.leg.downlink_bad,
        }
        try:
            merged.update(self.uplink.stats)          # from_watch / filled / starved / depth …
        except Exception as error:                    # noqa: BLE001
            merged["stats_unavailable"] = "%s: %s" % (type(error).__name__, error)
        return merged

    def _on_leg_failure(self, code: str, message: str) -> None:
        self._relay.schedule_call_failure(code, message)

    # ── 🎯 50 Hz 恒定节拍：整个进程的心脏 ──────────────────────────────────
    async def _pace_loop(self) -> None:
        """按**绝对时刻**排程调 `uplink.tick()`，一拍一帧。

        ⚠ 不要改成 `await asyncio.sleep(0.02)` 累加 —— 手表侧实测那样会漂到 1/3 速率。
        每一拍的目标时刻是 `origin + n*20ms`，睡到那个时刻为止。

        序号与时间戳由 wire 的 `UplinkSequencer` 产生，与手表到没到帧毫无关系，所以
        「严格 +1 / 严格递增」在任何丢包、任何断线下都恒成立。本循环唯一的职责是
        **把拍子打准**：多调一次 tick 是加速，少调一次是可闻的断续。
        """
        loop = asyncio.get_running_loop()
        origin = loop.time()
        tick = 0
        try:
            while True:
                target = origin + tick * FRAME_PERIOD_SECONDS
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    behind = -delay
                    if behind > PACER_CATCHUP_LIMIT:
                        # 追不上了。**出声**，然后把时间轴重锚到现在 —— 绝不默默变慢。
                        missed = int(behind // FRAME_PERIOD_SECONDS)
                        self.pacer_slips += 1
                        self.pacer_missed += missed
                        now = loop.time()
                        if now - self._slip_logged_at >= PACER_SLIP_LOG_INTERVAL:
                            self._slip_logged_at = now
                            _log("pacer.slip", session=self.session_id,
                                 behind_ms=int(behind * 1000), missed=missed,
                                 slips=self.pacer_slips)
                        origin += behind          # 重锚：节拍从此刻重新起算
                    else:
                        await asyncio.sleep(0)    # 小幅落后：不睡，但让出事件循环
                tick += 1

                try:
                    frame = self.uplink.tick()
                except Exception as error:            # noqa: BLE001
                    code = getattr(error, "code", "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME")
                    _log("pacer.tick_failed", session=self.session_id, code=code,
                         detail="%s: %s" % (type(error).__name__, error))
                    self._relay.schedule_call_failure(
                        code, getattr(error, "message", "上行取帧失败"))
                    return
                try:
                    await self.leg.send_pcm(frame)
                except asyncio.CancelledError:
                    raise
                except Exception as error:            # noqa: BLE001
                    _log("pacer.send_failed", session=self.session_id, tick=tick,
                         detail="%s: %s" % (type(error).__name__, error))
                    self._relay.schedule_call_failure(
                        "BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
                        "Reader 麦克风 PCM 发送失败")
                    return
                self.frames_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as error:                    # noqa: BLE001
            _log("pacer.crashed", session=self.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            self._relay.schedule_call_failure("BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME",
                                              "上行节拍器异常退出")

    # ── 心跳：单次超时不判死，连续两次才判 ────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        """5 s 一次。单次 7 s 超时**不**判死 —— 那比服务端 15 s 阈值还严，一次 RTT 抖动
        就会误杀整通电话（iOS 侧就是踩了这个才从 1 次改成 2 次）。第一次失败只留痕并
        告诉手表「正在重试」，**不能**走「通话结束」那一档。
        """
        sequence = 0
        strikes = 0
        loop = asyncio.get_running_loop()
        next_at = loop.time() + HEARTBEAT_INTERVAL_SECONDS
        try:
            while True:
                delay = next_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                sent_at = loop.time()
                # 必须从 1 开始且恰好 +1：服务端 `RenewHeartbeatAsync` 拿
                # `_heartbeatSequence + 1` 逐个比对，而它**只在接受时**才前进。
                # ⚠ 超时会烧掉一个序号（我们不知道对端收没收到）。这是刻意的：
                #   Pi↔Windows 是同一局域网的 TCP，「超时」几乎只可能是对端慢而不是
                #   消息没到 —— 那种情况下它稍后仍会处理，序号该前进。反过来若在
                #   超时后重用同一个序号，那个主要场景反而必然撞 SEQUENCE_INVALID。
                #   iOS 侧（`DirectVoiceSocket.swift:843`）是同一个取舍。
                sequence += 1
                try:
                    await self.leg.heartbeat(sequence)
                    if strikes:
                        _log("heartbeat.recovered", session=self.session_id,
                             seq=sequence, strikes=strikes)
                    strikes = 0
                except asyncio.CancelledError:
                    raise
                except WindowsBridgeError as error:
                    strikes += 1
                    _log("heartbeat.failed", session=self.session_id, seq=sequence,
                         strikes=strikes, code=error.code, message=error.message[:120])
                    if strikes >= HEARTBEAT_STRIKES:
                        self._relay.schedule_call_failure(
                            "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
                            "Windows 桥接器心跳连续失败")
                        return
                    # 这一档是「瞬时重试」，不是「通话结束」——手表 UI 不该因此挂断。
                    self._relay.notify_watch_warning(
                        "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_RETRY",
                        "Windows 桥接器心跳超时，正在重试")
                # 从**发出时刻**起算 5 s：回执快就是精确 5 s 节奏；超时了就立刻补发，
                # 免得下一次落到服务端 15 s 判死线之后。
                next_at = max(sent_at + HEARTBEAT_INTERVAL_SECONDS, loop.time())
        except asyncio.CancelledError:
            raise
        except Exception as error:                    # noqa: BLE001
            _log("heartbeat.crashed", session=self.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            self._relay.schedule_call_failure("BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
                                              "心跳循环异常退出")


# ══════════════════════════════════════════════════════════════════════════════
# 手表腿：一条 WS 连接 = 一支手表
# ══════════════════════════════════════════════════════════════════════════════
class _WatchLeg:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.id = _b64url(os.urandom(6))
        # 每条连接铸一个新 stream_id：抖动缓冲用它把上一条连接的残帧判成 FOREIGN，
        # 而不是让旧音频混进新会话。手表必须把它盖进自己每一帧的 session 字段。
        self.stream_id = wire.new_session_id()
        self.attached_at = time.time()
        self.bad_messages = 0
        self.downlink_sent = 0
        self.downlink_dropped = 0
        self.downlink_refused = 0
        self.enqueue_closed = 0
        self._out: asyncio.Queue = asyncio.Queue(maxsize=WATCH_DOWNLINK_QUEUE)
        self._sender: Optional[asyncio.Task] = None
        self._closed = False

    async def start_sender(self) -> None:
        self._sender = asyncio.create_task(self._send_loop(), name="watch-sender")

    async def flush(self, timeout: float = 0.5) -> int:
        """关连接前把已排队的说明尽量送出去，返回没送出去的条数。

        ⚠ 这不是可有可无的礼貌：`stop_sender()` 直接取消发送任务，队列里没发出去的
        东西**全部丢弃** —— 包括「你为什么被断开」那一条。手表上没有控制台，最后那条
        说明要是也丢了，用户看到的就只是一次没有理由的断线，而服务端日志他看不到。
        （`silent-failure-lessons.md`：无控制台设备上沉默等于不可诊断。）
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while not self._out.empty() and loop.time() < deadline:
            if self._sender is None or self._sender.done():
                break                                 # 发送任务已经死了，等下去没有意义
            await asyncio.sleep(0.01)
        left = self._out.qsize()
        if left:
            _log("watch.flush_incomplete", watch=self.id, left=left, timeout=timeout)
        return left

    async def stop_sender(self) -> None:
        self._closed = True
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sender
            self._sender = None

    async def _send_loop(self) -> None:
        while True:
            payload = await self._out.get()
            try:
                await self.connection.send(payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:                # noqa: BLE001
                _log("watch.send_failed", watch=self.id,
                     detail="%s: %s" % (type(error).__name__, error))
                return

    def enqueue(self, payload) -> None:
        """下行入队。**满了丢最老的** —— 手表是容错的一侧，宁可丢音频，也不许堵住
        Windows 那条读循环（堵住会连带饿死心跳，把一次手表卡顿升级成掉线）。

        ⚠ 三条提前退出各有各的计数。以前 `put_nowait` 的 QueueFull 是被 suppress 掉
        的裸路径：说明性的 `ev:error` 在这里蒸发，连一个计数都没有，而手表上没有
        控制台 —— 用户看到的就是一次没有理由的断线。
        """
        if self._closed:
            self.enqueue_closed += 1
            return
        if self._out.full():
            try:
                self._out.get_nowait()
                self.downlink_dropped += 1
            except asyncio.QueueEmpty:               # 竞态：刚被发送任务取走了
                pass
        try:
            self._out.put_nowait(payload)
        except asyncio.QueueFull:
            self.downlink_refused += 1
            if self.downlink_refused % 50 == 1:      # 限流，计数不限流
                _log("watch.enqueue_refused", watch=self.id,
                     refused=self.downlink_refused, depth=self._out.qsize(),
                     kind=("pcm" if isinstance(payload, (bytes, bytearray))
                           else "json"))
            return
        if isinstance(payload, (bytes, bytearray)):
            self.downlink_sent += 1

    def send_json(self, **fields: Any) -> None:
        self.enqueue(json.dumps(fields, ensure_ascii=False, separators=(",", ":")))


# ══════════════════════════════════════════════════════════════════════════════
# 中继：唯一的协调器
# ══════════════════════════════════════════════════════════════════════════════
class Relay:
    def __init__(self, token: str, kill_file: Path, *,
                 grace_seconds: float = WATCH_GRACE_SECONDS) -> None:
        self._token = token
        self._token_fingerprint = _fingerprint(token)
        self.kill_file = kill_file
        self.grace_seconds = grace_seconds
        self.state = CallState.IDLE
        self._call: Optional[_Call] = None
        self._watch: Optional[_WatchLeg] = None       # ⚠ 单并发：同时只允许一支手表
        self._lock = asyncio.Lock()
        self._grace: Optional[asyncio.Task] = None
        # ⚠ 强引用：`asyncio.create_task` 的返回值不留着，任务可能在跑到一半时被
        #   GC 掉（CPython 官方就是这么警告的）。这里丢掉的是「拆掉一通电话」，
        #   丢了就是一支活麦永远留在用户 PC 上。
        self._background: set = set()
        # 手表不在时被丢掉的下行帧。丢是对的（容错的一侧随便断），沉默不是。
        self.downlink_no_watch = 0
        self._no_watch_logged_at = 0.0
        self.killed = False
        # 卡片桥。⚠ 跑在**独立线程**里,不进这个事件循环 —— 它要发同步 HTTP,
        # 一次慢请求进了这里就会让 50Hz 的音频节拍卡一下。
        self._card_bridge = None
        self._card_thread = None

    # ── 卡片桥（手表单独在线时,让 AI 的卡片有地方可去）──────────────────
    def start_card_bridge(self, leg) -> None:
        """手表接上了 → 向 Windows 注册成一个 Reader 来源。

        ⚠ 失败**不影响通话**:卡片是附加能力,音频才是主线。所以这里的任何
        错误都只出声、不上抛。
        """
        self.stop_card_bridge()
        try:
            import threading

            import watch_card_bridge
            bridge = watch_card_bridge.WatchCardBridge(
                # 复用手表这一路的 streamId 当来源标识:它本来就是**每条连接
                # 一个新的**,天然满足"来源随连接生灭"。
                leg.stream_id,
                deliver=lambda payload: leg.send_json(**payload),
                log=lambda ev, **kw: _log(ev, watch=leg.id, **kw))
            thread = threading.Thread(
                target=bridge.run, name="watch-card-bridge", daemon=True)
            thread.start()
            self._card_bridge = bridge
            self._card_thread = thread
        except Exception as error:                    # noqa: BLE001
            _log("card.bridge_failed_to_start",
                 detail="%s: %s" % (type(error).__name__, error))

    def stop_card_bridge(self) -> None:
        bridge = self._card_bridge
        if bridge is not None:
            bridge.stop()
        self._card_bridge = None
        self._card_thread = None

    # ── 鉴权 ──────────────────────────────────────────────────────────────
    def authorize(self, headers, query_token: Optional[str]) -> Optional[str]:
        """通过返回 None，失败返回**拒绝理由的错误码**。理由分档，不折成一个布尔。"""
        try:
            raw = headers.get("Authorization")
        except Exception:                             # noqa: BLE001
            raw = None
        if isinstance(raw, str) and raw:
            parts = raw.split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                _log("auth.scheme_invalid", parts=len(parts),
                     scheme=(parts[0][:16] if parts else ""))
                return "BW_WATCH_VOICE_AUTH_SCHEME"
            supplied, source = parts[1].strip(), "header"
        elif query_token:
            supplied, source = query_token, "query"
        else:
            _log("auth.missing", has_header=bool(raw), has_query=bool(query_token))
            return "BW_WATCH_VOICE_AUTH_MISSING"
        if not hmac.compare_digest(supplied, self._token):
            # 只记长度与指纹，**永不记 token 本身**。
            _log("auth.mismatch", source=source, supplied_length=len(supplied),
                 supplied_fingerprint=_fingerprint(supplied),
                 expected_fingerprint=self._token_fingerprint)
            return "BW_WATCH_VOICE_AUTH_INVALID"
        _log("auth.ok", source=source)
        return None

    # ── 应急闸 ────────────────────────────────────────────────────────────
    def kill_file_present(self) -> bool:
        try:
            return self.kill_file.exists()
        except Exception as error:                    # noqa: BLE001
            # 查不了就当拉闸了：应急闸 fail-closed，宁可不通，也不给一支状态不明的活麦。
            _log("kill.stat_failed", path=str(self.kill_file),
                 detail="%s: %s" % (type(error).__name__, error))
            return True

    async def watchdog_loop(self) -> None:
        """1 s 一次盯应急闸；顺带每 10 s 打一条计数快照并回给手表。

        ⚠ 循环体整个包在 try 里，而且**吞掉异常继续转**。这跟本文件别处「出了错就
        出声并退出」相反，是故意的：这个循环是应急闸唯一的执行者。它一旦因为某次取数
        抛异常而结束，任务的异常还没人取（`main_async` 只在退出时 cancel 它并
        suppress），表现就是拉下 kill 文件之后**什么都不发生** —— 而那正是拉闸的人
        最需要它起作用的时刻。所以这里出声但不退出。
        """
        last_stats = 0.0
        while True:
            await asyncio.sleep(KILL_POLL_SECONDS)
            try:
                last_stats = await self._watchdog_pass(last_stats)
            except asyncio.CancelledError:
                raise
            except Exception as error:                # noqa: BLE001
                _log("watchdog.pass_failed", state=self.state.value,
                     detail="%s: %s" % (type(error).__name__, error))

    async def _watchdog_pass(self, last_stats: float) -> float:
        present = self.kill_file_present()
        if present and not self.killed:
            self.killed = True
            _log("kill.engaged", path=str(self.kill_file), state=self.state.value)
            await self.tear_down("BW_WATCH_VOICE_KILLED",
                                 "应急闸已拉下（kill 文件存在）", graceful=True)
            watch = self._watch
            if watch is not None:
                await self._close_watch(watch, CLOSE_KILLED, "BW_WATCH_VOICE_KILLED")
        elif not present and self.killed:
            self.killed = False
            _log("kill.released", path=str(self.kill_file))
        now = time.time()
        call = self._call
        if call is not None and now - last_stats >= STATS_INTERVAL_SECONDS:
            last_stats = now
            counters = call.counters()
            counters["downlink_no_watch"] = self.downlink_no_watch
            _log("call.tick", state=self.state.value, session=call.session_id,
                 watch_attached=self._watch is not None, **counters)
            watch = self._watch
            if watch is not None:
                watch.send_json(ev="stats", state=self.state.value, **counters)
        return last_stats

    # ── 手表接入 / 顶替 ───────────────────────────────────────────────────
    async def attach_watch(self, leg: _WatchLeg) -> None:
        """**单并发，明确顶替。**

        选顶替而不是拒绝，理由是链路现实：手表射频实测约 5 秒自愈，而那条自愈**就是**
        一条新连接。若拒绝新连接，旧连接的 TCP 半开会把手表挡在门外直到超时 —— 等于
        亲手废掉这个设计最重要的性质。所以新连接顶替旧的，旧的收 4409 明确关闭。

        ⚠ 顶替**不动通话**：Windows 那头继续收 50 Hz 静音，通话在它眼里从未中断。
        安全上也不多开口子 —— 顶替者本来就得先过同一把 token。
        """
        previous = self._watch
        self._watch = leg
        if previous is not None:
            _log("watch.displaced", old=previous.id, new=leg.id,
                 held_seconds=round(time.time() - previous.attached_at, 1))
            await self._close_watch(previous, CLOSE_DISPLACED, "BW_WATCH_VOICE_DISPLACED")
        call = self._call
        if call is not None:
            # 新连接 = 新序号空间。清掉旧缓冲，旧连接的残帧此后判 FOREIGN。
            call.uplink.rebind_watch(leg.stream_id)
            _log("watch.rebound", watch=leg.id, session=call.session_id)
        self._cancel_grace("手表回来了", watch=leg.id, state=self.state.value)

    async def detach_watch(self, leg: _WatchLeg) -> None:
        if self._watch is not leg:
            # 已被顶替，顶替时就处理过了。出声，否则「顶替」和「detach 走错了腿」
            # 这两件事在日志里一样都是什么都没有。
            _log("watch.detach_stale", watch=leg.id,
                 current=self._watch.id if self._watch else None)
            return
        self._watch = None
        self.stop_card_bridge()
        _log("watch.detached", watch=leg.id, state=self.state.value,
             held_seconds=round(time.time() - leg.attached_at, 1),
             downlink_sent=leg.downlink_sent, downlink_dropped=leg.downlink_dropped)
        call = self._call
        if call is not None:
            # 绑到一个没有任何手表持有的 id：既清空了缓冲（手表走了，它的音频已经陈旧），
            # 又让任何迟到的残帧判 FOREIGN，而不是被当成新会话的音频播出去。
            call.uplink.rebind_watch(wire.new_session_id())
        if self.state in (CallState.STARTING, CallState.ACTIVE):
            # 掉线不挂电话 —— 这正是减震器。但也不能把一支活麦无限期留在用户 PC 上。
            self._grace = asyncio.create_task(self._grace_timer(), name="watch-grace")
            _log("watch.grace_started", seconds=self.grace_seconds)

    def _cancel_grace(self, why: str, **fields: Any) -> None:
        """撤掉宽限计时器。

        ⚠ **绝不能 cancel 正在跑的自己。** 宽限到点后是 `_grace_timer` 自己去调
        `tear_down` 的，而 tear_down 也要撤计时器 —— 若不认出「这就是我」，`cancel()`
        会把 CancelledError 投进当前调用栈，恰好落在紧接着的 `call.close()` 的第一个
        await 上，把 graceful STOP 拦腰截断。表现：电话在 Pi 这边算结束了，Windows
        那头的麦克风还开着，而日志里一个字都没有（`call.stop_ok` 不会出现，但也没有
        任何一行说它为什么没出现）。
        """
        task = self._grace
        if task is None:
            return
        self._grace = None
        if task is asyncio.current_task():
            _log("watch.grace_self_finished", why=why, **fields)
            return
        task.cancel()
        _log("watch.grace_cancelled", why=why, **fields)

    async def _grace_timer(self) -> None:
        try:
            await asyncio.sleep(self.grace_seconds)
        except asyncio.CancelledError:
            return
        _log("watch.grace_expired", seconds=self.grace_seconds)
        await self.tear_down("BW_WATCH_VOICE_WATCH_GONE",
                             "手表断开超过宽限期，自动结束通话", graceful=True)

    async def _close_watch(self, leg: _WatchLeg, code: int, reason: str) -> None:
        await leg.flush()          # 先把「为什么关你」送出去，再关
        await leg.stop_sender()
        with contextlib.suppress(Exception):
            # reason 只用 ASCII 错误码：WS 关闭原因上限 123 字节，中文很容易超。
            await leg.connection.close(code, reason)

    # ── 手表命令 → 自己拼的 Windows 消息 ──────────────────────────────────
    async def handle_command(self, leg: _WatchLeg, command: WatchCommand) -> None:
        """⚠ 注意入参：`command` 是**枚举**。手表的原始 JSON 到不了这里，更到不了
        Windows 腿 —— 下面每一条发出去的消息都是本文件用字面量拼的。"""
        if command is WatchCommand.PING:
            leg.send_json(ev="pong", atMs=int(time.time() * 1000), state=self.state.value)
            return
        if command is WatchCommand.START:
            await self._command_start(leg)
            return
        if command is WatchCommand.STOP:
            await self._command_stop(leg)
            return
        # 枚举穷尽；走到这里说明有人加了成员却没接线。出声，别静默。
        _log("command.unhandled", command=command.name, watch=leg.id)
        leg.send_json(ev="error", code="BW_WATCH_VOICE_OP_UNWIRED", message="该指令尚未接线")

    async def _release_call(self, call: "_Call") -> bool:
        """把一通失败的电话从状态机里摘掉。**只摘自己那通。**

        无条件写 `self._call = None` + `state = IDLE` 看着无害，但 open() 是在锁外跑
        的：这段时间里通话可能已经被拆掉、手表又发起了新的一通。那时这两行抹掉的是
        **别人**，而被抹掉的那通电话对象还活着、节拍器还在推帧 —— 又一支关不掉的活麦。
        """
        async with self._lock:
            if self._call is not call:
                _log("call.release_stale", session=call.session_id,
                     current=self._call.session_id if self._call else None,
                     state=self.state.value)
                return False
            self._call = None
            self.state = CallState.IDLE
        return True

    async def _command_start(self, leg: _WatchLeg) -> None:
        if self.killed or self.kill_file_present():
            _log("command.start_refused_killed", watch=leg.id)
            leg.send_json(ev="error", code="BW_WATCH_VOICE_KILLED",
                          message="应急闸已拉下，拒绝开始新通话")
            return
        async with self._lock:
            if self.state is CallState.ACTIVE:
                _log("command.start_idempotent", watch=leg.id,
                     session=self._call.session_id if self._call else None)
                self._broadcast_state()
                return
            if self.state in (CallState.STARTING, CallState.STOPPING):
                _log("command.start_busy", watch=leg.id, state=self.state.value)
                leg.send_json(ev="error", code="BW_WATCH_VOICE_TRANSITIONING",
                              message="电脑语音连接正在转换状态")
                return
            self.state = CallState.STARTING
            self._broadcast_state()
            try:
                call = _Call(self)
            except Exception as error:                # noqa: BLE001
                # 连对象都没建起来。若不在这里收回状态，STARTING 会永久卡住，之后每次
                # start 都被自己的 "busy" 挡掉，而没有任何地方会说为什么。
                self.state = CallState.IDLE
                _log("call.construct_failed", watch=leg.id,
                     detail="%s: %s" % (type(error).__name__, error))
                leg.send_json(ev="error", code="BW_WATCH_VOICE_INTERNAL",
                              message="通话对象创建失败，已回到空闲")
                self._broadcast_state(code="BW_WATCH_VOICE_INTERNAL",
                                      message="通话对象创建失败")
                return
            self._call = call
            call.uplink.rebind_watch(leg.stream_id)   # 这一通电话认这支手表的序号空间
            _log("call.opening", watch=leg.id, session=call.session_id,
                 stream=leg.stream_id)
        try:
            await call.open()
        except WindowsBridgeError as error:
            _log("call.open_failed", session=call.session_id, code=error.code,
                 message=error.message[:160], retryable=error.retryable)
            await call.leg.close()
            if not await self._release_call(call):
                return                                # 已经不是我们这通了，别动状态机
            self._broadcast_state(code=error.code, message=error.message)
            return
        except Exception as error:                    # noqa: BLE001
            _log("call.open_crashed", session=call.session_id,
                 detail="%s: %s" % (type(error).__name__, error))
            await call.leg.close()
            if not await self._release_call(call):
                return
            # ⚠ START 结果未知时**不许再发 STOP**，只能关连接（iOS 同规则）：服务端可能
            #   已经接管了音频路由，一条形状不明的 STOP 只会把状态搅得更糊。也不自动重试。
            self._broadcast_state(code="BW_COMPUTER_VOICE_DIRECT_START_UNKNOWN",
                                  message="Windows 启动结果未知；连接已关闭，不会自动重试")
            return
        async with self._lock:
            superseded = self._call is not call
            if not superseded:
                self.state = CallState.ACTIVE
        if superseded:
            # 我们在 open() 期间被拆掉了（STOP / 应急闸 / 宽限到期都会）。**必须自己
            # 收尸**：这通电话的节拍器和心跳是 open() 刚建起来的，tear_down 跑的那会儿
            # 它们还不存在，没有任何人取消得了。不收的后果不是状态错乱那么轻 ——
            # 是一支 STOP 也关不掉的活麦留在用户 PC 上（此后 tear_down 看到
            # self._call is None 会直接返回，schedule_call_failure 也只打一行
            # "call.failure_after_close" 就完）。
            _log("call.superseded_during_open", session=call.session_id,
                 state=self.state.value)
            await call.close("BW_WATCH_VOICE_SUPERSEDED",
                             "通话在启动过程中已被结束", graceful=True)
            self._broadcast_state()
            return
        _log("call.active", session=call.session_id)
        self._broadcast_state()

    async def _command_stop(self, leg: _WatchLeg) -> None:
        if self.state is CallState.IDLE:
            _log("command.stop_noop", watch=leg.id)
            self._broadcast_state()
            return
        _log("command.stop", watch=leg.id, state=self.state.value)
        await self.tear_down("BW_WATCH_VOICE_STOPPED", "手表结束通话", graceful=True)

    # ── 通话拆除 ──────────────────────────────────────────────────────────
    async def tear_down(self, code: str, message: str, *, graceful: bool) -> None:
        async with self._lock:
            call = self._call
            if call is None or self.state is CallState.STOPPING:
                # 拆除已在进行、或本来就没有通话。不做事是对的，但要说出来：
                # 应急闸拉下时走到这一支，日志里没有一行的话会被读成「闸没生效」。
                _log("call.teardown_noop", code=code, state=self.state.value,
                     has_call=call is not None)
                if call is None and self.state is not CallState.IDLE:
                    self.state = CallState.IDLE
                return
            self.state = CallState.STOPPING
            self._call = None
        self._cancel_grace("通话正在拆除", session=call.session_id)
        await call.close(code, message, graceful=graceful)
        async with self._lock:
            self.state = CallState.IDLE
        self._broadcast_state(code=code, message=message)

    def _spawn(self, coro, name: str) -> None:
        """开一个后台任务并**留住引用**，完成时把异常取出来。

        直接 `asyncio.create_task(...)` 丢掉返回值有两个坑，而且都是无声的：任务可能
        被 GC 掉；任务里抛的异常没人取，只会在解释器退出时打一行没有上下文的告警。
        """
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._background.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                _log("task.crashed", task=name,
                     detail="%s: %s" % (type(error).__name__, error))

        task.add_done_callback(_done)

    def schedule_call_failure(self, code: str, message: str) -> None:
        """从读循环 / 节拍器 / 心跳里调用（同步上下文）。"""
        if self._call is None:
            _log("call.failure_after_close", code=code, message=message[:120])
            return
        _log("call.failing", code=code, message=message[:160], state=self.state.value)
        self._spawn(self.tear_down(code, message, graceful=False), "call-teardown")

    def notify_watch_warning(self, code: str, message: str) -> None:
        watch = self._watch
        if watch is not None:
            watch.send_json(ev="warning", code=code, message=message)

    # ── 音频两个方向 ──────────────────────────────────────────────────────
    def on_downlink_payload(self, payload: bytes) -> None:
        """Windows → 手表。手表不在就直接丢：容错的一侧随便断。

        ⚠ 丢是对的，沉默不是。一次 5 秒空档就是 250 帧无声消失；不上计数的话，
        「手表没声音」和「Windows 根本没在发」在日志里长得一模一样。
        """
        watch = self._watch
        if watch is None:
            self.downlink_no_watch += 1
            now = time.time()
            if now - self._no_watch_logged_at >= STATS_INTERVAL_SECONDS:
                self._no_watch_logged_at = now
                _log("watch.downlink_discarded", dropped=self.downlink_no_watch,
                     state=self.state.value)
            return
        watch.enqueue(payload)

    def on_watch_frame(self, leg: _WatchLeg, data: bytes) -> None:
        """手表 → 抖动缓冲。

        ⚠ 这里收下的字节唯一的去处是 `uplink.on_watch_frame()`，由 wire 丢掉它的
        seq/ts/session、只留 1920 字节载荷，再由 Pi 自己的序号重新编码。手表的字节
        在结构上不可能构成一条发往 Windows 的消息。
        """
        call = self._call
        if call is None:
            # 没在通话里收到音频：出声（别静默丢），但不算错 —— 多半是 STOP 的竞态。
            _log("watch.frame_no_call", watch=leg.id, bytes=len(data))
            return
        try:
            outcome = call.uplink.on_watch_frame(data)
        except Exception as error:                    # noqa: BLE001
            call.watch_malformed += 1
            _log("watch.frame_crashed", watch=leg.id,
                 detail="%s: %s" % (type(error).__name__, error))
            return
        # 容错**不等于**沉默：内容问题不抛异常，但每一类都要能上计数、能查。
        # ⚠ 用 wire 的常量而不是字面量 "malformed" / "foreign"：字面量在 wire 改名
        #   之后会**永远不再匹配**，表现是这两个计数恒为 0 —— 一个看上去很健康的
        #   仪表盘，正好盖住"手表帧全被判废"这种最需要看见的状况。
        status = getattr(outcome, "status", "")
        if status == wire.MALFORMED:
            call.watch_malformed += 1
        elif status == wire.FOREIGN:
            call.watch_foreign += 1
        else:
            return
        if (call.watch_malformed + call.watch_foreign) % 25 == 1:   # 限流，计数不限流
            _log("watch.frame_rejected", watch=leg.id, status=status,
                 reason=str(getattr(outcome, "reason", ""))[:80],
                 malformed=call.watch_malformed, foreign=call.watch_foreign)

    def _broadcast_state(self, code: Optional[str] = None,
                         message: Optional[str] = None) -> None:
        watch = self._watch
        if watch is None:
            return
        fields: dict = {"ev": "state", "state": self.state.value}
        if code:
            fields["code"] = code
        if message:
            fields["message"] = message
        watch.send_json(**fields)

    def snapshot(self) -> dict:
        return {"state": self.state.value,
                "session": self._call.session_id if self._call else None,
                "watch": self._watch.id if self._watch else None,
                "killed": self.killed}


# ══════════════════════════════════════════════════════════════════════════════
# 手表腿 WS 服务端
# ══════════════════════════════════════════════════════════════════════════════
def _query_token(path: str) -> Optional[str]:
    import urllib.parse                               # noqa: PLC0415
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    except Exception as error:                        # noqa: BLE001
        _log("watch.query_unparsable", detail="%s: %s" % (type(error).__name__, error))
        return None
    values = query.get("token") or []
    return values[0] if values else None


def make_watch_handler(relay: Relay):
    async def handle(connection) -> None:
        # ⚠ 外层这个 try 不是装饰：handler 由 websockets 当**独立 task** 跑，这里抛出
        #   的任何异常（包括写错代码抛的 TypeError）都不会传到任何人面前，表现就是
        #   「手表连上了，然后什么都不发生」。`silent-failure-lessons.md` 规则一。
        try:
            await _handle(connection)
        except asyncio.CancelledError:
            raise
        except Exception as error:                    # noqa: BLE001
            _log("watch.handler_crashed",
                 detail="%s: %s" % (type(error).__name__, error))
            with contextlib.suppress(Exception):
                await connection.close(CLOSE_PROTOCOL, "BW_WATCH_VOICE_INTERNAL")

    async def _handle(connection) -> None:
        peer = str(getattr(connection, "remote_address", None))
        try:
            request = connection.request
            path = str(getattr(request, "path", ""))
            headers = getattr(request, "headers", {})
        except Exception as error:                    # noqa: BLE001
            _log("watch.request_unreadable", peer=peer,
                 detail="%s: %s" % (type(error).__name__, error))
            with contextlib.suppress(Exception):
                await connection.close(CLOSE_PROTOCOL, "BW_WATCH_VOICE_REQUEST_UNREADABLE")
            return

        if relay.kill_file_present():
            _log("watch.refused_killed", peer=peer, path=path.split("?", 1)[0][:80])
            with contextlib.suppress(Exception):
                await connection.send(json.dumps(
                    {"ev": "error", "code": "BW_WATCH_VOICE_KILLED",
                     "message": "应急闸已拉下，本机暂不接受手表语音会话"}, ensure_ascii=False))
                await connection.close(CLOSE_KILLED, "BW_WATCH_VOICE_KILLED")
            return

        denial = relay.authorize(headers, _query_token(path))
        if denial is not None:
            _log("watch.unauthorized", peer=peer, code=denial)
            with contextlib.suppress(Exception):
                await connection.send(json.dumps(
                    {"ev": "error", "code": denial, "message": "鉴权失败"},
                    ensure_ascii=False))
                await connection.close(CLOSE_UNAUTHORIZED, denial)
            return

        leg = _WatchLeg(connection)
        await leg.start_sender()
        await relay.attach_watch(leg)
        snapshot = relay.snapshot()
        _log("watch.attached", watch=leg.id, peer=peer, stream=leg.stream_id,
             state=snapshot["state"], session=snapshot["session"],
             killed=snapshot["killed"])
        # ⚠ 卡片桥跟**这一路手表连接**同生共死:手表来了才向 Windows 注册成
        # 一个 Reader 来源,走了就撤。不这样的话 AI 会把卡片投给一个根本没人
        # 看的地方,**而它还以为送到了**。
        relay.start_card_bridge(leg)
        # streamId 必须先于任何音频到达手表：它要盖进自己每一帧的 session 字段，
        # 用旧的会被抖动缓冲判 FOREIGN 全部丢掉（且那时只有服务端日志看得见）。
        leg.send_json(ev="hello", protocol=WATCH_PROTOCOL, streamId=leg.stream_id,
                      frameBytes=wire.PCM_FRAME_BYTES,
                      payloadBytes=wire.PCM_PAYLOAD_BYTES,
                      sampleRate=wire.SAMPLE_RATE_HZ,
                      paceHz=round(1 / FRAME_PERIOD_SECONDS),
                      ops=[member.value for member in WatchCommand])
        leg.send_json(ev="state", state=relay.state.value)

        try:
            async for message in connection:
                if isinstance(message, (bytes, bytearray)):
                    relay.on_watch_frame(leg, bytes(message))
                    continue
                try:
                    command = _parse_watch_command(message)
                except WatchProtocolError as error:
                    leg.bad_messages += 1
                    _log("watch.protocol_error", watch=leg.id, code=error.code,
                         bad=leg.bad_messages, **error.detail)
                    leg.send_json(ev="error", code=error.code, message=error.message)
                    if leg.bad_messages >= WATCH_BAD_MESSAGE_LIMIT:
                        _log("watch.protocol_giving_up", watch=leg.id, bad=leg.bad_messages)
                        await leg.flush()
                        with contextlib.suppress(Exception):
                            await connection.close(CLOSE_PROTOCOL, "BW_WATCH_VOICE_PROTOCOL")
                        break
                    continue
                _log("watch.command", watch=leg.id, op=command.value, state=relay.state.value)
                await relay.handle_command(leg, command)
        except ConnectionClosed as error:
            closed_code, closed_reason = _close_info(error)
            _log("watch.closed", watch=leg.id, code=closed_code,
                 reason=closed_reason[:120])
        except Exception as error:                    # noqa: BLE001
            _log("watch.loop_error", watch=leg.id,
                 detail="%s: %s" % (type(error).__name__, error))
        finally:
            await leg.flush(0.2)   # 最后那条 state / error 值得多等 200 ms
            await leg.stop_sender()
            await relay.detach_watch(leg)

    return handle


# ══════════════════════════════════════════════════════════════════════════════
# 启动自检 / 配置
# ══════════════════════════════════════════════════════════════════════════════
def _verify_isolation() -> None:
    """铁律二的机器校验。**任何一条不过就拒绝启动。**

    这不是装饰：日后谁把手表命令解析器放宽（哪怕只是「加个 requestId 方便排查」），
    进程当场起不来，而不是等到某天有人从手表那头发出一条 anki 删卡。
    """
    members = {member.value for member in WatchCommand}
    if members != {"start", "stop", "ping"}:
        raise RelayStartupError(
            "WatchCommand 枚举被改动：%s（只允许 start/stop/ping）" % sorted(members))

    def must_reject(raw: str, why: str) -> None:
        try:
            _parse_watch_command(raw)
        except WatchProtocolError:
            return
        raise RelayStartupError("铁律二自检失败：%s —— 解析器接受了 %s" % (why, raw[:80]))

    must_reject('{"op":"start","extra":1}', "控制帧多出了自由字段")
    must_reject('{"op":"start","payload":{"a":1}}', "控制帧夹带了嵌套载荷")
    must_reject('{"op":"start","requestId":"r"}', "控制帧多出了 requestId")
    must_reject('{"op":"anki-card-operation-local"}', "危险 op 未被枚举挡下")
    must_reject('{"op":"codex-voice-set"}', "危险 op 未被枚举挡下")
    must_reject('{"op":"context-mode-set"}', "危险 op 未被枚举挡下")
    must_reject('{"op":"service-mode-set"}', "危险 op 未被枚举挡下")
    must_reject('["start"]', "控制帧不是对象也被接受")
    must_reject('{}', "空对象被接受")
    for value, expected in (("start", WatchCommand.START), ("stop", WatchCommand.STOP),
                            ("ping", WatchCommand.PING)):
        if _parse_watch_command('{"op":"%s"}' % value) is not expected:
            raise RelayStartupError("铁律二自检失败：合法 %s 未被解析成枚举" % value)

    # 发往 Windows 的四种消息：键集合必须与 C# 侧 RequireExactKeys 一致。
    probe_session = "session-" + _b64url(os.urandom(16))
    probe_request = _new_request_id()
    shapes = {
        "hello": _hello_message(probe_request),
        "start": _start_message(probe_request, probe_session),
        "stop": _stop_message(probe_request, probe_session),
        "heartbeat": _heartbeat_message(probe_request, probe_session, 1),
    }
    if set(shapes) != set(WINDOWS_MESSAGE_KEYS):
        raise RelayStartupError("Windows 消息种类与键表不一致：%s vs %s"
                                % (sorted(shapes), sorted(WINDOWS_MESSAGE_KEYS)))
    for action, message in shapes.items():
        if set(message) != WINDOWS_MESSAGE_KEYS[action] or message["type"] != action:
            raise RelayStartupError(
                "Windows 消息形状自检失败：%s got=%s expected=%s"
                % (action, sorted(set(message)), sorted(WINDOWS_MESSAGE_KEYS[action])))
        if message["contract"] != CONTRACT:
            raise RelayStartupError("Windows 消息 contract 不对：%s" % action)
    if len(probe_session) != 30:
        raise RelayStartupError("sessionId 形状不对：%r" % probe_session)
    if len(probe_request) > 160:
        raise RelayStartupError("requestId 超长：%d > 160" % len(probe_request))
    _log("selftest.isolation_ok", commands=sorted(members),
         windows_types=sorted(WINDOWS_MESSAGE_KEYS))


def load_token(explicit_file: Optional[Path]) -> str:
    """token 只从环境变量 / 文件读，**绝不硬编码**。

    这把 token 能启动电脑上的语音助手并双向串音频 —— 被偷等于一条通到用户 PC 的活麦。
    所以缺失 / 过短一律**拒绝启动**，不给「先跑起来再说」的默认值。
    """
    env_token = (os.environ.get("WATCH_VOICE_TOKEN") or "").strip()
    if env_token:
        token, source = env_token, "env:WATCH_VOICE_TOKEN"
    else:
        path = explicit_file or Path(
            os.environ.get("WATCH_VOICE_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except Exception as error:                    # noqa: BLE001
            raise RelayStartupError(
                "读不到手表 token（%s）：%s: %s —— 用 "
                "`python3 -c \"import secrets;print(secrets.token_urlsafe(32))\" > %s` "
                "生成并 chmod 600。" % (path, type(error).__name__, error, path)) from error
        source = "file:%s" % path
    if len(token) < MIN_TOKEN_LENGTH:
        raise RelayStartupError(
            "手表 token 太短（%d 字符 < %d，来源 %s）。这把 token 能开一支通到你 PC 的"
            "活麦，不接受弱值。" % (len(token), MIN_TOKEN_LENGTH, source))
    _log("token.loaded", source=source, length=len(token), fingerprint=_fingerprint(token))
    return token


async def main_async(args: argparse.Namespace) -> None:
    _load_wire()
    _verify_isolation()
    token = load_token(Path(args.token_file).expanduser() if args.token_file else None)
    kill_file = Path(args.kill_file).expanduser() if args.kill_file else Path(
        os.environ.get("WATCH_VOICE_KILL_FILE") or DEFAULT_KILL_FILE).expanduser()
    relay = Relay(token, kill_file, grace_seconds=args.grace_seconds)
    if relay.kill_file_present():
        # 起得来，但一开始就是拒绝态 —— 而且说出来，别让人对着一个「服务在跑」发呆。
        relay.killed = True
        _log("kill.engaged_at_startup", path=str(kill_file))

    watchdog = asyncio.create_task(relay.watchdog_loop(), name="watchdog")
    handler = make_watch_handler(relay)
    async with websockets.serve(
        handler, args.host, args.port,
        max_size=WATCH_MAX_MESSAGE_BYTES,
        ping_interval=WATCH_PING_INTERVAL, ping_timeout=WATCH_PING_INTERVAL,
    ):
        _log("listening", host=args.host, port=args.port, windows=WINDOWS_ENDPOINT,
             origin=WINDOWS_ORIGIN, kill_file=str(kill_file),
             grace_seconds=args.grace_seconds,
             log_file=str(_LOG_PATH) if _LOG_PATH else None)
        try:
            await asyncio.Future()
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="手表 ↔ Windows 语音桥的 Pi 侧中继（减震器）")
    # ⚠ 这里**故意没有** --windows-endpoint / --windows-origin：目标地址是常量，
    #   见本文件顶部「Windows 直连合同」那段的理由。别加回来。
    parser.add_argument("--host", default=os.environ.get("WATCH_VOICE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("WATCH_VOICE_PORT", DEFAULT_PORT)))
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--kill-file", default=None)
    parser.add_argument("--log-file", default=os.environ.get("WATCH_VOICE_LOG_FILE"))
    parser.add_argument("--grace-seconds", type=float, default=WATCH_GRACE_SECONDS)
    parser.add_argument("--selftest", action="store_true",
                        help="只跑铁律二与消息形状自检然后退出（不需要 wire / token）")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    global _LOG_PATH
    log_path = Path(args.log_file).expanduser() if args.log_file else DEFAULT_LOG_FILE
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _LOG_PATH = log_path
    except Exception as error:                        # noqa: BLE001
        _log("log.dir_unavailable", path=str(log_path.parent),
             detail="%s: %s" % (type(error).__name__, error))
    if args.selftest:
        try:
            _verify_isolation()
        except RelayStartupError as error:
            _log("selftest.failed", detail=str(error))
            return 2
        _log("selftest.ok")
        return 0
    try:
        asyncio.run(main_async(args))
    except RelayStartupError as error:
        _log("startup.refused", detail=str(error))
        return 2
    except KeyboardInterrupt:
        _log("shutdown", reason="KeyboardInterrupt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
