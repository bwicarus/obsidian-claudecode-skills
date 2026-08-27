#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手表 ↔ Pi ↔ Windows 语音链路的**线格式与节拍**纯函数层(无 socket、无线程)。

Pi 在这条链路上是**减震器**,不是转发器:

- 朝 Windows 的一侧**严格**:上行 PCM 帧的序号必须恰好等于上一帧 +1、时间戳必须
  严格递增,掉一帧就是 `UPLINK_SEQUENCE_INVALID`,整通电话被挂断,而且**没有
  resume**。所以上行序号只能由 Pi 自己产生(`UplinkSequencer`),按 50 Hz 恒定
  节拍发,与手表在不在线**无关**。
- 朝手表的一侧**容错**:手表射频实测有约 5 秒空档,帧会乱序、重复、迟到。手表帧
  只决定"这一拍填什么内容",没来就填静音(`WatchJitterBuffer` + `WatchVoiceUplink`)。

一句话:**严格的一侧永不断,容错的一侧随便断。**

⚠ 本模块只做 PCM 帧。控制面(hello / start / stop / heartbeat)的消息**必须由调用方
自己拼字面量** —— 那条 WS 上复用着删卡、全局热键注入、改写 Windows 配置等 22 种
危险消息,安全边界靠"代码里不存在把手表 JSON 递给 Windows 的路径",不靠黑名单。
本文件因此**不提供**任何把外来结构转成 Windows 消息的函数,连 PCM 帧也是重编码而
不是透传:手表帧的 seq/ts/session 三个字段全部被丢弃,只有 1920 字节 payload 过河。

出处(全部按 `extensions/bw-reader-webext/windows/ComputerVoiceAudio/` 下的实现校对):
`DirectPcmFrame.cs`(帧布局)、`DirectBridgeContract.cs`(常量与 base64url)、
`Pcm48kMonoFramer.cs`(20 ms/960 采样)、`DirectBridgeServer.cs`(上行序号闸)、
`ios/BWReader/App/DirectVoiceSocket.swift`(客户端侧校验与错误码)。
"""
from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional


# ── 线格式常量 ───────────────────────────────────────────────────────────────
PCM_MAGIC = b"BWCV"
PCM_FRAME_VERSION = 1

TRACK_APP_OUTPUT = 1          # 下行:Windows → Pi(目标应用的输出)
TRACK_LEGACY_MICROPHONE = 2   # 本链路不用
TRACK_BROWSER_MICROPHONE = 3  # 上行:Pi → Windows(虚拟麦克风)

PCM_FRAME_HEADER_BYTES = 36
SAMPLE_RATE_HZ = 48_000
SAMPLES_PER_FRAME = 960
BYTES_PER_SAMPLE = 2                                        # s16le 单声道
PCM_PAYLOAD_BYTES = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE    # 1920
PCM_FRAME_BYTES = PCM_FRAME_HEADER_BYTES + PCM_PAYLOAD_BYTES  # 1956
FRAME_DURATION_US = 20_000

SILENCE_PAYLOAD = bytes(PCM_PAYLOAD_BYTES)

SESSION_ID_PREFIX = "session-"
SESSION_ID_BYTES = 16
SESSION_ID_LENGTH = len(SESSION_ID_PREFIX) + 22             # 30

# 手表 → Pi 的帧格式由我们自己定,布局与 Windows 那份逐字节相同,只换 magic。
# 换 magic 是为了"接错线要炸得响":两条 socket 万一互串,收到的帧会立刻判无效,
# 而不是被当成合法音频悄悄播出去。
WATCH_MAGIC = b"BWWV"
WATCH_FRAME_VERSION = 1

_UINT32_MAX = 0xFFFF_FFFF
_UINT64_MAX = 0xFFFF_FFFF_FFFF_FFFF

# 2 ms。够短,听不出音量变化;够长,盖住一次采样级跳变造成的"咔"。
DEFAULT_FADE_SAMPLES = SAMPLE_RATE_HZ * 2 // 1000           # 96

# 抖动缓冲默认值。上限 300 ms 与 Windows 侧自己的 200 ms 上行队列同量级 ——
# 再大就是拿通话延迟换手表的稳定性,不划算。
DEFAULT_CAPACITY_FRAMES = 15
DEFAULT_PRIME_FRAMES = 3        # 60 ms:起播前先攒一点,免得刚开口就断续
DEFAULT_GAP_WAIT_TICKS = 2      # 缺一帧最多等 40 ms,等不到就跳过,绝不回填


class WireError(ValueError):
    """线格式错误。

    code / message 与 Windows 桥、iOS 客户端**逐字一致**,三处日志才对得上;
    出问题时能直接搜同一个串,而不是三套各自的说法。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = bool(retryable)


