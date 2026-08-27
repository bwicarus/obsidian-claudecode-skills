#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`watch_voice_relay` 的单测：两条铁律的**可执行版**，加上跟 wire 的接口一致性。

跑法（仓库里没有 pytest）：
    python -m unittest discover -s _server_deploy/tests -p "test_watch_voice*.py" -v

`test_watch_voice_wire.py` 验的是纯函数层；这一份验的是**进程接线**，因为两条铁律
真正会破的地方都在接线上：

1. **铁律二（零转发）**必须在代码结构上成立，不能只靠注释。所以这里既有 AST 层的
   断言（发往 Windows 的字节只可能从两个函数出去），也有跑真 WebSocket 的端到端
   断言（手表拼命发危险 op，记录下来的 Windows 流量里一个字都没有）。
2. **铁律一（Pi 自己当时钟）**必须在手表**完全没上线**时也成立。所以这里起真的
   asyncio 服务端、真的节拍器，用一个**独立手写**的对端解码器验收 —— 不调被测模块
   的任何一个解码函数。

⚠ 这里刻意不 mock 节拍器。手表侧实测过 `sleep(0.02)` 累加会漂到 1/3 速率，而那种
错误在「把 tick 调 N 次」的假时钟下**永远测不出来**：假时钟测的是次数，漂移是时间。
所以 `test_uplink_runs_at_50hz…` 用墙钟量真实速率。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import websockets                                     # noqa: E402
from websockets.exceptions import ConnectionClosed    # noqa: E402

import watch_voice_relay as relay_mod                 # noqa: E402
import watch_voice_wire as wire_mod                   # noqa: E402

RELAY_SOURCE = (ROOT / "watch_voice_relay.py").read_text(encoding="utf-8")
RELAY_TREE = ast.parse(RELAY_SOURCE)

TOKEN = "watch-voice-test-token-0123456789abcdef"     # ≥ MIN_TOKEN_LENGTH


def setUpModule() -> None:
    relay_mod._load_wire()


# ══════════════════════════════════════════════════════════════════════════════
# 独立复刻的对端。**不调用被测模块的任何解码函数** —— 自证不算证据。
# ══════════════════════════════════════════════════════════════════════════════
class WindowsUplinkGuardModel:
    """`DirectBridgeServer.cs` 里 `DirectUplinkSequenceGuard.Validate` 的手写复刻。"""

    def __init__(self) -> None:
        self.session_bytes = None
        self.frames = 0
        self.payloads: list = []
        self.sequences: list = []
        self.timestamps: list = []
        self.errors: list = []
        self._last_sequence = None
        self._last_timestamp = None

    def bind(self, session_bytes: bytes) -> None:
        self.session_bytes = session_bytes

    def feed(self, raw: bytes) -> None:
        # ⚠ 失败记进 errors 而不是抛：这个方法跑在假 Windows 的 handler 任务里，
        #   抛出去只会关掉那条连接，断言就此蒸发（诊断通道不能穿过被测对象）。
        try:
            if len(raw) != 1956:
                raise AssertionError("binary 上限就是 1956，收到 %d" % len(raw))
            if raw[0:4] != b"BWCV":
                raise AssertionError("magic 不符：%r" % raw[0:4])
            if raw[4] != 1:
                raise AssertionError("version 不符：%d" % raw[4])
            if raw[5] != 3:
                raise AssertionError("客户端 binary 只允许浏览器麦克风轨道")
            if int.from_bytes(raw[6:8], "little") != 0:
                raise AssertionError("flags 必须为 0")
            if self.session_bytes is not None and raw[8:24] != self.session_bytes:
                raise AssertionError("浏览器麦克风 binary 会话不匹配")
            sequence = int.from_bytes(raw[24:28], "little")
            timestamp = int.from_bytes(raw[28:36], "little")
            if self._last_sequence is None:
                if sequence != 0:
                    raise AssertionError("首帧序号必须是 0，收到 %d" % sequence)
            else:
                if sequence != self._last_sequence + 1:
                    raise AssertionError("序号必须恰好 +1：%d → %d"
                                         % (self._last_sequence, sequence))
                if timestamp <= self._last_timestamp:
                    raise AssertionError("时间戳必须严格递增：%d → %d"
                                         % (self._last_timestamp, timestamp))
            self._last_sequence = sequence
            self._last_timestamp = timestamp
            self.frames += 1
            self.sequences.append(sequence)
            self.timestamps.append(timestamp)
            self.payloads.append(raw[36:])
        except AssertionError as error:
            self.errors.append(str(error))


