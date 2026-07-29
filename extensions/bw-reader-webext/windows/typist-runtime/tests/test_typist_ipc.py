"""voice-typist 本地 IPC(reader-voice-typist-ipc/1)的 framing / 账本 / ACK 合同。

对应 Codex 2026-07-29 07:47 冻结的 transport。纯逻辑测试,不需要 pywin32,
因此在任何平台可跑;真 pipe 部分由 C# server 就绪后做联调。
"""
from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import typist_ipc as IPC  # noqa: E402

sys.path.insert(0, str(HERE.parents[3] / "_server_deploy"))
import reader_outgoing_context as OC  # noqa: E402


def _reader(payload: bytes):
    buf = io.BytesIO(payload)

    def read_exact(n):
        got = buf.read(n)
        return got if got else None
    return read_exact


def _req(session="s1", event_id="e1", seq=1, request_id="r1", **extra):
    ev = {"id": event_id, "seq": seq}
    ev.update(extra.pop("event", {}))
    body = {"contract": IPC.CONTRACT, "requestId": request_id,
            "sessionId": session, "action": "context", "event": ev}
    body.update(extra)
    return body


class FramingTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        obj = {"contract": IPC.CONTRACT, "text": "中文与 ⟦ 标记"}
        frame = IPC.encode_frame(obj)
        (n,) = struct.unpack("<I", frame[:4])
        self.assertEqual(n, len(frame) - 4, "长度前缀必须是 4 字节小端且等于帧体长")
        self.assertEqual(IPC.read_frame(_reader(frame)), obj)

    def test_clean_eof_returns_none(self) -> None:
        self.assertIsNone(IPC.read_frame(_reader(b"")))

    def test_oversize_length_prefix_is_rejected(self) -> None:
        bad = struct.pack("<I", IPC.MAX_FRAME + 1) + b"{}"
        with self.assertRaises(IPC.FramingError):
            IPC.read_frame(_reader(bad))

    def test_zero_length_is_rejected(self) -> None:
        with self.assertRaises(IPC.FramingError):
            IPC.read_frame(_reader(struct.pack("<I", 0)))

    def test_truncated_body_is_rejected(self) -> None:
        frame = IPC.encode_frame({"a": 1})
        with self.assertRaises(IPC.FramingError):
            IPC.read_frame(_reader(frame[:-1]))

    def test_invalid_utf8_is_rejected(self) -> None:
        body = b"\xff\xfe not utf8"
        with self.assertRaises(IPC.FramingError):
            IPC.read_frame(_reader(struct.pack("<I", len(body)) + body))

    def test_invalid_json_is_rejected(self) -> None:
        body = "{不是 JSON".encode("utf-8")
        with self.assertRaises(IPC.FramingError):
            IPC.read_frame(_reader(struct.pack("<I", len(body)) + body))


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.json"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_first_event_accepted_then_duplicate(self) -> None:
        led = IPC.EventLedger(self.path)
        self.assertEqual(led.record("s1", "e1", 5), "accepted")
        self.assertEqual(led.record("s1", "e1", 5), "duplicate")

    def test_seq_may_skip_but_not_go_backwards(self) -> None:
        led = IPC.EventLedger(self.path)
        led.record("s1", "e1", 5)
        self.assertEqual(led.record("s1", "e2", 9), "accepted", "seq 可跳")
        with self.assertRaises(IPC.ProtocolError):
            led.record("s1", "e3", 4)          # 倒退必须拒绝

    def test_sessions_are_isolated(self) -> None:
        led = IPC.EventLedger(self.path)
        led.record("s1", "e1", 9)
        self.assertEqual(led.record("s2", "e1", 1), "accepted",
                         "去重键是 (sessionId,eventId),不是 eventId")

    def test_persisted_before_ack_survives_restart(self) -> None:
        """先落盘再 ACK:崩溃重连后不得把已确认的事件当新事件重放。"""
        IPC.EventLedger(self.path).record("s1", "e1", 5)
        reopened = IPC.EventLedger(self.path)
        self.assertEqual(reopened.cursor("s1"), 5)
        self.assertEqual(reopened.record("s1", "e1", 5), "duplicate")

    def test_corrupt_ledger_falls_back_to_empty(self) -> None:
        self.path.write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual(IPC.EventLedger(self.path).cursor("s1"), 0)


class AckContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.led = IPC.EventLedger(Path(self.dir.name) / "l.json")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_ack_echoes_four_fields_exactly(self) -> None:
        r = IPC.handle_request(_req("sX", "eX", 7, "rX"), self.led)
        self.assertTrue(r["ok"])
        self.assertEqual(r["requestId"], "rX", "requestId 必须精确回显")
        self.assertEqual(r["action"], "context")
        self.assertEqual(r["payload"], {"sessionId": "sX", "eventId": "eX",
                                        "seq": 7, "outcome": "accepted"})

    def test_duplicate_outcome(self) -> None:
        IPC.handle_request(_req(), self.led)
        r = IPC.handle_request(_req(), self.led)
        self.assertEqual(r["payload"]["outcome"], "duplicate")

    def test_outcome_vocabulary_is_closed(self) -> None:
        for req in (_req("s", "a", 1), _req("s", "a", 1)):
            r = IPC.handle_request(req, self.led)
            self.assertIn(r["payload"]["outcome"], ("accepted", "duplicate"))

    def test_wrong_contract_is_rejected(self) -> None:
        bad = _req()
        bad["contract"] = "reader-outgoing-context/1"
        r = IPC.handle_request(bad, self.led)
        self.assertFalse(r["ok"])
        self.assertFalse(r["error"]["retryable"])

    def test_missing_fields_are_rejected(self) -> None:
        for drop in ("requestId", "sessionId", "event"):
            req = _req()
            req.pop(drop)
            self.assertFalse(IPC.handle_request(req, self.led)["ok"], drop)

    def test_bad_seq_types_are_rejected(self) -> None:
        for seq in (-1, "3", 1.5, True, None):
            req = _req(seq=seq)
            self.assertFalse(IPC.handle_request(req, self.led)["ok"], repr(seq))

    def test_backwards_seq_returns_error_not_ack(self) -> None:
        IPC.handle_request(_req("s", "e1", 5), self.led)
        r = IPC.handle_request(_req("s", "e2", 2), self.led)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "BW_TYPIST_IPC_PROTOCOL")

    def test_rejected_request_does_not_advance_cursor(self) -> None:
        IPC.handle_request(_req("s", "e1", 5), self.led)
        IPC.handle_request(_req("s", "e2", 2), self.led)      # 被拒
        self.assertEqual(self.led.cursor("s"), 5, "被拒的请求不得推进游标")


class EscapeValidationTest(unittest.TestCase):
    """坏转义必须 fail closed —— ACK 之后 server 就推进游标,再也送不回来。"""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.led = IPC.EventLedger(Path(self.dir.name) / "l.json")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_valid_escaped_text_is_accepted(self) -> None:
        text = OC._escape_marks("正文含 ⟦ 与 ⟧ 以及反斜杠 \\")
        req = _req(event={"text": text})
        r = IPC.handle_request(req, self.led, validate_text=OC.unescape_marks)
        self.assertTrue(r["ok"], r)

    def test_dangling_backslash_is_rejected_and_not_recorded(self) -> None:
        req = _req(event={"text": "坏正文 \\"})
        r = IPC.handle_request(req, self.led, validate_text=OC.unescape_marks)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "BW_TYPIST_IPC_PAYLOAD")
        self.assertEqual(self.led.cursor("s1"), 0, "坏数据不得进账本")

    def test_page_context_text_is_also_validated(self) -> None:
        req = _req(event={"page_context": {"text": "坏 \\"}})
        r = IPC.handle_request(req, self.led, validate_text=OC.unescape_marks)
        self.assertFalse(r["ok"])


class WireCompatTest(unittest.TestCase):
    def test_response_is_serialisable_within_frame_limits(self) -> None:
        r = IPC.handle_request(_req(), IPC.EventLedger(None))
        frame = IPC.encode_frame(r)
        self.assertLessEqual(len(frame) - 4, IPC.MAX_FRAME)
        self.assertEqual(json.loads(frame[4:].decode("utf-8")), r)

    def test_pipe_name_matches_contract(self) -> None:
        self.assertEqual(IPC.PIPE_NAME, "bw-reader-voice-typist-v1")

    def test_pipe_path_is_exactly_the_win32_form(self) -> None:
        """逐字符断言,不用 endswith —— 多一个反斜杠 CreateFile 就打不开,
        而 endswith 照样通过(第一版就是这么漏掉的)。"""
        bs = chr(92)
        self.assertEqual(IPC.PIPE_PATH, bs * 2 + "." + bs + "pipe" + bs + IPC.PIPE_NAME)
        self.assertEqual(IPC.PIPE_PATH.count(bs), 4)


if __name__ == "__main__":
    unittest.main()