# ── sessionId ────────────────────────────────────────────────────────────────
_SESSION_ID_INVALID = "BW_COMPUTER_VOICE_DIRECT_SESSION_ID_INVALID"
_SESSION_ID_INVALID_MESSAGE = "sessionId 必须绑定 16 字节随机值"
_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str, *, size: int, code: str) -> bytes:
    """严格 base64url 解码:padding、`+/` 别名、尾部冗余位一律拒绝。

    Windows 侧 `DirectBase64Url.Decode` 解完会**重新编码再逐字比对**。Python 的
    `urlsafe_b64decode` 对尾部冗余位是宽松的,照抄它就会造出一个本机自认合法、
    Windows 却拒收的 sessionId —— 而那要等 START 之后第一帧上行才炸。
    """
    if not text or set(text) - _B64URL_ALPHABET:
        raise WireError(code, _SESSION_ID_INVALID_MESSAGE)
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise WireError(code, _SESSION_ID_INVALID_MESSAGE) from exc
    if len(raw) != size or _b64url_encode(raw) != text:
        raise WireError(code, _SESSION_ID_INVALID_MESSAGE)
    return raw


def format_session_id(raw: bytes) -> str:
    if len(raw) != SESSION_ID_BYTES:
        raise WireError(_SESSION_ID_INVALID, _SESSION_ID_INVALID_MESSAGE)
    return SESSION_ID_PREFIX + _b64url_encode(raw)


def parse_session_id(session_id: str) -> bytes:
    if (
        not isinstance(session_id, str)
        or len(session_id) != SESSION_ID_LENGTH
        or not session_id.startswith(SESSION_ID_PREFIX)
    ):
        raise WireError(_SESSION_ID_INVALID, _SESSION_ID_INVALID_MESSAGE)
    return _b64url_decode(
        session_id[len(SESSION_ID_PREFIX):],
        size=SESSION_ID_BYTES,
        code=_SESSION_ID_INVALID,
    )


def is_valid_session_id(session_id: str) -> bool:
    try:
        parse_session_id(session_id)
    except WireError:
        return False
    return True


def new_session_id() -> str:
    """铸一个新 sessionId。

    ⚠ 手表侧的流 id 与这个 Windows sessionId **必须是两个不同的值**:手表永远不该
    知道 Windows 这一侧的会话身份,否则"手表能影响 Windows 会话"就有了第一条缝。
    """
    return format_session_id(secrets.token_bytes(SESSION_ID_BYTES))


# ── 帧编解码 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PcmFrame:
    session_id: str
    track: int
    sequence: int
    timestamp_us: int
    payload: bytes


def _encode_frame(
    magic: bytes,
    version: int,
    session_id: str,
    *,
    track: int,
    sequence: int,
    timestamp_us: int,
    payload: bytes,
    invalid_code: str,
    invalid_message: str,
) -> bytes:
    session_bytes = parse_session_id(session_id)
    if (
        len(payload) != PCM_PAYLOAD_BYTES
        or not 0 <= sequence <= _UINT32_MAX
        or not 0 <= timestamp_us <= _UINT64_MAX
    ):
        raise WireError(invalid_code, invalid_message)
    header = bytearray(PCM_FRAME_HEADER_BYTES)
    header[0:4] = magic
    header[4] = version
    header[5] = track
    header[6:8] = (0).to_bytes(2, "little")          # flags/reserved 恒 0
    header[8:24] = session_bytes
    header[24:28] = sequence.to_bytes(4, "little")
    header[28:36] = timestamp_us.to_bytes(8, "little")
    return bytes(header) + bytes(payload)


