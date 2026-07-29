"""voice-typist 本地 IPC client(合同 reader-voice-typist-ipc/1)。

Codex 2026-07-29 07:47 冻结的 transport:

  · C# 是 named-pipe **server**,typist 是 **client**;pipe 名固定
    `bw-reader-voice-typist-v1`(`\\\\.\\pipe\\bw-reader-voice-typist-v1`)。
  · framing 双向相同:4 字节小端 uint32 长度 + 严格 UTF-8 JSON,长度 1..65536。
  · 一条 request 对一条 response,串行单 in-flight。
  · ACK 必须精确回显 requestId/sessionId/eventId/seq;outcome 仅 accepted|duplicate。
  · typist 完成 schema + 转义校验后，先把事件 stage 到 durable queue，再提交
    durable ledger，最后把 queue item 发布为 committed，成功后才 ACK；
    **不等 UI 打字完成**。ACK 表示"已可靠接管"，不表示"已经打出来"。
  · committed item 在送入 Windows UI 前先持久化 `delivery_started`；进程若在
    SendInput 期间退出，重启后标为 delivery-uncertain，绝不盲目重发；只有停止
    typist、人工核对 transcript 后，才能按 exact session/event/seq 解除。

方向与常规 client 相反(request 由 server 发起),所以这里是 read → handle → write 循环。

纯逻辑(framing / ledger / handle_request)与 transport 分开,前者可在任何平台测试;
transport 也只用 Python 标准库 ctypes,不依赖 pywin32。
"""
from __future__ import annotations

import json
import os
import re
import struct
import ctypes
import ctypes.wintypes as wt
from pathlib import Path

CONTRACT = "reader-voice-typist-ipc/1"
PIPE_NAME = "bw-reader-voice-typist-v1"
# ⚠ 不要写成 r"\\.\pipe\\" + NAME —— raw string 里的 `\\` 是**两个**字面反斜杠,
# 会拼出 \\.\pipe\\name(5 个),CreateFile 打不开。前缀用 raw、分隔符单独给。
PIPE_PATH = r"\\.\pipe" + "\\" + PIPE_NAME
MAX_FRAME = 65536
MIN_FRAME = 1
CONNECT_TIMEOUT_S = 3.0          # 合同建议值;超时回 retryable,浏览器保留游标重试
MARK_L = "⟦"
MARK_R = "⟧"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
EVENT_TYPES = frozenset({
    "page.context",
    "focus",
    "drawing",
    "command",
    "command-failed",
})


class FramingError(ValueError):
    """长度前缀或 UTF-8/JSON 解码不合法。连接必须就此中止,不许猜。"""


class ProtocolError(ValueError):
    """帧本身合法,但内容不符合合同。回结构化 error,不断连接。"""


class LedgerError(RuntimeError):
    """账本无法证明此前 ACK 的状态。此时必须停止而不是覆盖旧证据。"""


class MarkEscapeError(ValueError):
    """正文锚定转义损坏；不得猜测结构标记还是用户正文。"""


def unescape_marks(value: str) -> str:
    """单次扫描反转正文 token；未知 ``\\x`` 保留，悬空 ``\\`` 拒绝。"""
    source = str(value or "")
    output: list[str] = []
    index = 0
    while index < len(source):
        current = source[index]
        if current != "\\":
            output.append(current)
            index += 1
            continue
        if index + 1 >= len(source):
            raise MarkEscapeError("末尾悬空的反斜杠")
        following = source[index + 1]
        if following in ("\\", MARK_L, MARK_R):
            output.append(following)
        else:
            output.extend(("\\", following))
        index += 2
    return "".join(output)