def decode_session_bytes(session_id: str) -> bytes:
    """手写的 base64url 解码，同样不借被测模块。"""
    import base64
    body = session_id.split("-", 1)[1]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def _enclosing_functions(tree):
    mapping = {}

    def walk(node, current):
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            mapping[child] = name
            walk(child, name)

    walk(tree, "<module>")
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# 铁律二：结构性断言（不启动任何 socket）
# ══════════════════════════════════════════════════════════════════════════════
class ZeroForwardingStructureTest(unittest.TestCase):
    """铁律二在**类型和结构**上成立，而不是靠一张会漏的白名单。"""

    def test_only_two_functions_can_put_bytes_on_the_windows_socket(self):
        # 这是整条链路的安全边界：Windows 那头不会再校验第二次。要新增一个发送点，
        # 得先让这条断言红 —— 而不是「顺手在某处 send 一下」。
        owners = _enclosing_functions(RELAY_TREE)
        senders = set()
        for node in ast.walk(RELAY_TREE):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "send":
                continue
            target = func.value
            if (isinstance(target, ast.Attribute) and target.attr == "_ws"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                senders.add(owners.get(node, "<module>"))
        self.assertEqual(senders, {"_send_json", "send_pcm"},
                         "发往 Windows 的发送点变了：%s" % sorted(senders))

    def test_watch_command_parser_returns_an_enum_not_data(self):
        # 枚举成员装不下数据 —— 这就是「把手表 JSON 塞给 Windows」在类型上不存在的原因。
        for op in ("start", "stop", "ping"):
            parsed = relay_mod._parse_watch_command('{"op":"%s"}' % op)
            self.assertIsInstance(parsed, relay_mod.WatchCommand)

    def test_every_dangerous_action_on_this_socket_is_unreachable(self):
        # 这份清单逐条抄自 DirectBridgeProtocol.cs 的 dispatch（`case "…"`）与 anki
        # 子动作，都是这条 WS 上真实存在、且危险的能力。
        dangerous = (
            "codex-voice-set", "codex-voice-keepalive-set", "dictionary-lookup",
            "anki-add-cards-local", "anki-card-operation-local", "context-mode",
            "context-mode-set", "service-mode-set", "context-open", "context",
            "context-clear", "active-reading", "log", "status", "hello",
            "delete-notes", "update-note-fields", "answer-cards", "sync",
        )
        for action in dangerous:
            with self.subTest(action=action):
                with self.assertRaises(relay_mod.WatchProtocolError):
                    relay_mod._parse_watch_command('{"op":"%s"}' % action)

    def test_parser_rejects_anything_carrying_a_payload(self):
        for raw in ('{"op":"start","extra":1}',
                    '{"op":"start","payload":{"anki":"delete-notes"}}',
                    '{"op":"start","requestId":"r"}',
                    '{"op":"ping","contract":"reader-computer-voice-direct/1"}',
                    '{}', '[]', '["start"]', '"start"', 'null', '{"op":1}',
                    '{"OP":"start"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(relay_mod.WatchProtocolError):
                    relay_mod._parse_watch_command(raw)

    def test_oversized_control_frame_is_refused_before_json_parsing(self):
        with self.assertRaises(relay_mod.WatchProtocolError) as caught:
            relay_mod._parse_watch_command('{"op":"' + "a" * 4096 + '"}')
        self.assertEqual(caught.exception.code, "BW_WATCH_VOICE_MESSAGE_TOO_LARGE")

    def test_verify_isolation_is_the_startup_gate(self):
        relay_mod._verify_isolation()                 # 不抛即通过

    def test_send_json_refuses_an_action_outside_the_key_table(self):
        leg = relay_mod._WindowsLeg.__new__(relay_mod._WindowsLeg)
        leg._ws = object()
        with self.assertRaises(AssertionError):
            asyncio.run(leg._send_json({"op": "start"}, "anki-card-operation-local"))
        with self.assertRaises(AssertionError):
            # 键集合多一个就拒 —— 对应 C# 侧的 RequireExactKeys。
            asyncio.run(leg._send_json(
                {"contract": relay_mod.CONTRACT, "type": "hello", "requestId": "r",
                 "protocolVersion": 3, "takeover": True}, "hello"))

    def test_windows_endpoint_is_a_constant_with_no_cli_override(self):
        # 可配置的目标地址 = 一个能被改指向的攻击面。这条断言防的是「为了测试方便
        # 加个 --windows-endpoint」，那个口子一开就再也关不上了。
        parser = relay_mod.build_parser()
        options = {action.dest for action in parser._actions}
        self.assertNotIn("windows_endpoint", options)
        self.assertNotIn("windows_origin", options)


class WireInterfaceTest(unittest.TestCase):
    """接口一致：relay 用到的每一个 `wire.X` 都真的存在，且都进了启动闸的必需表。"""

    def _used_symbols(self):
        # ⚠ 走 AST 而不是正则扫文本：注释和 docstring 里满是 `wire.X` 这种举例，正则
        #   会把它们当成真实用法，于是这条断言变成「文档里提过什么」而不是「代码用了
        #   什么」—— 一条永远绿不了、也证明不了任何事的断言。
        used = set()
        for node in ast.walk(RELAY_TREE):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("wire", "_wire")):
                used.add(node.attr)
        return used

    def test_every_wire_symbol_used_is_declared_required(self):
        missing = self._used_symbols() - set(relay_mod.REQUIRED_WIRE_SYMBOLS)
        self.assertEqual(missing, set(),
                         "relay 用了但没进 REQUIRED_WIRE_SYMBOLS：%s" % sorted(missing))

    def test_every_required_symbol_exists_in_wire(self):
        absent = [name for name in relay_mod.REQUIRED_WIRE_SYMBOLS
                  if not hasattr(wire_mod, name)]
        self.assertEqual(absent, [], "wire 里没有：%s" % absent)

    def test_the_required_table_has_no_dead_entries(self):
        # 表里留着没人用的名字，下一个人就分不清「必需」和「曾经必需」。
        unused = set(relay_mod.REQUIRED_WIRE_SYMBOLS) - self._used_symbols()
        self.assertEqual(unused, set(),
                         "REQUIRED_WIRE_SYMBOLS 里的死条目：%s" % sorted(unused))

    def test_pace_constant_matches_the_wire_frame_duration(self):
        self.assertEqual(int(relay_mod.FRAME_PERIOD_SECONDS * 1_000_000),
                         wire_mod.FRAME_DURATION_US)

    def test_watch_receive_limit_admits_exactly_one_frame(self):
        self.assertGreater(relay_mod.WATCH_MAX_MESSAGE_BYTES, wire_mod.PCM_FRAME_BYTES)

    def test_relay_uses_the_wire_status_constants_not_string_literals(self):
        self.assertIn("wire.MALFORMED", RELAY_SOURCE)
        self.assertIn("wire.FOREIGN", RELAY_SOURCE)

    def test_call_uses_the_uplink_surface_wire_actually_exposes(self):
        uplink = wire_mod.WatchVoiceUplink(wire_mod.new_session_id())
        for name in ("tick", "on_watch_frame", "rebind_watch", "stats", "session_id"):
            self.assertTrue(hasattr(uplink, name), name)


# ══════════════════════════════════════════════════════════════════════════════
# 端到端：真 asyncio、真 WebSocket、真节拍器
# ══════════════════════════════════════════════════════════════════════════════
class FakeWindowsBridge:
    """假的 Windows 桥：回执照 C# 的真实形状拼，并记下收到的每一条消息。"""

    def __init__(self) -> None:
        self.texts: list = []
        self.raw_texts: list = []
        self.frames: list = []
        self.guard = WindowsUplinkGuardModel()
        self.connections = 0
        self.session_id = None
        self.heartbeats: list = []
        self.start_delay = 0.0                        # 让 START 停在半途，好制造竞态
        # 卡在**握手响应之前**：TCP 已接受，客户端的 connect() 还没返回。这正是
        # `_WindowsLeg.close()` 够不到的那个窗口（此时 _ws 还是 None、_pending 还空）。
        self.handshake_delay = 0.0
        self.stopped = asyncio.Event()
        self._server = None
        self.port = 0

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle, "127.0.0.1", 0, process_request=self._gate)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _gate(self, connection, request):
        if self.handshake_delay:
            await asyncio.sleep(self.handshake_delay)
        return None

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection) -> None:
        self.connections += 1
        try:
            async for message in connection:
                if isinstance(message, (bytes, bytearray)):
                    raw = bytes(message)
                    self.frames.append(raw)
                    self.guard.feed(raw)
                    continue
                self.raw_texts.append(message)
                parsed = json.loads(message)
                self.texts.append(parsed)
                if parsed.get("type") == "start" and self.start_delay:
                    await asyncio.sleep(self.start_delay)
                await connection.send(json.dumps(self._reply(parsed)))
        except ConnectionClosed:
            pass

    def _reply(self, message: dict) -> dict:
        action = message.get("type")
        request_id = message.get("requestId")
        session_id = message.get("sessionId")
        if action == "hello":
            payload = {"protocolVersion": 3,
                       "limits": {"maxMessageBytes": 262144,
                                  "pcmFrameBytes": wire_mod.PCM_FRAME_BYTES,
                                  "uplinkTrack": 3, "heartbeatIntervalMs": 5000,
                                  "heartbeatTimeoutMs": 15000}}
        elif action == "start":
            self.session_id = session_id
            self.guard.bind(decode_session_bytes(session_id))
            payload = {"sessionId": session_id, "state": "active",
                       "media": {"hostReady": True, "captureActive": True}}
        elif action == "heartbeat":
            self.heartbeats.append(message.get("sequence"))
            payload = {"sessionId": session_id, "sequence": message.get("sequence"),
                       "state": "active"}
        elif action == "stop":
            self.stopped.set()
            payload = {"sessionId": session_id, "state": "idle"}
        else:
            return {"contract": relay_mod.CONTRACT, "type": "result",
                    "requestId": request_id, "action": action, "ok": False,
                    "error": {"code": "BW_TEST_UNEXPECTED_ACTION",
                              "message": "假 Windows 桥不认识 %r" % action}}
        return {"contract": relay_mod.CONTRACT, "type": "result",
                "requestId": request_id, "action": action, "ok": True,
                "payload": payload}


