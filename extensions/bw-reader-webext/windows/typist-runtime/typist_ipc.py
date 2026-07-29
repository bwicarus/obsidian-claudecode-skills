"""voice-typist 本地 IPC client(合同 reader-voice-typist-ipc/1)。

Codex 2026-07-29 07:47 冻结的 transport:

  · C# 是 named-pipe **server**,typist 是 **client**;pipe 名固定
    `bw-reader-voice-typist-v1`(`\\\\.\\pipe\\bw-reader-voice-typist-v1`)。
  · framing 双向相同:4 字节小端 uint32 长度 + 严格 UTF-8 JSON,长度 1..65536。
  · 一条 request 对一条 response,串行单 in-flight。
  · ACK 必须精确回显 requestId/sessionId/eventId/seq;outcome 仅 accepted|duplicate。
  · typist 完成 schema + 转义校验并**持久化**三元组后就 ACK,**不等 UI 打字完成** ——
    打字是尽力而为,ACK 表示"已可靠接管",不表示"已经打出来"。

方向与常规 client 相反(request 由 server 发起),所以这里是 read → handle → write 循环。

纯逻辑(framing / ledger / handle_request)与 transport 分开,前者不依赖 pywin32,
可在任何平台测试;后者只在真连 pipe 时用。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

CONTRACT = "reader-voice-typist-ipc/1"
PIPE_NAME = "bw-reader-voice-typist-v1"
# ⚠ 不要写成 r"\\.\pipe\\" + NAME —— raw string 里的 `\\` 是**两个**字面反斜杠,
# 会拼出 \\.\pipe\\name(5 个),CreateFile 打不开。前缀用 raw、分隔符单独给。
PIPE_PATH = r"\\.\pipe" + "\\" + PIPE_NAME
MAX_FRAME = 65536
MIN_FRAME = 1
CONNECT_TIMEOUT_S = 3.0          # 合同建议值;超时回 retryable,浏览器保留游标重试


class FramingError(ValueError):
    """长度前缀或 UTF-8/JSON 解码不合法。连接必须就此中止,不许猜。"""


class ProtocolError(ValueError):
    """帧本身合法,但内容不符合合同。回结构化 error,不断连接。"""


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
    if not head or len(head) < 4:
        return None                          # 干净的 EOF:server 关闭了本次 lease
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
        self._seen: dict[str, set] = {}
        self._cursor: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return                            # 账本损坏:当作空账本,宁可重放也不静默错认
        for sid, rec in (data.get("sessions") or {}).items():
            self._seen[sid] = set(rec.get("seen") or [])
            self._cursor[sid] = int(rec.get("cursor") or 0)

    def _persist(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"contract": CONTRACT, "sessions": {
            sid: {"seen": sorted(self._seen.get(sid, ())),
                  "cursor": self._cursor.get(sid, 0)}
            for sid in self._seen}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)                # 原子:半截账本比没有账本更危险

    def cursor(self, session_id: str) -> int:
        return self._cursor.get(session_id, 0)

    def record(self, session_id: str, event_id: str, seq: int) -> str:
        """返回 'accepted' 或 'duplicate';seq 倒退抛 ProtocolError。"""
        seen = self._seen.setdefault(session_id, set())
        if event_id in seen:
            return "duplicate"                # 幂等:重发同一条不推进游标、不重复打字
        cur = self._cursor.get(session_id, 0)
        if seq < cur:
            raise ProtocolError(f"seq {seq} 倒退(当前游标 {cur});seq 可跳不可倒")
        seen.add(event_id)
        self._cursor[session_id] = seq
        self._persist()                       # 先落盘,再由调用方 ACK
        return "accepted"


# ── request 处理(纯函数,便于逐字段测试)──────────────────────────────────
def _need(obj: dict, key: str, kind, label: str):
    v = obj.get(key)
    if not isinstance(v, kind) or isinstance(v, bool) or (kind is str and not v):
        raise ProtocolError(f"{label} 缺失或类型错误:{key}")
    return v


def handle_request(req, ledger: EventLedger, *, validate_text=None) -> dict:
    """校验一条 context request 并返回 response。

    `validate_text` 可传 unescape_marks:正文转义不合法时 fail closed,
    绝不把坏正文 ACK 掉 —— ACK 之后 server 就推进游标,再也送不回来了。
    """
    if not isinstance(req, dict):
        return _err("BW_TYPIST_IPC_SCHEMA", "request 必须是对象", False, None)
    request_id = req.get("requestId")
    try:
        if req.get("contract") != CONTRACT:
            raise ProtocolError(f"contract 必须是 {CONTRACT}")
        request_id = _need(req, "requestId", str, "request")
        session_id = _need(req, "sessionId", str, "request")
        if req.get("action") != "context":
            raise ProtocolError("action 只支持 context")
        event = _need(req, "event", dict, "request")
        event_id = _need(event, "id", str, "event")
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ProtocolError("event.seq 必须是非负整数")
        if validate_text is not None:
            for field in ("text", "page_context"):
                val = event.get(field)
                if isinstance(val, dict):
                    val = val.get("text")
                if isinstance(val, str) and val:
                    validate_text(val)        # 坏转义 → 抛错 → 下面回 error
        outcome = ledger.record(session_id, event_id, seq)
    except ProtocolError as ex:
        return _err("BW_TYPIST_IPC_PROTOCOL", str(ex), False, request_id)
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


# ── transport(仅 Windows;失败不结束音频,只回 retryable)────────────────────
def connect_pipe(timeout_s: float = CONNECT_TIMEOUT_S):
    """连接 C# 的 named pipe。连不上抛 OSError —— 调用方据此回 retryable。"""
    import time
    import win32file                          # noqa: F401 - 仅 Windows
    import pywintypes
    deadline = time.time() + max(0.0, timeout_s)
    last = None
    while True:
        try:
            return win32file.CreateFile(
                PIPE_PATH,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
        except pywintypes.error as ex:
            last = ex
            if time.time() >= deadline:
                raise OSError(f"连接 {PIPE_PATH} 超时:{ex}") from ex
            time.sleep(0.1)
        if last is None:
            break


def serve(handle, ledger: EventLedger, *, on_event=None, validate_text=None) -> None:
    """读一条 → 处理 → 回一条,串行。对端关闭即正常返回。"""
    import win32file

    def _read_exact(n: int):
        buf = b""
        while len(buf) < n:
            _, chunk = win32file.ReadFile(handle, n - len(buf))
            if not chunk:
                return None if not buf else buf
            buf += chunk
        return buf

    while True:
        req = read_frame(_read_exact)
        if req is None:
            return
        resp = handle_request(req, ledger, validate_text=validate_text)
        win32file.WriteFile(handle, encode_frame(resp))
        if on_event and resp.get("ok") and resp["payload"]["outcome"] == "accepted":
            try:
                on_event(req.get("event") or {})   # 打字是 ACK 之后的尽力而为
            except Exception:
                pass