def unescape_annotated_text(value: str) -> str:
    """只反转结构标记外的正文，保留合法 HIGHLIGHT/CARD 边界。

    先把裸 ``⟦...⟧`` 识别为受控结构，再对两侧正文 token 调
    :func:`unescape_marks`。若先把整串反转义，原文的 ``\\⟦`` 会变成裸标记，
    消费端就无法再分辨它和真正边界。
    """
    source = str(value or "")
    output: list[str] = []
    body: list[str] = []

    def flush_body() -> None:
        if body:
            output.append(unescape_marks("".join(body)))
            body.clear()

    index = 0
    while index < len(source):
        current = source[index]
        if current == "\\":
            if index + 1 >= len(source):
                raise MarkEscapeError("末尾悬空的反斜杠")
            body.extend((current, source[index + 1]))
            index += 2
            continue
        if current == MARK_R:
            raise MarkEscapeError("正文出现未配对的右边界标记")
        if current != MARK_L:
            body.append(current)
            index += 1
            continue
        flush_body()
        end = source.find(MARK_R, index + 1)
        if end < 0:
            raise MarkEscapeError("结构边界缺少右标记")
        tag = source[index + 1:end]
        valid_tag = (
            tag == "/HIGHLIGHT"
            or tag == "CARD_END"
            or tag == "HIGHLIGHT"
            or tag.startswith("HIGHLIGHT ")
            or tag == "CARD_START"
            or tag.startswith("CARD_START ")
        )
        if (
            not valid_tag
            or MARK_L in tag
            or "\\" in tag
            or "\r" in tag
            or "\n" in tag
        ):
            raise MarkEscapeError("正文含未知或损坏的结构边界")
        output.append(source[index:end + 1])
        index = end + 1
    flush_body()
    return "".join(output)


# ── framing ────────────────────────────────────────────────────────────────
def encode_frame(obj) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not (MIN_FRAME <= len(body) <= MAX_FRAME):
        raise FramingError(f"帧长 {len(body)} 越界(允许 {MIN_FRAME}..{MAX_FRAME})")
    return struct.pack("<I", len(body)) + body


def decode_frame(body: bytes):
    """严格 UTF-8 + JSON。宽松解码会让上游把坏数据当好数据用。"""
    if not (MIN_FRAME <= len(body) <= MAX_FRAME):
        raise FramingError(f"帧长 {len(body)} 越界(允许 {MIN_FRAME}..{MAX_FRAME})")
    try:
        text = body.decode("utf-8")          # 不给 errors= ,坏字节必须炸
    except UnicodeDecodeError as ex:
        raise FramingError(f"非法 UTF-8:{ex}") from ex
    try:
        return json.loads(text)
    except json.JSONDecodeError as ex:
        raise FramingError(f"非法 JSON:{ex}") from ex


def read_frame(read_exact):
    """`read_exact(n)` 必须**恰好**返回 n 字节,不足即视为对端断开。"""
    head = read_exact(4)
    if not head:
        return None                          # 干净的 EOF:server 关闭了本次 lease
    if len(head) != 4:
        raise FramingError("长度前缀不足:对端在帧头中途断开")
    (n,) = struct.unpack("<I", head)
    if not (MIN_FRAME <= n <= MAX_FRAME):
        raise FramingError(f"长度前缀 {n} 越界;连接已不可信")
    body = read_exact(n)
    if body is None or len(body) != n:
        raise FramingError("帧体不足:对端在中途断开")
    return decode_frame(body)