def _decode_frame(
    raw: bytes,
    *,
    magic: bytes,
    version: int,
    track: int,
    invalid_code: str,
    invalid_message: str,
) -> PcmFrame:
    data = bytes(raw)
    if (
        len(data) != PCM_FRAME_BYTES
        or data[0:4] != magic
        or data[4] != version
        or data[5] != track
        or int.from_bytes(data[6:8], "little") != 0
    ):
        raise WireError(invalid_code, invalid_message)
    return PcmFrame(
        session_id=format_session_id(data[8:24]),
        track=track,
        sequence=int.from_bytes(data[24:28], "little"),
        timestamp_us=int.from_bytes(data[28:36], "little"),
        payload=data[PCM_FRAME_HEADER_BYTES:],
    )


def encode_uplink_frame(
    session_id: str, *, sequence: int, timestamp_us: int, payload: bytes
) -> bytes:
    """Pi → Windows 的一帧(track=3)。"""
    return _encode_frame(
        PCM_MAGIC,
        PCM_FRAME_VERSION,
        session_id,
        track=TRACK_BROWSER_MICROPHONE,
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=payload,
        invalid_code="BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME",
        invalid_message="Reader 麦克风 PCM 帧或连接状态无效",
    )


def decode_uplink_frame(raw: bytes) -> PcmFrame:
    """按 **Windows 服务端**的规则解一帧上行。

    Pi 运行时并不收上行帧;这是对端 fail-closed 解码器的**模型**,存在的意义是让
    单测能用它验我们发出去的帧 —— 而不是等真机上一帧不合规就整通电话被挂断。
    """
    return _decode_frame(
        raw,
        magic=PCM_MAGIC,
        version=PCM_FRAME_VERSION,
        track=TRACK_BROWSER_MICROPHONE,
        invalid_code="BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID",
        invalid_message="浏览器麦克风 binary 帧合同无效",
    )


def encode_downlink_frame(
    session_id: str, *, sequence: int, timestamp_us: int, payload: bytes
) -> bytes:
    """Windows → Pi 的一帧(track=1)。Pi 不产生它,单测用它造对端的输入。"""
    return _encode_frame(
        PCM_MAGIC,
        PCM_FRAME_VERSION,
        session_id,
        track=TRACK_APP_OUTPUT,
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=payload,
        invalid_code="BW_COMPUTER_VOICE_DIRECT_PCM_FRAME_INVALID",
        invalid_message="PCM 帧合同无效",
    )


def decode_downlink_frame(raw: bytes, *, session_id: Optional[str] = None) -> PcmFrame:
    """按 **iOS 客户端**的规则解一帧下行,错误码逐字照抄 iOS。

    下行走的是 Pi↔Windows 同一局域网那一跳(direct 192.168.3.20),丢包不是常态,
    所以这一侧照样 fail-closed;真出问题应当拆掉 Windows 腿,而不是猜着往下播。
    ⚠ 但**不要**把这套语义原样传给手表:朝手表那一侧允许丢帧。
    """
    data = bytes(raw)
    if len(data) != PCM_FRAME_BYTES:
        raise WireError(
            "BW_COMPUTER_VOICE_DIRECT_PCM_UNEXPECTED",
            "Windows 在非通话连接发送了 PCM",
        )
    if data[0:4] != PCM_MAGIC or data[4] != PCM_FRAME_VERSION:
        raise WireError(
            "BW_COMPUTER_VOICE_DIRECT_PCM_MAGIC", "Windows PCM magic 或版本无效"
        )
    if data[5] != TRACK_APP_OUTPUT or int.from_bytes(data[6:8], "little") != 0:
        raise WireError(
            "BW_COMPUTER_VOICE_DIRECT_PCM_TRACK", "Windows PCM track 或 flags 无效"
        )
    if session_id is not None and data[8:24] != parse_session_id(session_id):
        raise WireError(
            "BW_COMPUTER_VOICE_DIRECT_PCM_SESSION",
            "Windows PCM session 与当前通话不匹配",
        )
    return PcmFrame(
        session_id=format_session_id(data[8:24]),
        track=TRACK_APP_OUTPUT,
        sequence=int.from_bytes(data[24:28], "little"),
        timestamp_us=int.from_bytes(data[28:36], "little"),
        payload=data[PCM_FRAME_HEADER_BYTES:],
    )