class Harness:
    """一整套：假 Windows + 真 Relay + 手表客户端入口。"""

    def __init__(self, tmp_dir: Path) -> None:
        self.windows = FakeWindowsBridge()
        self.relay = None
        self.logs: list = []
        self._server = None
        self._watchdog = None
        self._saved: dict = {}
        self.kill_file = tmp_dir / "watch-voice.disabled"
        self.port = 0

    async def start(self, *, heartbeat_interval=None, grace_seconds=45.0) -> None:
        await self.windows.start()
        self._saved["endpoint"] = relay_mod.WINDOWS_ENDPOINT
        self._saved["log"] = relay_mod._log
        # ⚠ 只有测试才动这个常量。产品侧刻意没有任何开关能改它（见 relay 顶部）。
        relay_mod.WINDOWS_ENDPOINT = (
            "ws://127.0.0.1:%d/reader-computer-voice/v1" % self.windows.port)
        if heartbeat_interval is not None:
            self._saved["heartbeat"] = relay_mod.HEARTBEAT_INTERVAL_SECONDS
            relay_mod.HEARTBEAT_INTERVAL_SECONDS = heartbeat_interval

        def record(name, /, **fields):
            self.logs.append(dict(fields, ev=name))

        relay_mod._log = record                       # 顺便让测试输出不被日志淹掉
        self.relay = relay_mod.Relay(TOKEN, self.kill_file,
                                     grace_seconds=grace_seconds)
        self._watchdog = asyncio.create_task(self.relay.watchdog_loop())
        self._server = await websockets.serve(
            relay_mod.make_watch_handler(self.relay), "127.0.0.1", 0,
            max_size=relay_mod.WATCH_MAX_MESSAGE_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.relay is not None:
            await self.relay.tear_down("BW_TEST_TEARDOWN", "测试结束", graceful=False)
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.windows.close()
        if "endpoint" in self._saved:
            relay_mod.WINDOWS_ENDPOINT = self._saved["endpoint"]
            relay_mod._log = self._saved["log"]
        if "heartbeat" in self._saved:
            relay_mod.HEARTBEAT_INTERVAL_SECONDS = self._saved["heartbeat"]

    def connect_watch(self, token: str = TOKEN):
        return websockets.connect(
            "ws://127.0.0.1:%d/watch-voice" % self.port,
            additional_headers={"Authorization": "Bearer " + token},
            max_size=relay_mod.WATCH_MAX_MESSAGE_BYTES)

    def events(self, name: str) -> list:
        return [record for record in self.logs if record["ev"] == name]


async def watch_event(connection, predicate, timeout: float = 10.0) -> dict:
    """读到第一条满足条件的手表事件；二进制下行直接跳过。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("等手表事件超时")
        message = await asyncio.wait_for(connection.recv(), timeout=remaining)
        if isinstance(message, (bytes, bytearray)):
            continue
        parsed = json.loads(message)
        if predicate(parsed):
            return parsed


async def wait_active(connection) -> dict:
    return await watch_event(
        connection,
        lambda ev: ev.get("ev") == "state" and ev.get("state") == "active")


async def wait_for(predicate, timeout: float = 8.0, what: str = "条件") -> None:
    """等一个条件成立。

    ⚠ 别拿「假 Windows 收到了 stop」当「Pi 已经回到 idle」：前者只是 tear_down 中间
    的一步，后者还要等回执校验、关连接、重新拿锁。写成直接断言就是个稳定复现的竞态，
    而且失败信息会指向状态机，看上去像产品的问题。
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等 %s 超时" % what)
        await asyncio.sleep(0.02)


async def expect_closed(connection, timeout: float = 8.0) -> None:
    """等这条手表连接被服务端关掉（中途的下行/事件一律丢弃）。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("连接没有被关闭")
        try:
            await asyncio.wait_for(connection.recv(), timeout=remaining)
        except ConnectionClosed:
            return


class RelayEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self._tmp.name))

    async def asyncTearDown(self) -> None:
        await self.harness.close()
        self._tmp.cleanup()

    # ── 铁律一 ───────────────────────────────────────────────────────────
    async def test_uplink_runs_at_50hz_and_stays_continuous_with_no_watch_audio(self):
        """手表一帧音频都不发，上行照样连续 —— 这就是减震器的全部意义。"""
        await self.harness.start()
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            started = time.monotonic()
            base = len(self.harness.windows.frames)
            await asyncio.sleep(1.2)
            elapsed = time.monotonic() - started
            produced = len(self.harness.windows.frames) - base

        guard = self.harness.windows.guard
        self.assertEqual(guard.errors, [], "对端解码器判违约：%s" % guard.errors[:3])
        self.assertGreater(guard.frames, 40)
        # 序号必须是 0,1,2,… 一个不缺（对端 fail-closed，缺一个就是挂断整通电话）。
        self.assertEqual(guard.sequences, list(range(guard.frames)))
        self.assertEqual(guard.timestamps,
                         [guard.timestamps[0] + i * wire_mod.FRAME_DURATION_US
                          for i in range(guard.frames)])
        # 没有手表音频时填的必须是真静音。
        self.assertTrue(all(p == wire_mod.SILENCE_PAYLOAD for p in guard.payloads))
        rate = produced / elapsed
        # ⚠ 这条抓的是 `sleep(0.02)` 累加式写法：那样会漂到 ~1/3（≈17 Hz）。
        self.assertGreater(rate, 38, "上行只有 %.1f Hz，节拍器在漂" % rate)
        self.assertLess(rate, 62, "上行到了 %.1f Hz，节拍器在跑快" % rate)

    async def test_uplink_survives_a_watch_disconnect_mid_call(self):
        """手表整条连接断掉，Windows 那侧一帧不缺 —— 严格的一侧永不断。"""
        await self.harness.start(grace_seconds=30.0)
        async with self.harness.connect_watch() as watch:
            hello = await watch_event(watch, lambda ev: ev.get("ev") == "hello")
            stream_id = hello["streamId"]
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            for sequence in range(5):
                await watch.send(wire_mod.encode_watch_frame(
                    stream_id, sequence=sequence,
                    timestamp_us=sequence * wire_mod.FRAME_DURATION_US,
                    payload=bytes([0x11, 0x00]) * wire_mod.SAMPLES_PER_FRAME))
            await asyncio.sleep(0.3)
        # 手表**走了**，通话不挂：宽限期内继续按 50 Hz 推。
        before = len(self.harness.windows.frames)
        await asyncio.sleep(0.6)
        after = len(self.harness.windows.frames)
        self.assertGreater(after - before, 15, "手表断开后上行停了")
        guard = self.harness.windows.guard
        self.assertEqual(guard.errors, [])
        self.assertEqual(guard.sequences, list(range(guard.frames)))
        self.assertEqual(self.harness.relay.state, relay_mod.CallState.ACTIVE)

    async def test_watch_audio_crosses_as_payload_only(self):
        """手表帧的 seq/ts/session 全部过不了河，只有 1920 字节载荷过。"""
        await self.harness.start()
        marker = bytes([0x37, 0x00]) * wire_mod.SAMPLES_PER_FRAME
        async with self.harness.connect_watch() as watch:
            hello = await watch_event(watch, lambda ev: ev.get("ev") == "hello")
            stream_id = hello["streamId"]
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            for index in range(6):                    # prime_frames=3，多喂几帧
                await watch.send(wire_mod.encode_watch_frame(
                    stream_id,
                    sequence=4_000_000_000 + index,   # 荒唐的序号
                    timestamp_us=99_999_999_999_999 + index,  # 荒唐的时间戳
                    payload=marker))
            await asyncio.sleep(0.4)

        guard = self.harness.windows.guard
        self.assertEqual(guard.errors, [])
        self.assertNotIn(4_000_000_000, guard.sequences, "手表的序号过了河")
        self.assertNotIn(99_999_999_999_999, guard.timestamps, "手表的时间戳过了河")
        self.assertEqual(guard.sequences, list(range(guard.frames)))
        # 载荷确实到了对端（淡入只改开头 96 个采样，尾部保持原样）。
        self.assertTrue(any(p.endswith(marker[-64:]) for p in guard.payloads),
                        "手表的音频载荷没过河")
        # 而**原始手表帧**一个字节都不该出现在发往 Windows 的流量里。
        self.assertNotIn(wire_mod.WATCH_MAGIC, b"".join(self.harness.windows.frames))

    # ── 铁律二 ───────────────────────────────────────────────────────────
    async def test_dangerous_ops_never_produce_any_windows_traffic(self):
        """手表拼命发危险 op；Windows 那侧只看得到 Pi 自己拼的四种消息。"""
        await self.harness.start(heartbeat_interval=0.15)
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            for raw in ('{"op":"anki-card-operation-local"}',
                        '{"op":"codex-voice-set"}',
                        '{"op":"service-mode-set"}',
                        '{"op":"start","takeover":true}',
                        '{"op":"start","appKind":"codex-desktop"}',
                        '{"contract":"reader-computer-voice-direct/1",'
                        '"type":"stop","requestId":"r","sessionId":"x"}'):
                await watch.send(raw)
            await asyncio.sleep(0.5)
            await watch.send('{"op":"stop"}')
            await asyncio.wait_for(self.harness.windows.stopped.wait(), timeout=5)

        seen = self.harness.windows.texts
        kinds = [message["type"] for message in seen]
        self.assertEqual(set(kinds), {"hello", "start", "heartbeat", "stop"},
                         "Windows 收到了计划外的消息类型：%s" % sorted(set(kinds)))
        for message in seen:
            expected = relay_mod.WINDOWS_MESSAGE_KEYS[message["type"]]
            self.assertEqual(set(message), expected)
            self.assertEqual(message["contract"], relay_mod.CONTRACT)
        # 手表送来的每一个字面量都不该在 Windows 流量里出现过。
        blob = "".join(self.harness.windows.raw_texts)
        for needle in ("anki", "codex-voice", "service-mode", "takeover", "appKind"):
            self.assertNotIn(needle, blob, "手表的 %r 漏过去了" % needle)
        self.assertGreaterEqual(len(self.harness.windows.heartbeats), 1)
        self.assertEqual(self.harness.windows.heartbeats,
                         list(range(1, len(self.harness.windows.heartbeats) + 1)),
                         "心跳序号必须从 1 开始且恰好 +1")

    async def test_protocol_violations_are_answered_and_then_capped(self):
        await self.harness.start()
        async with self.harness.connect_watch() as watch:
            await watch_event(watch, lambda ev: ev.get("ev") == "hello")
            await watch.send('{"op":"anki-card-operation-local"}')
            error = await watch_event(watch, lambda ev: ev.get("ev") == "error")
            self.assertEqual(error["code"], "BW_WATCH_VOICE_OP_UNKNOWN")
            for _ in range(relay_mod.WATCH_BAD_MESSAGE_LIMIT):
                await watch.send('{"op":"delete-notes"}')
            await expect_closed(watch)
        self.assertEqual(self.harness.windows.connections, 0,
                         "协议违规居然碰到了 Windows 那条腿")

    async def test_unauthorized_watch_never_reaches_the_windows_leg(self):
        await self.harness.start()
        async with self.harness.connect_watch(token="x" * 40) as watch:
            error = await watch_event(watch, lambda ev: ev.get("ev") == "error")
            self.assertEqual(error["code"], "BW_WATCH_VOICE_AUTH_INVALID")
            await expect_closed(watch)
        self.assertEqual(self.harness.windows.connections, 0)
        # 只记指纹与长度，绝不记 token 本身。
        mismatch = self.harness.events("auth.mismatch")
        self.assertEqual(len(mismatch), 1)
        self.assertNotIn("x" * 40, json.dumps(mismatch[0]))

    # ── 生命周期 / 静默失败 ───────────────────────────────────────────────
    async def test_stop_closes_the_windows_leg_gracefully(self):
        await self.harness.start()
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            await asyncio.sleep(0.2)
            await watch.send('{"op":"stop"}')
            await asyncio.wait_for(self.harness.windows.stopped.wait(), timeout=5)
            await watch_event(watch, lambda ev: ev.get("ev") == "state"
                              and ev.get("state") == "idle")
        self.assertEqual(self.harness.relay.state, relay_mod.CallState.IDLE)
        frozen = len(self.harness.windows.frames)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.harness.windows.frames), frozen,
                         "STOP 之后节拍器还在推帧")

    async def test_kill_file_tears_down_an_active_call(self):
        await self.harness.start()
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            self.harness.kill_file.write_text("stop", encoding="utf-8")
            await asyncio.wait_for(self.harness.windows.stopped.wait(), timeout=8)
            await expect_closed(watch)
        self.assertTrue(self.harness.relay.killed)
        await wait_for(lambda: self.harness.relay.state is relay_mod.CallState.IDLE,
                       what="应急闸拆除后回到 idle")

    async def test_a_second_watch_displaces_the_first_without_dropping_the_call(self):
        await self.harness.start()
        async with self.harness.connect_watch() as first:
            await watch_event(first, lambda ev: ev.get("ev") == "hello")
            await first.send('{"op":"start"}')
            await wait_active(first)
            frames_before = len(self.harness.windows.frames)
            async with self.harness.connect_watch() as second:
                second_hello = await watch_event(
                    second, lambda ev: ev.get("ev") == "hello")
                await expect_closed(first)
                await asyncio.sleep(0.3)
                self.assertGreater(len(self.harness.windows.frames),
                                   frames_before + 8, "顶替把通话打断了")
                self.assertEqual(self.harness.relay.state, relay_mod.CallState.ACTIVE)
                # 每条连接一个新 stream id：旧连接的残帧此后判 FOREIGN。
                self.assertTrue(second_hello["streamId"])
        guard = self.harness.windows.guard
        self.assertEqual(guard.errors, [])
        self.assertEqual(guard.sequences, list(range(guard.frames)))

    async def test_downlink_dropped_without_a_watch_is_counted_not_silent(self):
        """手表不在时丢下行是对的，沉默不是。"""
        await self.harness.start()
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
        await asyncio.sleep(0.1)
        relay = self.harness.relay
        self.assertIsNone(relay._watch)
        for _ in range(30):
            relay.on_downlink_payload(wire_mod.SILENCE_PAYLOAD)
        self.assertEqual(relay.downlink_no_watch, 30)
        self.assertTrue(self.harness.events("watch.downlink_discarded"))

    async def test_a_teardown_racing_an_in_flight_connect_leaves_no_orphan(self):
        """连接还没握完手就被拆掉 —— 不能留下一支谁也关不掉的活麦。

        `_command_start` 把 `self._call` 设好之后是在**锁外**跑 `call.open()`；这段时间
        里应急闸和宽限计时器都在别的任务里，随时可能 tear_down。

        ⚠ 大多数时刻这是安全的：`_WindowsLeg.close()` 会给所有在途请求塞异常，于是
        hello/start 抛错、走失败路径。**唯一够不到的窗口是 `websockets.connect()` 本身
        还没返回的时候** —— 那时 `_ws` 是 None、`_pending` 是空的，close() 是个空操作；
        随后 connect() 成功，握手和 START 一路绿灯，节拍器被建起来，而状态机里早已没有
        这通电话了。当时的代码在这里无条件写 `state = ACTIVE`，于是：`self._call` 是
        None，STOP 拆不掉它（tear_down 看到 None 直接返回），`schedule_call_failure` 也
        只打一行 `call.failure_after_close` —— 一支永远关不掉的麦克风留在用户 PC 上。

        ⚠ 这个竞态**不能**用手表发 STOP 来制造：手表的消息在读循环里是串行 await 的，
        STOP 会排在 `_command_start` 后面。能撞进来的是应急闸（独立任务）。
        """
        await self.harness.start()
        self.harness.windows.handshake_delay = 3.0    # 把 connect() 按在半空中
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await asyncio.sleep(0.05)
            self.harness.kill_file.write_text("stop", encoding="utf-8")
            await wait_for(lambda: self.harness.relay.killed,
                           timeout=6, what="应急闸生效")
            await expect_closed(watch, timeout=12)

        # 等到握手 3 s 之后：这时 open() 要么已经成功（并建好了节拍器），要么已经失败。
        # ⚠ 这里刻意**不**等某条日志再断言 —— 那样测的是「修复留下的痕迹」，
        #   而不是「有没有留下孤儿」；修复一改名，测试就跟着绿，什么也证明不了。
        await asyncio.sleep(3.4)

        # 主断言：**不再有帧流向 Windows**。孤儿的节拍器会一直推下去，
        # 因为状态机里已经没有任何东西引用得到它。
        frozen = len(self.harness.windows.frames)
        await asyncio.sleep(0.6)
        self.assertEqual(
            len(self.harness.windows.frames), frozen,
            "拆除之后还有 %d 帧被推出去：留下了一通谁也关不掉的电话"
            % (len(self.harness.windows.frames) - frozen))
        # 收尸必须是 graceful 的：STOP 真的发到了对端，而不只是本地忘掉了这通电话。
        self.assertTrue(self.harness.windows.stopped.is_set(),
                        "孤儿通话被丢下了，没有对 Windows 发 STOP")
        self.assertIsNone(self.harness.relay._call)
        self.assertIs(self.harness.relay.state, relay_mod.CallState.IDLE,
                      "状态机停在一个没有通话的非 idle 状态")

    async def test_watch_grace_expiry_completes_a_graceful_stop(self):
        """宽限到期那条路走的是 graceful STOP —— 它曾经会被自己 cancel 掉。"""
        await self.harness.start(grace_seconds=0.3)
        async with self.harness.connect_watch() as watch:
            await watch.send('{"op":"start"}')
            await wait_active(watch)
            await asyncio.sleep(0.2)
        await asyncio.wait_for(self.harness.windows.stopped.wait(), timeout=8)
        await wait_for(lambda: self.harness.relay.state is relay_mod.CallState.IDLE,
                       what="宽限拆除后回到 idle")
        # graceful=True 意味着 STOP 必须真的发出去并拿到回执，而不是被中途取消。
        self.assertTrue(self.harness.events("windows.stop_ok"),
                        "宽限到期的 STOP 没拿到回执（很可能被 cancel 截断了）")
        self.assertEqual(self.harness.events("call.stop_failed"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