# ── 去重与顺序账本 ─────────────────────────────────────────────────────────
class EventLedger:
    """`(sessionId, event.id)` 去重 + `seq` 单调性。

    合同(07:37):seq **可跳不可倒**;cursor 只推进到最后一次 ACK 的 seq。
    持久化是 ACK 的前置条件 —— 先落盘再 ACK,否则 typist 崩溃重连后会把已确认的
    事件当新事件重放,而 server 已经推进了游标,那条事件就永久丢了。
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._seen: dict[str, dict[str, int]] = {}
        self._cursor: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"contract", "sessions"}
                or data.get("contract") != CONTRACT
                or not isinstance(data.get("sessions"), dict)
            ):
                raise ValueError("账本根结构无效")
            loaded_seen: dict[str, dict[str, int]] = {}
            loaded_cursor: dict[str, int] = {}
            for sid, rec in data["sessions"].items():
                if (
                    not isinstance(sid, str)
                    or not sid
                    or not isinstance(rec, dict)
                    or set(rec) != {"seen", "cursor"}
                    or not isinstance(rec.get("seen"), dict)
                    or not isinstance(rec.get("cursor"), int)
                    or isinstance(rec.get("cursor"), bool)
                    or rec["cursor"] < 0
                ):
                    raise ValueError("账本 session 结构无效")
                seen: dict[str, int] = {}
                for event_id, seq in rec["seen"].items():
                    if (
                        not isinstance(event_id, str)
                        or not event_id
                        or not isinstance(seq, int)
                        or isinstance(seq, bool)
                        or seq < 1
                    ):
                        raise ValueError("账本 event 结构无效")
                    seen[event_id] = seq
                expected_cursor = max(seen.values(), default=0)
                if rec["cursor"] != expected_cursor:
                    raise ValueError("账本 cursor 与 seen 不一致")
                loaded_seen[sid] = seen
                loaded_cursor[sid] = rec["cursor"]
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as ex:
            raise LedgerError(f"voice-typist IPC 账本损坏:{ex}") from ex
        self._seen = loaded_seen
        self._cursor = loaded_cursor

    def _persist_state(
        self,
        seen: dict[str, dict[str, int]],
        cursor: dict[str, int],
    ) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"contract": CONTRACT, "sessions": {
            sid: {"seen": dict(sorted(seen.get(sid, {}).items())),
                  "cursor": cursor.get(sid, 0)}
            for sid in sorted(seen)}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with tmp.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)        # 原子:半截账本比没有账本更危险
        except OSError as ex:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise LedgerError(f"voice-typist IPC 账本无法持久化:{ex}") from ex

    def cursor(self, session_id: str) -> int:
        return self._cursor.get(session_id, 0)

    def classify(self, session_id: str, event_id: str, seq: int) -> str:
        """不改状态地判定 new/duplicate；冲突或非前进 seq 直接拒绝。"""
        known = self._seen.get(session_id, {}).get(event_id)
        if known is not None:
            if known != seq:
                raise ProtocolError(
                    f"event.id {event_id} 已绑定 seq {known},不能改成 {seq}")
            return "duplicate"
        cur = self._cursor.get(session_id, 0)
        if seq <= cur:
            raise ProtocolError(
                f"seq {seq} 未前进(当前游标 {cur});新事件 seq 必须更大")
        return "new"

    def record(self, session_id: str, event_id: str, seq: int) -> str:
        """返回 accepted/duplicate；只有成功原子落盘后才改变内存状态。"""
        if self.classify(session_id, event_id, seq) == "duplicate":
            return "duplicate"                # 幂等:不推进游标、不重复交接
        next_seen = {
            sid: dict(events)
            for sid, events in self._seen.items()
        }
        next_cursor = dict(self._cursor)
        next_seen.setdefault(session_id, {})[event_id] = seq
        next_cursor[session_id] = seq
        self._persist_state(next_seen, next_cursor)
        self._seen = next_seen
        self._cursor = next_cursor
        return "accepted"


# ── request 处理(纯函数,便于逐字段测试)──────────────────────────────────
def _need(obj: dict, key: str, kind, label: str):
    v = obj.get(key)
    if not isinstance(v, kind) or isinstance(v, bool) or (kind is str and not v):
        raise ProtocolError(f"{label} 缺失或类型错误:{key}")
    return v


def handle_request(
    req,
    ledger: EventLedger,
    *,
    validate_text=None,
    on_event=None,
    after_record=None,
) -> dict:
    """校验一条 context request 并返回 response。

    `validate_text` 可传 unescape_marks:正文转义不合法时 fail closed,
    绝不把坏正文 ACK 掉 —— ACK 之后 server 就推进游标,再也送不回来了。
    """
    if not isinstance(req, dict):
        return _err("BW_TYPIST_IPC_SCHEMA", "request 必须是对象", False, None)
    request_id = req.get("requestId")
    try:
        if set(req) != {
            "contract",
            "requestId",
            "sessionId",
            "action",
            "event",
        }:
            raise ProtocolError("request 字段不精确")
        if req.get("contract") != CONTRACT:
            raise ProtocolError(f"contract 必须是 {CONTRACT}")
        request_id = _need(req, "requestId", str, "request")
        session_id = _need(req, "sessionId", str, "request")
        if not SAFE_ID.fullmatch(request_id) or not SAFE_ID.fullmatch(session_id):
            raise ProtocolError("requestId/sessionId 格式无效")
        if req.get("action") != "context":
            raise ProtocolError("action 只支持 context")
        event = _need(req, "event", dict, "request")
        if not {"v", "seq", "type", "ts", "id"}.issubset(event):
            raise ProtocolError("event 缺少 v/seq/type/ts/id")
        if event.get("v") != 1 or isinstance(event.get("v"), bool):
            raise ProtocolError("event.v 必须是 1")
        if event.get("type") not in EVENT_TYPES:
            raise ProtocolError("event.type 不在白名单")
        timestamp = event.get("ts")
        if (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or abs(timestamp) > MAX_SAFE_INTEGER
        ):
            raise ProtocolError("event.ts 必须是 JS 安全整数")
        event_id = _need(event, "id", str, "event")
        if not SAFE_ID.fullmatch(event_id):
            raise ProtocolError("event.id 格式无效")
        seq = event.get("seq")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or seq > MAX_SAFE_INTEGER
        ):
            raise ProtocolError("event.seq 必须是 JS 安全正整数")
        if validate_text is not None:
            for field in ("text", "page_context"):
                val = event.get(field)
                if isinstance(val, dict):
                    val = val.get("text")
                if isinstance(val, str) and val:
                    validate_text(val)        # 坏转义 → 抛错 → 下面回 error
        classification = ledger.classify(session_id, event_id, seq)
        if classification == "duplicate":
            outcome = "duplicate"
        else:
            if on_event is not None:
                try:
                    accepted = on_event(event, session_id, seq)
                except ProtocolError:
                    raise
                except Exception as ex:
                    return _err(
                        "BW_TYPIST_IPC_HANDOFF_FAILED",
                        f"{type(ex).__name__}: {ex}",
                        True,
                        request_id,
                    )
                if accepted is False:
                    return _err(
                        "BW_TYPIST_IPC_HANDOFF_FAILED",
                        "durable handoff 未接受事件",
                        True,
                        request_id,
                    )
            # Durable handoff must finish before the ledger is advanced.  If
            # ledger persistence fails, a retry re-enters the idempotent
            # handoff and still cannot be misreported as duplicate.
            outcome = ledger.record(session_id, event_id, seq)
        if after_record is not None:
            try:
                committed = after_record(
                    event,
                    session_id,
                    seq,
                    outcome,
                )
            except Exception as ex:
                return _err(
                    "BW_TYPIST_IPC_HANDOFF_FAILED",
                    f"{type(ex).__name__}: {ex}",
                    True,
                    request_id,
                )
            if committed is False:
                return _err(
                    "BW_TYPIST_IPC_HANDOFF_FAILED",
                    "durable handoff 未提交事件",
                    True,
                    request_id,
                )
    except ProtocolError as ex:
        return _err("BW_TYPIST_IPC_PROTOCOL", str(ex), False, request_id)
    except LedgerError as ex:
        return _err("BW_TYPIST_IPC_LEDGER_FAILED", str(ex), True, request_id)
    except Exception as ex:                   # 校验器(如反转义)抛的任何异常都算坏数据
        return _err("BW_TYPIST_IPC_PAYLOAD", f"{type(ex).__name__}: {ex}", False, request_id)
    # ACK 精确回显四个字段:server 靠它把 ACK 对回具体那一条,回显错了等于没 ACK。
    return {"contract": CONTRACT, "requestId": request_id, "ok": True,
            "action": "context",
            "payload": {"sessionId": session_id, "eventId": event_id,
                        "seq": seq, "outcome": outcome}}


def _err(code: str, message: str, retryable: bool, request_id) -> dict:
    return {"contract": CONTRACT, "requestId": request_id, "ok": False,
            "error": {"code": code, "message": str(message)[:300],
                      "retryable": bool(retryable)}}


# ── transport(仅 Windows;只用标准库 Win32,候选不依赖 pywin32)──────────────
class PipeHandle:
    def __init__(self, value: int):
        self.value = int(value)

    def Close(self) -> None:
        if not self.value:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        value, self.value = self.value, 0
        kernel32.CloseHandle(wt.HANDLE(value))

    def __enter__(self) -> "PipeHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.Close()


def _pipe_api():
    if os.name != "nt":
        raise OSError("voice-typist named pipe is Windows-only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        wt.DWORD,
        wt.LPVOID,
        wt.DWORD,
        wt.DWORD,
        wt.HANDLE,
    ]
    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
    kernel32.WaitNamedPipeW.restype = wt.BOOL
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
    return kernel32


def connect_pipe(timeout_s: float = CONNECT_TIMEOUT_S):
    """连接 C# 的 named pipe。连不上抛 OSError —— 调用方据此回 retryable。"""
    import time
    kernel32 = _pipe_api()
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_error = 2
    invalid = ctypes.c_void_p(-1).value
    while True:
        raw = kernel32.CreateFileW(
            PIPE_PATH,
            0x80000000 | 0x40000000,           # GENERIC_READ | GENERIC_WRITE
            0,
            None,
            3,                                 # OPEN_EXISTING
            0,
            None,
        )
        value = ctypes.cast(raw, ctypes.c_void_p).value
        if value not in (None, invalid):
            return PipeHandle(int(value))
        last_error = ctypes.get_last_error()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError(
                last_error,
                f"连接 {PIPE_PATH} 超时",
                PIPE_PATH,
            )
        if last_error not in (2, 231):         # FILE_NOT_FOUND / PIPE_BUSY
            raise OSError(last_error, f"连接 {PIPE_PATH} 失败", PIPE_PATH)
        wait_ms = max(1, min(100, int(remaining * 1000)))
        kernel32.WaitNamedPipeW(PIPE_PATH, wait_ms)
        if last_error == 2:
            time.sleep(min(0.05, remaining))