class DownlinkSequenceGuard:
    """下行连续性校验,与 iOS `handleBinary` 同构:首帧 seq=0、之后 +1、ts 严格递增。"""

    def __init__(self) -> None:
        self._next_sequence = 0
        self._last_timestamp_us: Optional[int] = None

    def check(self, frame: PcmFrame) -> PcmFrame:
        if frame.sequence != self._next_sequence:
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE", "Windows PCM sequence 不连续"
            )
        if (
            self._last_timestamp_us is not None
            and frame.timestamp_us <= self._last_timestamp_us
        ):
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_PCM_TIMESTAMP",
                "Windows PCM timestamp 未严格递增",
            )
        if self._next_sequence >= _UINT32_MAX:
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE", "Windows PCM sequence 已耗尽"
            )
        self._next_sequence += 1
        self._last_timestamp_us = frame.timestamp_us
        return frame


def encode_watch_frame(
    stream_id: str, *, sequence: int, timestamp_us: int, payload: bytes
) -> bytes:
    """手表 → Pi 的一帧。手表侧不在本次范围,这里给的是**可执行的参考实现**。"""
    return _encode_frame(
        WATCH_MAGIC,
        WATCH_FRAME_VERSION,
        stream_id,
        track=TRACK_BROWSER_MICROPHONE,
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=payload,
        invalid_code="BW_WATCH_VOICE_FRAME_INVALID",
        invalid_message="手表上行帧合同无效",
    )


def decode_watch_frame(raw: bytes) -> PcmFrame:
    """解一帧手表上行。抛 `WireError`;容错处理在 `WatchJitterBuffer.accept` 里。"""
    return _decode_frame(
        raw,
        magic=WATCH_MAGIC,
        version=WATCH_FRAME_VERSION,
        track=TRACK_BROWSER_MICROPHONE,
        invalid_code="BW_WATCH_VOICE_FRAME_INVALID",
        invalid_message="手表上行帧合同无效",
    )


# ── 静音与淡出 ───────────────────────────────────────────────────────────────
def _read_s16(payload: bytes, index: int) -> int:
    offset = index * BYTES_PER_SAMPLE
    return int.from_bytes(
        payload[offset:offset + BYTES_PER_SAMPLE], "little", signed=True
    )


def last_sample_of(payload: bytes) -> int:
    return _read_s16(payload, SAMPLES_PER_FRAME - 1)


def fade_to_silence(
    last_sample: int, *, fade_samples: int = DEFAULT_FADE_SAMPLES
) -> bytes:
    """从 `last_sample` 线性降到 0 的一帧,其余补零。

    波形从非零瞬间跳到 0 会在扬声器上听成"咔"。手表一断,**下一拍就必须发静音**
    (节拍不能停,否则 Windows 那侧序号就断了),所以只能在静音帧的**开头**接上
    一帧的尾巴做淡出 —— 不能回头去改已经发出去的那一帧。
    """
    if last_sample == 0:
        return SILENCE_PAYLOAD
    fade_samples = max(1, min(int(fade_samples), SAMPLES_PER_FRAME))
    out = bytearray(SILENCE_PAYLOAD)
    for index in range(fade_samples):
        value = (last_sample * (fade_samples - 1 - index)) // fade_samples
        offset = index * BYTES_PER_SAMPLE
        out[offset:offset + BYTES_PER_SAMPLE] = value.to_bytes(
            2, "little", signed=True
        )
    return bytes(out)


