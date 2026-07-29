"""voice-typist 本地 IPC(reader-voice-typist-ipc/1)的 framing / 账本 / ACK 合同。

对应 Codex 2026-07-29 07:47 冻结的 transport。纯逻辑测试可在任何平台跑；
Windows 另跑一次真实 named-pipe 回环，仍不需要 pywin32。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

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
    ev = {"v": 1, "id": event_id, "seq": seq, "type": "focus", "ts": 1}
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

    def test_partial_header_is_rejected(self) -> None:
        for size in (1, 2, 3):
            with self.subTest(size=size):
                with self.assertRaises(IPC.FramingError):
                    IPC.read_frame(_reader(b"\x01" * size))

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

    def test_duplicate_event_id_requires_the_original_seq(self) -> None:
        led = IPC.EventLedger(self.path)
        led.record("s1", "e1", 5)
        with self.assertRaises(IPC.ProtocolError):
            led.record("s1", "e1", 6)

    def test_seq_may_skip_but_not_go_backwards(self) -> None:
        led = IPC.EventLedger(self.path)
        led.record("s1", "e1", 5)
        self.assertEqual(led.record("s1", "e2", 9), "accepted", "seq 可跳")
        with self.assertRaises(IPC.ProtocolError):
            led.record("s1", "e3", 4)          # 倒退必须拒绝
        with self.assertRaises(IPC.ProtocolError):
            led.record("s1", "e4", 9)          # 同 seq 的另一个 event 也不是前进

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

    def test_corrupt_ledger_fails_closed(self) -> None:
        self.path.write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(IPC.LedgerError):
            IPC.EventLedger(self.path)

    def test_persist_failure_does_not_mutate_memory(self) -> None:
        led = IPC.EventLedger(self.path)
        with patch.object(
            led,
            "_persist_state",
            side_effect=IPC.LedgerError("disk failed"),
        ):
            with self.assertRaises(IPC.LedgerError):
                led.record("s1", "e1", 1)
        self.assertEqual(led.cursor("s1"), 0)
        self.assertEqual(led.classify("s1", "e1", 1), "new")

    def test_persist_flushes_before_atomic_replace(self) -> None:
        led = IPC.EventLedger(self.path)
        calls: list[int] = []
        with patch.object(IPC.os, "fsync", side_effect=lambda fd: calls.append(fd)):
            led.record("s1", "e1", 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(IPC.EventLedger(self.path).cursor("s1"), 1)


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
        for seq in (0, -1, IPC.MAX_SAFE_INTEGER + 1, "3", 1.5, True, None):
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

    def test_durable_handoff_precedes_ledger_and_ack(self) -> None:
        order: list[str] = []
        original_record = self.led.record

        def record(session_id, event_id, seq):
            order.append("ledger")
            return original_record(session_id, event_id, seq)

        with patch.object(self.led, "record", side_effect=record):
            response = IPC.handle_request(
                _req(),
                self.led,
                on_event=lambda _event, _session, _seq: (
                    order.append("handoff") or True
                ),
            )
        self.assertTrue(response["ok"], response)
        self.assertEqual(order, ["handoff", "ledger"])

    def test_handoff_failure_is_retryable_and_not_misreported_duplicate(self) -> None:
        failed = IPC.handle_request(
            _req(),
            self.led,
            on_event=lambda _event, _session, _seq: (_ for _ in ()).throw(
                OSError("queue unavailable")
            ),
        )
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["error"]["retryable"])
        self.assertEqual(
            failed["error"]["code"],
            "BW_TYPIST_IPC_HANDOFF_FAILED",
        )
        self.assertEqual(self.led.cursor("s1"), 0)
        retry = IPC.handle_request(
            _req(),
            self.led,
            on_event=lambda _event, _session, _seq: True,
        )
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["payload"]["outcome"], "accepted")

    def test_duplicate_skips_handoff_after_previous_durable_accept(self) -> None:
        calls: list[str] = []
        first = IPC.handle_request(
            _req(),
            self.led,
            on_event=lambda _event, _session, _seq: (
                calls.append("first") or True
            ),
        )
        duplicate = IPC.handle_request(
            _req(),
            self.led,
            on_event=lambda _event, _session, _seq: (
                calls.append("duplicate") or True
            ),
        )
        self.assertEqual(first["payload"]["outcome"], "accepted")
        self.assertEqual(duplicate["payload"]["outcome"], "duplicate")
        self.assertEqual(calls, ["first"])

    def test_ledger_failure_returns_retryable_without_ack(self) -> None:
        with patch.object(
            self.led,
            "record",
            side_effect=IPC.LedgerError("disk failed"),
        ):
            response = IPC.handle_request(
                _req(),
                self.led,
                on_event=lambda _event, _session, _seq: True,
            )
        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["code"],
            "BW_TYPIST_IPC_LEDGER_FAILED",
        )
        self.assertTrue(response["error"]["retryable"])


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

    def test_annotated_text_preserves_structure_and_unescapes_only_body(self) -> None:
        original = "前 ⟦ 中 \\ 后"
        escaped = OC._escape_marks(original)
        annotated = (
            f"{IPC.MARK_L}HIGHLIGHT color=\"#ffd54a\"{IPC.MARK_R}"
            f"{escaped}"
            f"{IPC.MARK_L}/HIGHLIGHT{IPC.MARK_R}"
        )
        restored = IPC.unescape_annotated_text(annotated)
        self.assertEqual(
            restored,
            f"{IPC.MARK_L}HIGHLIGHT color=\"#ffd54a\"{IPC.MARK_R}"
            f"{original}"
            f"{IPC.MARK_L}/HIGHLIGHT{IPC.MARK_R}",
        )

    def test_unknown_or_unbalanced_bare_structure_mark_fails_closed(self) -> None:
        for value in (
            f"{IPC.MARK_L}UNKNOWN{IPC.MARK_R}正文",
            f"正文{IPC.MARK_L}HIGHLIGHT",
            f"正文{IPC.MARK_R}",
        ):
            with self.subTest(value=value):
                with self.assertRaises(IPC.MarkEscapeError):
                    IPC.unescape_annotated_text(value)


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


@unittest.skipUnless(os.name == "nt", "真实 named pipe 回环只在 Windows 运行")
class WindowsNamedPipeTransportTest(unittest.TestCase):
    """真实 Win32 byte-mode pipe；不启动 C# 服务、typist UI 或任何音频。"""

    def test_server_request_client_durable_ack_round_trip(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.argtypes = [
            wt.LPCWSTR,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.LPVOID,
        ]
        kernel32.CreateNamedPipeW.restype = wt.HANDLE
        kernel32.ConnectNamedPipe.argtypes = [wt.HANDLE, wt.LPVOID]
        kernel32.ConnectNamedPipe.restype = wt.BOOL
        kernel32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
        kernel32.DisconnectNamedPipe.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.ReadFile.argtypes = [
            wt.HANDLE,
            wt.LPVOID,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
            wt.LPVOID,
        ]
        kernel32.ReadFile.restype = wt.BOOL
        kernel32.WriteFile.argtypes = [
            wt.HANDLE,
            wt.LPCVOID,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
            wt.LPVOID,
        ]
        kernel32.WriteFile.restype = wt.BOOL

        pipe_path = (
            r"\\.\pipe\bw-reader-voice-typist-test-"
            f"{os.getpid()}-{uuid.uuid4().hex}"
        )
        ready = threading.Event()
        responses: list[dict] = []
        failures: list[BaseException] = []
        request = _req(
            session="pipe-session",
            event_id="pipe-event",
            seq=7,
            request_id="pipe-request",
        )
        invalid_handle = ctypes.c_void_p(-1).value

        def require(ok: bool, operation: str) -> None:
            if not ok:
                error = ctypes.get_last_error()
                raise OSError(error, f"{operation} 失败")

        def write_all(handle, payload: bytes) -> None:
            offset = 0
            while offset < len(payload):
                chunk = (ctypes.c_char * (len(payload) - offset)).from_buffer_copy(
                    payload[offset:]
                )
                written = wt.DWORD()
                require(
                    bool(kernel32.WriteFile(
                        handle,
                        chunk,
                        len(chunk),
                        ctypes.byref(written),
                        None,
                    )),
                    "WriteFile",
                )
                if written.value == 0:
                    raise OSError("WriteFile 未写入任何字节")
                offset += int(written.value)

        def read_exact(handle, size: int) -> bytes:
            buffer = ctypes.create_string_buffer(size)
            offset = 0
            while offset < size:
                read = wt.DWORD()
                require(
                    bool(kernel32.ReadFile(
                        handle,
                        ctypes.byref(buffer, offset),
                        size - offset,
                        ctypes.byref(read),
                        None,
                    )),
                    "ReadFile",
                )
                if read.value == 0:
                    raise OSError("ReadFile 提前返回 EOF")
                offset += int(read.value)
            return buffer.raw

        def server() -> None:
            raw = kernel32.CreateNamedPipeW(
                pipe_path,
                0x00000003,       # PIPE_ACCESS_DUPLEX
                0x00000000,       # byte type + byte read mode + blocking
                1,
                IPC.MAX_FRAME + 4,
                IPC.MAX_FRAME + 4,
                5_000,
                None,
            )
            value = ctypes.cast(raw, ctypes.c_void_p).value
            if value in (None, invalid_handle):
                failures.append(
                    OSError(
                        ctypes.get_last_error(),
                        "CreateNamedPipeW 失败",
                    )
                )
                ready.set()
                return
            handle = wt.HANDLE(value)
            ready.set()
            try:
                connected = bool(kernel32.ConnectNamedPipe(handle, None))
                if not connected and ctypes.get_last_error() != 535:
                    require(False, "ConnectNamedPipe")
                write_all(handle, IPC.encode_frame(request))
                head = read_exact(handle, 4)
                (length,) = struct.unpack("<I", head)
                responses.append(
                    IPC.decode_frame(read_exact(handle, length))
                )
            except BaseException as ex:
                failures.append(ex)
            finally:
                kernel32.DisconnectNamedPipe(handle)
                kernel32.CloseHandle(handle)

        thread = threading.Thread(
            target=server,
            name="typist-ipc-test-server",
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2), "测试 pipe server 未及时创建")
        self.assertFalse(failures, failures)

        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.json"
            handed_off: list[tuple[str, str, int]] = []
            with patch.object(IPC, "PIPE_PATH", pipe_path):
                with IPC.connect_pipe(timeout_s=2) as handle:
                    IPC.serve(
                        handle,
                        IPC.EventLedger(ledger_path),
                        on_event=lambda event, session_id, seq: (
                            handed_off.append((event["id"], session_id, seq))
                            or True
                        ),
                        validate_text=IPC.unescape_annotated_text,
                    )
            self.assertEqual(
                handed_off,
                [("pipe-event", "pipe-session", 7)],
            )
            self.assertEqual(
                IPC.EventLedger(ledger_path).cursor("pipe-session"),
                7,
            )

        thread.join(2)
        self.assertFalse(thread.is_alive(), "测试 pipe server 未正常退出")
        self.assertFalse(failures, failures)
        self.assertEqual(len(responses), 1)
        self.assertEqual(
            responses[0],
            {
                "contract": IPC.CONTRACT,
                "requestId": "pipe-request",
                "ok": True,
                "action": "context",
                "payload": {
                    "sessionId": "pipe-session",
                    "eventId": "pipe-event",
                    "seq": 7,
                    "outcome": "accepted",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
