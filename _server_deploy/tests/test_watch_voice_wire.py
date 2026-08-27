#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`watch_voice_wire` 的单测。

跑法(仓库里没有 pytest):
    python -m unittest discover -s _server_deploy/tests -p "test_watch_voice_wire.py" -v

这里最重要的不是覆盖率,是两类断言:

1. **铁律一的可执行版**:手表怎么断、怎么乱序、怎么重连,上行序号都必须连续、
   时间戳都必须严格递增 —— 因为 Windows 那侧掉一帧就挂断整通电话,且没有 resume。
   验这条不变量用的是 `_WindowsUplinkGuardModel`:**独立手写**的对端解码器模型,
   不调被测模块的任何一个函数。
2. **逐字节布局**:至少有一处拿手写的期望字节比对。用自己的编码器验自己的解码器
   等于没验。
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from watch_voice_wire import (  # noqa: E402
    ACCEPTED,
    DEFAULT_FADE_SAMPLES,
    DUPLICATE,
    EVICTED,
    FOREIGN,
    FRAME_DURATION_US,
    LATE,
    MALFORMED,
    PCM_FRAME_BYTES,
    PCM_FRAME_HEADER_BYTES,
    PCM_PAYLOAD_BYTES,
    RESET,
    SAMPLES_PER_FRAME,
    SESSION_ID_LENGTH,
    SILENCE_PAYLOAD,
    DownlinkSequenceGuard,
    UplinkSequencer,
    WatchJitterBuffer,
    WatchVoiceUplink,
    WireError,
    apply_fade_in,
    decode_downlink_frame,
    decode_uplink_frame,
    decode_watch_frame,
    encode_downlink_frame,
    encode_uplink_frame,
    encode_watch_frame,
    fade_to_silence,
    format_session_id,
    last_sample_of,
    new_session_id,
    parse_session_id,
)


# 手写的固定夹具:16 字节 00..0f,其 base64url 是逐字算出来的,不是跑编码器得到的。
FIXED_SESSION_BYTES = bytes(range(16))
FIXED_SESSION_ID = "session-AAECAwQFBgcICQoLDA0ODw"
OTHER_SESSION_ID = "session-" + "B" * 21 + "A"


def marker_payload(value: int) -> bytes:
    """每个采样都等于 `value` 的一帧,便于断言"这一拍播的是哪一帧"。"""
    return bytes([value & 0xFF, 0x00]) * SAMPLES_PER_FRAME


class _WindowsUplinkGuardModel:
    """独立复刻 `DirectBridgeServer.cs` 的 `DirectUplinkSequenceGuard`。

    故意**手工解析** header,不复用 `decode_uplink_frame`:被测模块自证不算证据。
    """

    def __init__(self, session_bytes: bytes):
        self.session_bytes = session_bytes
        self.frames = 0
        self._last_sequence = None
        self._last_timestamp = None

    def feed(self, raw: bytes) -> bytes:
        assert len(raw) == 1956, f"binary 上限就是 1956,收到 {len(raw)}"
        assert raw[0:4] == b"BWCV", "magic 不符"
        assert raw[4] == 1, "version 不符"
        assert raw[5] == 3, "客户端 binary 只允许浏览器麦克风轨道"
        assert int.from_bytes(raw[6:8], "little") == 0, "flags 必须为 0"
        assert raw[8:24] == self.session_bytes, "浏览器麦克风 binary 会话不匹配"
        sequence = int.from_bytes(raw[24:28], "little")
        timestamp = int.from_bytes(raw[28:36], "little")
        if self._last_sequence is None:
            assert sequence == 0, f"首帧序号必须是 0,收到 {sequence}"
        else:
            assert sequence == self._last_sequence + 1, (
                f"序号必须恰好 +1:{self._last_sequence} → {sequence}"
            )
            assert timestamp > self._last_timestamp, (
                f"时间戳必须严格递增:{self._last_timestamp} → {timestamp}"
            )
        self._last_sequence = sequence
        self._last_timestamp = timestamp
        self.frames += 1
        return raw[36:]