def apply_fade_in(
    payload: bytes, *, fade_samples: int = DEFAULT_FADE_SAMPLES
) -> bytes:
    """静音之后的第一帧真实音频,开头同样斜坡拉起 —— 咔哒声在两个方向上都存在。"""
    fade_samples = max(1, min(int(fade_samples), SAMPLES_PER_FRAME))
    out = bytearray(payload)
    for index in range(fade_samples):
        value = (_read_s16(payload, index) * (index + 1)) // fade_samples
        offset = index * BYTES_PER_SAMPLE
        out[offset:offset + BYTES_PER_SAMPLE] = value.to_bytes(
            2, "little", signed=True
        )
    return bytes(out)


# ── 上行节拍 ─────────────────────────────────────────────────────────────────
def _epoch_micros() -> int:
    return int(time.time() * 1_000_000)


class UplinkSequencer:
    """上行序号与时间戳的**唯一来源**。

    `seq = up_seq++`、`ts = up_t0 + seq * 20000`:序号天然 +1,时间戳天然严格递增,
    与手表帧到没到、到得整不整齐**完全无关**。这就是铁律一在代码里的落点 ——
    手表的帧只能决定 payload,决定不了 seq/ts。

    时间戳基准取 epoch 微秒(与 iOS 同款),纯粹为了三处日志能对上;服务端只校验
    严格递增,进了抖动队列之后 ts 就被丢弃了,基准取什么都合法。
    """

    def __init__(
        self,
        session_id: str,
        *,
        base_timestamp_us: Optional[int] = None,
        clock_us: Optional[Callable[[], int]] = None,
    ) -> None:
        parse_session_id(session_id)  # 早失败:别等 START 成功后第一帧才发现 id 不合规
        self.session_id = session_id
        if base_timestamp_us is None:
            base_timestamp_us = int((clock_us or _epoch_micros)())
        if not 0 <= base_timestamp_us <= _UINT64_MAX:
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_TIMESTAMP_INVALID",
                "Reader 麦克风 PCM 时间戳溢出",
            )
        self.base_timestamp_us = base_timestamp_us
        self._next_sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def timestamp_for(self, sequence: int) -> int:
        return self.base_timestamp_us + sequence * FRAME_DURATION_US

    def emit(self, payload: bytes) -> bytes:
        # 序号在编码前就占掉:发送失败也**不重用**这个序号。序号是节拍的坐标,
        # 补发一帧等于把整条流往回拨,Windows 侧只会判 sequence invalid。
        sequence = self._next_sequence
        if sequence >= _UINT32_MAX:
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_SEQUENCE_INVALID",
                "Reader 麦克风 PCM 序号已耗尽",
            )
        timestamp_us = self.timestamp_for(sequence)
        if timestamp_us > _UINT64_MAX:
            raise WireError(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_TIMESTAMP_INVALID",
                "Reader 麦克风 PCM 时间戳溢出",
            )
        raw = encode_uplink_frame(
            self.session_id,
            sequence=sequence,
            timestamp_us=timestamp_us,
            payload=payload,
        )
        self._next_sequence = sequence + 1
        return raw


# ── 手表侧抖动缓冲 ───────────────────────────────────────────────────────────
ACCEPTED = "accepted"
RESET = "reset"          # 认出手表重开了序号空间,已重新起算
DUPLICATE = "duplicate"
LATE = "late"            # 这一拍早发出去了,丢掉,不回填
EVICTED = "evicted"      # 收下了,但把最老的一帧挤出去以维持有界延迟
MALFORMED = "malformed"
FOREIGN = "foreign"      # 上一条连接的残帧