def serve(
    handle,
    ledger: EventLedger,
    *,
    on_event=None,
    after_record=None,
    validate_text=None,
) -> None:
    """读一条 → 处理 → 回一条,串行。对端关闭即正常返回。"""
    kernel32 = _pipe_api()
    raw_handle = wt.HANDLE(
        handle.value if isinstance(handle, PipeHandle) else int(handle))

    def _read_exact(n: int):
        buffer = ctypes.create_string_buffer(n)
        offset = 0
        while offset < n:
            read = wt.DWORD()
            ok = kernel32.ReadFile(
                raw_handle,
                ctypes.byref(buffer, offset),
                n - offset,
                ctypes.byref(read),
                None,
            )
            if not ok:
                error = ctypes.get_last_error()
                if error in (109, 232, 233):  # BROKEN_PIPE / NO_DATA / PIPE_NOT_CONNECTED
                    return None if offset == 0 else buffer.raw[:offset]
                raise OSError(error, "Voice Typist IPC ReadFile 失败")
            if read.value == 0:
                return None if offset == 0 else buffer.raw[:offset]
            offset += int(read.value)
        return buffer.raw

    while True:
        req = read_frame(_read_exact)
        if req is None:
            return
        resp = handle_request(
            req,
            ledger,
            validate_text=validate_text,
            on_event=on_event,
            after_record=after_record,
        )
        frame = encode_frame(resp)
        offset = 0
        while offset < len(frame):
            written = wt.DWORD()
            chunk = (ctypes.c_char * (len(frame) - offset)).from_buffer_copy(
                frame[offset:])
            ok = kernel32.WriteFile(
                raw_handle,
                chunk,
                len(chunk),
                ctypes.byref(written),
                None,
            )
            if not ok or written.value == 0:
                error = ctypes.get_last_error()
                raise OSError(error, "Voice Typist IPC WriteFile 失败")
            offset += int(written.value)