class SessionIdTest(unittest.TestCase):
    def test_hand_written_session_id_matches_encoder(self):
        self.assertEqual(format_session_id(FIXED_SESSION_BYTES), FIXED_SESSION_ID)
        self.assertEqual(parse_session_id(FIXED_SESSION_ID), FIXED_SESSION_BYTES)
        self.assertEqual(len(FIXED_SESSION_ID), SESSION_ID_LENGTH)

    def test_round_trip(self):
        for _ in range(32):
            value = new_session_id()
            self.assertEqual(len(value), SESSION_ID_LENGTH)
            self.assertEqual(format_session_id(parse_session_id(value)), value)

    def test_rejects_non_canonical_tail_bits(self):
        # 最后一个字符只承载 4 个有效位,'x' 比 'w' 多带了不该有的尾位。
        # Windows 侧解完会重新编码逐字比对,这种 id 到那边必然被拒。
        with self.assertRaises(WireError) as ctx:
            parse_session_id("session-AAECAwQFBgcICQoLDA0ODx")
        self.assertEqual(
            ctx.exception.code, "BW_COMPUTER_VOICE_DIRECT_SESSION_ID_INVALID"
        )

    def test_rejects_standard_base64_aliases_and_padding(self):
        for bad in (
            "session-AAECAwQFBgcICQoLDA0OD+",
            "session-AAECAwQFBgcICQoLDA0OD/",
            "session-AAECAwQFBgcICQoLDA0OD=",
            "session-AAECAwQFBgcICQoLDA0OD",   # 长度不足
            "call-AAECAwQFBgcICQoLDA0ODwAA",   # 前缀不对
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(WireError):
                    parse_session_id(bad)


class FrameLayoutTest(unittest.TestCase):
    """逐字节布局。期望字节是手写的,不经过被测编码器。"""

    SEQUENCE = 0x0102_0304
    TIMESTAMP = 0x0102_0304_0506_0708
    PAYLOAD = b"\x01\x02" * SAMPLES_PER_FRAME
    UPLINK_HEADER = (
        b"BWCV"                                  # 0..4   magic
        b"\x01"                                  # 4      version
        b"\x03"                                  # 5      track = BrowserMicrophone
        b"\x00\x00"                              # 6..8   flags/reserved
        b"\x00\x01\x02\x03\x04\x05\x06\x07"      # 8..24  sessionId 原始字节
        b"\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
        b"\x04\x03\x02\x01"                      # 24..28 sequence  uint32 LE
        b"\x08\x07\x06\x05\x04\x03\x02\x01"      # 28..36 timestamp uint64 LE
    )

    def test_frame_size_constants(self):
        self.assertEqual(PCM_FRAME_HEADER_BYTES, 36)
        self.assertEqual(PCM_PAYLOAD_BYTES, 1920)
        self.assertEqual(PCM_FRAME_BYTES, 1956)
        self.assertEqual(len(self.UPLINK_HEADER), 36)
        self.assertEqual(len(self.PAYLOAD), 1920)

    def test_uplink_frame_is_byte_exact(self):
        raw = encode_uplink_frame(
            FIXED_SESSION_ID,
            sequence=self.SEQUENCE,
            timestamp_us=self.TIMESTAMP,
            payload=self.PAYLOAD,
        )
        self.assertEqual(raw, self.UPLINK_HEADER + self.PAYLOAD)

    def test_decoder_reads_hand_written_bytes(self):
        frame = decode_uplink_frame(self.UPLINK_HEADER + self.PAYLOAD)
        self.assertEqual(frame.session_id, FIXED_SESSION_ID)
        self.assertEqual(frame.track, 3)
        self.assertEqual(frame.sequence, self.SEQUENCE)
        self.assertEqual(frame.timestamp_us, self.TIMESTAMP)
        self.assertEqual(frame.payload, self.PAYLOAD)

    def test_downlink_track_byte_is_one(self):
        raw = encode_downlink_frame(
            FIXED_SESSION_ID,
            sequence=0,
            timestamp_us=0,
            payload=self.PAYLOAD,
        )
        self.assertEqual(raw[0:6], b"BWCV\x01\x01")
        self.assertEqual(raw[24:36], bytes(12))
        self.assertEqual(decode_downlink_frame(raw).payload, self.PAYLOAD)

    def test_watch_frame_uses_a_distinct_magic(self):
        raw = encode_watch_frame(
            FIXED_SESSION_ID, sequence=1, timestamp_us=2, payload=self.PAYLOAD
        )
        self.assertEqual(raw[0:6], b"BWWV\x01\x03")
        # 两条 socket 万一接串,必须立刻炸,不能被当成合法音频播出去。
        with self.assertRaises(WireError):
            decode_uplink_frame(raw)
        with self.assertRaises(WireError):
            decode_watch_frame(self.UPLINK_HEADER + self.PAYLOAD)


class CodecTest(unittest.TestCase):
    def test_uplink_round_trip(self):
        payload = bytes((index * 7) % 256 for index in range(PCM_PAYLOAD_BYTES))
        raw = encode_uplink_frame(
            FIXED_SESSION_ID, sequence=4_294_967_294, timestamp_us=2**63,
            payload=payload,
        )
        frame = decode_uplink_frame(raw)
        self.assertEqual(frame.session_id, FIXED_SESSION_ID)
        self.assertEqual(frame.sequence, 4_294_967_294)
        self.assertEqual(frame.timestamp_us, 2**63)
        self.assertEqual(frame.payload, payload)

    def test_uplink_decoder_is_fail_closed(self):
        good = encode_uplink_frame(
            FIXED_SESSION_ID, sequence=0, timestamp_us=0, payload=SILENCE_PAYLOAD
        )
        broken = {
            "短一字节": good[:-1],
            "长一字节": good + b"\x00",
            "magic": b"XWCV" + good[4:],
            "version": good[:4] + b"\x02" + good[5:],
            "track": good[:5] + b"\x01" + good[6:],
            "flags": good[:6] + b"\x01\x00" + good[8:],
        }
        for name, raw in broken.items():
            with self.subTest(name=name):
                with self.assertRaises(WireError) as ctx:
                    decode_uplink_frame(raw)
                self.assertEqual(
                    ctx.exception.code,
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID",
                )

    def test_encoder_rejects_wrong_payload_length(self):
        for payload in (b"", SILENCE_PAYLOAD[:-2], SILENCE_PAYLOAD + b"\x00\x00"):
            with self.subTest(size=len(payload)):
                with self.assertRaises(WireError):
                    encode_uplink_frame(
                        FIXED_SESSION_ID, sequence=0, timestamp_us=0, payload=payload
                    )

    def test_downlink_guard_requires_continuity(self):
        guard = DownlinkSequenceGuard()
        for sequence in range(3):
            guard.check(
                decode_downlink_frame(
                    encode_downlink_frame(
                        FIXED_SESSION_ID,
                        sequence=sequence,
                        timestamp_us=sequence * FRAME_DURATION_US,
                        payload=SILENCE_PAYLOAD,
                    )
                )
            )
        skipped = decode_downlink_frame(
            encode_downlink_frame(
                FIXED_SESSION_ID, sequence=4, timestamp_us=99, payload=SILENCE_PAYLOAD
            )
        )
        with self.assertRaises(WireError) as ctx:
            guard.check(skipped)
        self.assertEqual(ctx.exception.code, "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE")

    def test_downlink_guard_requires_strictly_increasing_timestamp(self):
        guard = DownlinkSequenceGuard()
        guard.check(
            decode_downlink_frame(
                encode_downlink_frame(
                    FIXED_SESSION_ID, sequence=0, timestamp_us=500,
                    payload=SILENCE_PAYLOAD,
                )
            )
        )
        stalled = decode_downlink_frame(
            encode_downlink_frame(
                FIXED_SESSION_ID, sequence=1, timestamp_us=500,
                payload=SILENCE_PAYLOAD,
            )
        )
        with self.assertRaises(WireError) as ctx:
            guard.check(stalled)
        self.assertEqual(ctx.exception.code, "BW_COMPUTER_VOICE_DIRECT_PCM_TIMESTAMP")

    def test_downlink_rejects_another_session(self):
        raw = encode_downlink_frame(
            FIXED_SESSION_ID, sequence=0, timestamp_us=0, payload=SILENCE_PAYLOAD
        )
        with self.assertRaises(WireError) as ctx:
            decode_downlink_frame(raw, session_id=OTHER_SESSION_ID)
        self.assertEqual(ctx.exception.code, "BW_COMPUTER_VOICE_DIRECT_PCM_SESSION")


class SilenceAndFadeTest(unittest.TestCase):
    def test_silence_payload_is_all_zero(self):
        self.assertEqual(len(SILENCE_PAYLOAD), PCM_PAYLOAD_BYTES)
        self.assertEqual(SILENCE_PAYLOAD, bytes(PCM_PAYLOAD_BYTES))

    def test_fade_to_silence_decays_monotonically_to_zero(self):
        faded = fade_to_silence(20_000, fade_samples=8)
        samples = [
            int.from_bytes(faded[i * 2:i * 2 + 2], "little", signed=True)
            for i in range(SAMPLES_PER_FRAME)
        ]
        self.assertEqual(len(faded), PCM_PAYLOAD_BYTES)
        self.assertLess(samples[0], 20_000)
        for left, right in zip(samples[:8], samples[1:8]):
            self.assertGreaterEqual(left, right)
        self.assertEqual(samples[7], 0)
        self.assertEqual(faded[16:], SILENCE_PAYLOAD[16:])

    def test_fade_to_silence_handles_negative_tail(self):
        faded = fade_to_silence(-30_000, fade_samples=4)
        first = int.from_bytes(faded[0:2], "little", signed=True)
        self.assertLess(first, 0)
        self.assertGreaterEqual(first, -30_000)
        self.assertEqual(faded[8:], SILENCE_PAYLOAD[8:])

    def test_fade_from_digital_zero_is_plain_silence(self):
        self.assertEqual(fade_to_silence(0), SILENCE_PAYLOAD)

    def test_apply_fade_in_only_touches_the_ramp(self):
        payload = marker_payload(77)
        faded = apply_fade_in(payload, fade_samples=8)
        self.assertEqual(faded[16:], payload[16:])
        self.assertLess(
            int.from_bytes(faded[0:2], "little", signed=True), 77
        )
        self.assertEqual(int.from_bytes(faded[14:16], "little", signed=True), 77)

    def test_fade_of_one_sample_is_a_no_op_for_fade_in(self):
        # 单测里要断言"播的是哪一帧"就得让淡入淡出不改数据,fade_samples=1 是那个开关。
        payload = marker_payload(5)
        self.assertEqual(apply_fade_in(payload, fade_samples=1), payload)
        self.assertEqual(fade_to_silence(5, fade_samples=1), SILENCE_PAYLOAD)

    def test_last_sample_of(self):
        self.assertEqual(last_sample_of(marker_payload(9)), 9)
        self.assertEqual(last_sample_of(SILENCE_PAYLOAD), 0)


class UplinkSequencerTest(unittest.TestCase):
    def test_sequence_and_timestamp_formula(self):
        sequencer = UplinkSequencer(FIXED_SESSION_ID, base_timestamp_us=1_000_000)
        for expected in range(5):
            frame = decode_uplink_frame(sequencer.emit(SILENCE_PAYLOAD))
            self.assertEqual(frame.sequence, expected)
            self.assertEqual(
                frame.timestamp_us, 1_000_000 + expected * FRAME_DURATION_US
            )
        self.assertEqual(sequencer.next_sequence, 5)

    def test_clock_is_read_once_at_construction(self):
        ticks = iter([7_000_000, 9_999_999_999])
        sequencer = UplinkSequencer(FIXED_SESSION_ID, clock_us=lambda: next(ticks))
        self.assertEqual(sequencer.base_timestamp_us, 7_000_000)
        self.assertEqual(sequencer.timestamp_for(3), 7_000_000 + 60_000)

    def test_rejects_bad_session_id_before_the_first_frame(self):
        with self.assertRaises(WireError):
            UplinkSequencer("session-not-a-real-binding")

    def test_sequence_exhaustion_is_terminal(self):
        sequencer = UplinkSequencer(FIXED_SESSION_ID, base_timestamp_us=0)
        sequencer._next_sequence = 0xFFFF_FFFF
        with self.assertRaises(WireError) as ctx:
            sequencer.emit(SILENCE_PAYLOAD)
        self.assertEqual(
            ctx.exception.code, "BW_COMPUTER_VOICE_DIRECT_UPLINK_SEQUENCE_INVALID"
        )


class WatchJitterBufferTest(unittest.TestCase):
    def setUp(self):
        self.buffer = WatchJitterBuffer(
            stream_id=FIXED_SESSION_ID, prime_frames=1, gap_wait_ticks=0
        )

    def feed(self, sequence, value=None, stream_id=None):
        return self.buffer.accept(
            encode_watch_frame(
                stream_id or FIXED_SESSION_ID,
                sequence=sequence,
                timestamp_us=sequence * FRAME_DURATION_US,
                payload=marker_payload(sequence if value is None else value),
            )
        )

    def test_in_order_frames_play_in_order(self):
        for sequence in range(3):
            self.assertEqual(self.feed(sequence).status, ACCEPTED)
        self.assertEqual(
            [last_sample_of(self.buffer.pop()) for _ in range(3)], [0, 1, 2]
        )
        self.assertIsNone(self.buffer.pop())

    def test_out_of_order_frames_are_reordered(self):
        for sequence in (2, 0, 1):
            self.feed(sequence)
        self.assertEqual(
            [last_sample_of(self.buffer.pop()) for _ in range(3)], [0, 1, 2]
        )

    def test_duplicate_is_reported_and_ignored(self):
        self.feed(0)
        outcome = self.feed(0, value=99)
        self.assertEqual(outcome.status, DUPLICATE)
        self.assertTrue(outcome.reason)
        self.assertEqual(last_sample_of(self.buffer.pop()), 0)

    def test_late_frame_is_dropped_not_backfilled(self):
        self.feed(0)
        self.feed(1)
        self.buffer.pop()
        self.buffer.pop()
        outcome = self.feed(0, value=99)
        self.assertEqual(outcome.status, LATE)
        self.assertEqual(self.buffer.depth, 0)

    def test_buffered_flag_covers_every_accepted_status(self):
        # 调用方不该自己去记"哪几个状态算收下了"—— 那种表一定会漏一项。
        self.assertTrue(self.feed(0).buffered)
        self.assertFalse(self.feed(0).buffered)                      # DUPLICATE
        self.assertFalse(self.buffer.accept(b"nope").buffered)       # MALFORMED
        self.assertFalse(
            self.feed(1, stream_id=OTHER_SESSION_ID).buffered        # FOREIGN
        )

    def test_malformed_frame_is_reported_not_raised(self):
        outcome = self.buffer.accept(b"nope")
        self.assertEqual(outcome.status, MALFORMED)
        self.assertTrue(outcome.reason)
        self.assertEqual(self.buffer.counters[MALFORMED], 1)

    def test_frames_from_a_dead_connection_are_foreign(self):
        outcome = self.feed(0, stream_id=OTHER_SESSION_ID)
        self.assertEqual(outcome.status, FOREIGN)
        self.assertEqual(self.buffer.depth, 0)

    def test_capacity_drops_the_oldest_frame(self):
        buffer = WatchJitterBuffer(
            stream_id=FIXED_SESSION_ID, capacity_frames=3, prime_frames=1,
            gap_wait_ticks=0,
        )
        self.buffer = buffer
        for sequence in range(4):
            outcome = self.feed(sequence)
        self.assertEqual(outcome.status, EVICTED)
        self.assertEqual(buffer.depth, 3)
        self.assertEqual(
            [last_sample_of(buffer.pop()) for _ in range(3)], [1, 2, 3]
        )

    def test_a_hole_is_waited_for_then_skipped(self):
        buffer = WatchJitterBuffer(
            stream_id=FIXED_SESSION_ID, prime_frames=1, gap_wait_ticks=2
        )
        self.buffer = buffer
        self.feed(0)
        self.assertEqual(last_sample_of(buffer.pop()), 0)
        self.feed(2)
        self.assertIsNone(buffer.pop())          # 等 20 ms
        self.assertIsNone(buffer.pop())          # 再等 20 ms
        self.assertEqual(last_sample_of(buffer.pop()), 2)   # 放弃那一帧,跳过去
        self.assertEqual(buffer.counters["skipped"], 1)

    def test_a_backwards_jump_restarts_the_sequence_space(self):
        for sequence in range(60, 65):
            self.feed(sequence)
        for _ in range(5):
            self.buffer.pop()
        outcome = self.feed(0, value=7)
        self.assertEqual(outcome.status, RESET)
        self.assertEqual(last_sample_of(self.buffer.pop()), 7)


class UplinkInvariantTest(unittest.TestCase):
    """铁律一:严格的一侧永不断。"""

    BASE_US = 1_700_000_000_000_000

    def make(self, **buffer_kwargs):
        kwargs = {"stream_id": FIXED_SESSION_ID, "prime_frames": 1,
                  "gap_wait_ticks": 0}
        kwargs.update(buffer_kwargs)
        uplink = WatchVoiceUplink(
            OTHER_SESSION_ID,
            base_timestamp_us=self.BASE_US,
            buffer=WatchJitterBuffer(**kwargs),
            fade_samples=1,   # 让淡入淡出成为恒等变换,好断言"播的是哪一帧"
        )
        return uplink, _WindowsUplinkGuardModel(parse_session_id(OTHER_SESSION_ID))

    def watch_frame(self, sequence, value=None, stream_id=FIXED_SESSION_ID):
        return encode_watch_frame(
            stream_id,
            sequence=sequence,
            timestamp_us=sequence * FRAME_DURATION_US,
            payload=marker_payload(sequence if value is None else value),
        )

    def test_watch_id_never_becomes_the_windows_session_id(self):
        # 零转发的最小可测形态:手表那一侧的身份到不了 Windows 那一侧。
        uplink, _ = self.make()
        self.assertNotEqual(uplink.session_id, FIXED_SESSION_ID)
        uplink.on_watch_frame(self.watch_frame(0))
        frame = decode_uplink_frame(uplink.tick())
        self.assertEqual(frame.session_id, OTHER_SESSION_ID)

    def test_sequence_stays_continuous_when_every_watch_frame_is_lost(self):
        uplink, guard = self.make()
        for _ in range(300):          # 6 秒,比实测的 5 秒空档还长
            guard.feed(uplink.tick())
        self.assertEqual(guard.frames, 300)
        self.assertEqual(uplink.stats["filled"], 300)
        self.assertEqual(uplink.stats["from_watch"], 0)

    def test_silence_fill_is_really_silent(self):
        uplink, guard = self.make()
        payloads = [guard.feed(uplink.tick()) for _ in range(10)]
        self.assertTrue(all(payload == SILENCE_PAYLOAD for payload in payloads))

    def test_tick_never_returns_none_even_when_fed_garbage(self):
        uplink, guard = self.make()
        for junk in (b"", b"BWWV", b"\x00" * PCM_FRAME_BYTES, b"x" * 5000):
            outcome = uplink.on_watch_frame(junk)
            self.assertEqual(outcome.status, MALFORMED)
            payload = guard.feed(uplink.tick())
            self.assertEqual(len(payload), PCM_PAYLOAD_BYTES)
        self.assertEqual(guard.frames, 4)

    def test_watch_reconnect_does_not_skip_an_uplink_sequence(self):
        uplink, guard = self.make()
        sequences = []

        def beat():
            raw = uplink.tick()
            guard.feed(raw)
            sequences.append(decode_uplink_frame(raw).sequence)

        for sequence in range(5):
            uplink.on_watch_frame(self.watch_frame(sequence))
        for _ in range(5):
            beat()

        for _ in range(250):          # 手表息屏切网,5 秒不见
            beat()

        # 重连:Pi 铸一条新的手表流,旧连接的残帧还在飞。
        uplink.rebind_watch(OTHER_SESSION_ID)
        stale = uplink.on_watch_frame(self.watch_frame(5))
        self.assertEqual(stale.status, FOREIGN)

        for sequence in range(5):     # 手表自己的序号从 0 重新开始
            uplink.on_watch_frame(
                self.watch_frame(sequence, stream_id=OTHER_SESSION_ID)
            )
        for _ in range(5):
            beat()

        self.assertEqual(sequences, list(range(260)))
        self.assertEqual(guard.frames, 260)
        self.assertEqual(uplink.stats["from_watch"], 10)
        self.assertEqual(uplink.stats["filled"], 250)

    def test_timestamps_are_strictly_increasing_across_a_dropout(self):
        uplink, guard = self.make()
        stamps = []
        for tick in range(120):
            if tick % 17 == 0:        # 断断续续地喂,故意不整齐
                uplink.on_watch_frame(self.watch_frame(tick))
            raw = uplink.tick()
            guard.feed(raw)
            stamps.append(decode_uplink_frame(raw).timestamp_us)
        self.assertEqual(stamps[0], self.BASE_US)
        self.assertEqual(
            stamps, [self.BASE_US + i * FRAME_DURATION_US for i in range(120)]
        )

    def test_a_late_frame_does_not_disturb_the_beat(self):
        uplink, guard = self.make()
        played = []

        def beat():
            played.append(last_sample_of(guard.feed(uplink.tick())))

        for sequence in range(3):
            uplink.on_watch_frame(self.watch_frame(sequence))
        for _ in range(3):
            beat()

        outcome = uplink.on_watch_frame(self.watch_frame(1, value=99))
        self.assertEqual(outcome.status, LATE)

        uplink.on_watch_frame(self.watch_frame(3))
        beat()
        self.assertEqual(played, [0, 1, 2, 3])   # 99 从没被播过

    def test_reordered_burst_is_replayed_in_order(self):
        uplink, guard = self.make(prime_frames=3)
        for sequence in (3, 1, 0, 2):            # 一个乱序的突发
            uplink.on_watch_frame(self.watch_frame(sequence))
        played = [last_sample_of(guard.feed(uplink.tick())) for _ in range(4)]
        self.assertEqual(played, [0, 1, 2, 3])

    def test_default_fade_still_produces_valid_frames(self):
        # 前面的不变量测试把淡入淡出关了;这里确认真实参数下帧仍然合规。
        uplink = WatchVoiceUplink(
            OTHER_SESSION_ID,
            base_timestamp_us=self.BASE_US,
            buffer=WatchJitterBuffer(
                stream_id=FIXED_SESSION_ID, prime_frames=1, gap_wait_ticks=0
            ),
        )
        guard = _WindowsUplinkGuardModel(parse_session_id(OTHER_SESSION_ID))
        for sequence in range(3):
            uplink.on_watch_frame(self.watch_frame(sequence, value=100))
        for _ in range(3):
            guard.feed(uplink.tick())
        first_fill = guard.feed(uplink.tick())   # 手表没了,这一拍要淡出
        head = int.from_bytes(first_fill[0:2], "little", signed=True)
        self.assertLess(0, head)
        self.assertLess(head, 100)
        self.assertEqual(
            first_fill[DEFAULT_FADE_SAMPLES * 2:],
            SILENCE_PAYLOAD[DEFAULT_FADE_SAMPLES * 2:],
        )
        self.assertEqual(guard.feed(uplink.tick()), SILENCE_PAYLOAD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