@dataclass(frozen=True)
class WatchFrameOutcome:
    """每一帧的去向都有说法。

    容错**不等于**沉默:`silent-failure-lessons.md` 那十处的共同形态就是"出了状况
    就悄悄什么都不做"。这里对内容问题一律不抛异常(手表是容错的一侧),但每次都
    带回 status + 人话 reason,让调用方能记日志、能上计数。
    """

    status: str
    sequence: Optional[int] = None
    reason: str = ""

    @property
    def buffered(self) -> bool:
        return self.status in (ACCEPTED, RESET, EVICTED)


class WatchJitterBuffer:
    """手表 → Pi 方向的抖动缓冲。**容错的一侧**。

    手表自带一套独立序号空间(自己从 0 起,断线重连后还会从 0 再来一次)。这里只做
    排序、去重、丢迟到三件事,**任何情况下都不让上行节拍等它**:取不到就是取不到,
    由 `WatchVoiceUplink` 填静音。

    重连的权威信号是 `rebind()`(Pi 每条手表连接铸一个新 stream_id),`restart_gap`
    只是兜底:手表若自己把计数器归零而没经过 rebind,序号会**大幅**倒退,与"迟到
    几帧"区分得开,于是自愈而不是永久卡死。
    """

    def __init__(
        self,
        *,
        stream_id: Optional[str] = None,
        capacity_frames: int = DEFAULT_CAPACITY_FRAMES,
        prime_frames: int = DEFAULT_PRIME_FRAMES,
        gap_wait_ticks: int = DEFAULT_GAP_WAIT_TICKS,
        restart_gap: int = 50,
    ) -> None:
        self.stream_id = stream_id
        self.capacity_frames = max(1, int(capacity_frames))
        self.prime_frames = max(0, int(prime_frames))
        self.gap_wait_ticks = max(0, int(gap_wait_ticks))
        self.restart_gap = max(1, int(restart_gap))
        self._frames: dict[int, bytes] = {}
        self._next_sequence = 0
        self._priming = True
        self._gap_ticks = 0
        self.counters: dict[str, int] = {
            ACCEPTED: 0, RESET: 0, DUPLICATE: 0, LATE: 0, EVICTED: 0,
            MALFORMED: 0, FOREIGN: 0, "starved": 0, "priming": 0,
            "waited": 0, "skipped": 0, "played": 0,
        }

    @property
    def depth(self) -> int:
        return len(self._frames)

    def rebind(self, stream_id: Optional[str]) -> None:
        """换一条手表连接:清空缓冲、序号空间重新起算。旧 stream_id 的残帧此后判 FOREIGN。"""
        if stream_id is not None:
            parse_session_id(stream_id)
        self.stream_id = stream_id
        self._frames.clear()
        self._next_sequence = 0
        self._priming = True
        self._gap_ticks = 0

    def _bump(self, outcome: WatchFrameOutcome) -> WatchFrameOutcome:
        self.counters[outcome.status] = self.counters.get(outcome.status, 0) + 1
        return outcome

    def accept(self, raw: bytes) -> WatchFrameOutcome:
        try:
            frame = decode_watch_frame(raw)
        except WireError as exc:
            return self._bump(WatchFrameOutcome(MALFORMED, None, exc.message))
        if self.stream_id is not None and frame.session_id != self.stream_id:
            return self._bump(
                WatchFrameOutcome(FOREIGN, frame.sequence, "帧来自已结束的手表连接")
            )

        status = ACCEPTED
        reason = ""
        if frame.sequence + self.restart_gap < self._next_sequence:
            self._frames.clear()
            self._next_sequence = frame.sequence
            self._priming = True
            self._gap_ticks = 0
            status = RESET
            reason = "手表序号大幅倒退,按重开一条流处理"
        elif frame.sequence < self._next_sequence:
            return self._bump(
                WatchFrameOutcome(LATE, frame.sequence, "这一拍已经发出去了")
            )
        elif frame.sequence in self._frames:
            return self._bump(
                WatchFrameOutcome(DUPLICATE, frame.sequence, "缓冲里已有同序号帧")
            )

        self._frames[frame.sequence] = frame.payload
        if len(self._frames) > self.capacity_frames:
            stale = min(self._frames)
            del self._frames[stale]
            # 挤掉的那帧不能再被等待,否则 pop() 会白等 gap_wait_ticks。
            self._next_sequence = max(self._next_sequence, stale + 1)
            if status == ACCEPTED:
                status = EVICTED
                reason = "缓冲已满,丢掉最老的一帧以维持有界延迟"
        return self._bump(WatchFrameOutcome(status, frame.sequence, reason))

    def pop(self) -> Optional[bytes]:
        """取"这一拍"的 payload;取不到返回 None,**绝不阻塞节拍**。"""
        if not self._frames:
            self._priming = True
            self._gap_ticks = 0
            self.counters["starved"] += 1
            return None
        if self._priming:
            if len(self._frames) < self.prime_frames:
                self.counters["priming"] += 1
                return None
            self._priming = False
            # 起播点对齐到缓冲里最早的一帧:空档期间那些没来的帧就当没有过,
            # 不回填、不倒着播。
            self._next_sequence = min(self._frames)
        if self._next_sequence not in self._frames:
            if self._gap_ticks < self.gap_wait_ticks:
                self._gap_ticks += 1
                self.counters["waited"] += 1
                return None
            self._next_sequence = min(self._frames)
            self._gap_ticks = 0
            self.counters["skipped"] += 1
        payload = self._frames.pop(self._next_sequence)
        self._next_sequence += 1
        self._gap_ticks = 0
        self.counters["played"] += 1
        return payload


class WatchVoiceUplink:
    """一通电话的上行侧:手表帧进来,50 Hz 恒定节拍的 Windows 帧出去。

    `tick()` **一定**返回一帧 —— 这是与 Windows 的契约里唯一不能协商的部分。调用方
    只需按 20 ms 一拍调它,手表在不在线都一样。
    """

    def __init__(
        self,
        session_id: str,
        *,
        base_timestamp_us: Optional[int] = None,
        clock_us: Optional[Callable[[], int]] = None,
        buffer: Optional[WatchJitterBuffer] = None,
        fade_samples: int = DEFAULT_FADE_SAMPLES,
    ) -> None:
        self.sequencer = UplinkSequencer(
            session_id, base_timestamp_us=base_timestamp_us, clock_us=clock_us
        )
        self.buffer = buffer if buffer is not None else WatchJitterBuffer()
        self.fade_samples = int(fade_samples)
        self._last_sample = 0
        self._silent = True
        self._from_watch = 0
        self._filled = 0

    @property
    def session_id(self) -> str:
        return self.sequencer.session_id

    def rebind_watch(self, stream_id: Optional[str]) -> None:
        self.buffer.rebind(stream_id)

    def on_watch_frame(self, raw: bytes) -> WatchFrameOutcome:
        return self.buffer.accept(raw)

    def tick(self) -> bytes:
        payload = self.buffer.pop()
        if payload is None:
            payload = fade_to_silence(
                self._last_sample, fade_samples=self.fade_samples
            )
            self._last_sample = 0
            self._silent = True
            self._filled += 1
        else:
            if self._silent:
                payload = apply_fade_in(payload, fade_samples=self.fade_samples)
            self._last_sample = last_sample_of(payload)
            self._silent = False
            self._from_watch += 1
        return self.sequencer.emit(payload)

    @property
    def stats(self) -> dict[str, int]:
        """诊断出口。手表上没有控制台,不上报等于不可诊断。"""
        merged = dict(self.buffer.counters)
        merged.update(
            {
                "uplink_sequence": self.sequencer.next_sequence,
                "from_watch": self._from_watch,
                "filled": self._filled,
                "depth": self.buffer.depth,
            }
        )
        return merged
