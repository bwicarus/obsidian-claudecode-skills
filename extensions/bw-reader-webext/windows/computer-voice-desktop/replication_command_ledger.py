"""两节点复制的命令信封（权威定义）+ Windows 侧命令账本。

规格：references/reader-two-node-replication.md §2/§8/§9；
约束依据：references/book-identity-investigation-20260824.md 方向四。

信封 ``replication-command/1``（一帧一命令，两个方向共用同一形状）::

    {
      "contract": "replication-command/1",
      "deviceId": "(native-app|pwa-install|server-node)-v1-<32hex>",
      "replicationBookId": "repbook-<32hex>",   # 前提 A 的跨设备书身份
      "actor": "user" | "ai" | "system",        # 冲突规则（user 永远赢 ai）的依据
      "op": {                                   # command-outbox/2 的 op 原形
        "mutationId": "mut-v2-<32hex>",
        "url": "/pdf/api/…",
        "method": "POST" | "PATCH" | "DELETE",
        "body": { … }
      }
    }

设计要点（每条都来自已拍板规格或既有账本的实测约束，不要在实现里放宽）：

- **cursor 不在信封里**：两套既有账本（C# ReaderRealtimeOutputOutbox、
  Pi reader_sync_relay）的游标都是接收端落账时分配的。本账本照 relay 的
  SQLite 模式落账并分配游标。
- **deviceId 用设备族格式，不用 sourceInstanceId**（后者属于单个 WebView
  生命周期，outbox 刻意不持久化）。``server-node`` 是服务端**角色**的设备族
  —— 规格 §2.3：服务端角色不绑机器，今天在 Windows、将来可以是 Mac mini，
  所以刻意不叫 windows。
- **幂等按 mutationId + payload 摘要**（照 relay 的 relay_mutations）：
  同 id 同内容 = 重放（返回原 cursor，不再入账）；同 id 不同内容 =
  出声冲突，绝不静默覆盖。
- **账本行就是活动账本的原始记录**（规格 §8：同步操作本身就是记录）。
  ⚠ AI 只读处理过的派生层，永远不要把这张表直接喂给模型。
- **信封尺寸 ≤ 200 KiB**（Direct 桥单帧硬上限 256 KiB，给传输包裹留余量）。
- url 只做形状校验（路径形、无 ``..``、无控制字符）；**端点白名单由执行端把守**
  （App 侧 NATIVE_SYNC_BATCH_ENDPOINTS / 服务端重放闸），账本是存储不是执行者，
  在这里再抄一份白名单只会多一份要同步的副本。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable


REPLICATION_COMMAND_CONTRACT = "replication-command/1"
REPLICATION_COMMAND_LEDGER_FILE_NAME = "replication-command-ledger.sqlite3"

# Direct 桥单帧 256 KiB 双端硬校验；给 {contract,type,requestId,…} 包裹留余量。
MAX_ENVELOPE_BYTES = 200 * 1024
MAX_PULL_LIMIT = 100

_DEVICE_ID_RE = re.compile(
    r"^(?:native-app|pwa-install|server-node)-v1-[a-f0-9]{32}$"
)
_REPLICATION_BOOK_ID_RE = re.compile(r"^repbook-[a-f0-9]{32}$")
_MUTATION_ID_RE = re.compile(r"^mut-v2-[a-f0-9]{32}$")
_ACTORS = frozenset(("user", "ai", "system"))
_METHODS = frozenset(("POST", "PATCH", "DELETE"))

_ENVELOPE_KEYS = frozenset(("contract", "deviceId", "replicationBookId", "actor", "op"))
_OP_KEYS = frozenset(("mutationId", "url", "method", "body"))


class ReplicationCommandError(RuntimeError):
    pass


class ReplicationCommandConflict(ReplicationCommandError):
    """同 mutationId 不同内容 —— 必须出声，绝不静默覆盖。"""


def _valid_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if not value.startswith("/") or value.startswith("//"):
        return False
    if ".." in value or "#" in value or "\x00" in value:
        return False
    return all(0x20 < ord(ch) < 0x7F for ch in value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def validate_command_envelope(value: object) -> dict[str, Any]:
    """校验并返回规范化信封。任何不符即抛错 —— 存进账本的必须全部合法。"""
    if not isinstance(value, dict) or set(value.keys()) != _ENVELOPE_KEYS:
        raise ReplicationCommandError("信封字段不符合 replication-command/1")
    if value["contract"] != REPLICATION_COMMAND_CONTRACT:
        raise ReplicationCommandError("信封 contract 不符")
    device_id = value["deviceId"]
    book_id = value["replicationBookId"]
    actor = value["actor"]
    op = value["op"]
    if not isinstance(device_id, str) or not _DEVICE_ID_RE.fullmatch(device_id):
        raise ReplicationCommandError("deviceId 必须是设备族格式（不要用 sourceInstanceId）")
    if not isinstance(book_id, str) or not _REPLICATION_BOOK_ID_RE.fullmatch(book_id):
        raise ReplicationCommandError("replicationBookId 形状非法")
    if actor not in _ACTORS:
        raise ReplicationCommandError("actor 必须是 user/ai/system")
    if not isinstance(op, dict) or set(op.keys()) != _OP_KEYS:
        raise ReplicationCommandError("op 字段不符合 command-outbox/2 op 原形")
    mutation_id = op["mutationId"]
    if not isinstance(mutation_id, str) or not _MUTATION_ID_RE.fullmatch(mutation_id):
        raise ReplicationCommandError("mutationId 形状非法")
    if not _valid_url(op["url"]):
        raise ReplicationCommandError("op.url 必须是无 .. 的站内路径")
    if op["method"] not in _METHODS:
        raise ReplicationCommandError("op.method 必须是 POST/PATCH/DELETE")
    if not isinstance(op["body"], dict):
        raise ReplicationCommandError("op.body 必须是 JSON 对象")
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as error:
        raise ReplicationCommandError(f"信封不是纯 JSON：{error}") from error
    if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise ReplicationCommandError(
            f"信封超过 {MAX_ENVELOPE_BYTES} 字节（Direct 桥单帧 256KiB 的余量线）"
        )
    return value


@dataclass(frozen=True)
class LedgerEntry:
    cursor: int
    mutation_id: str
    device_id: str
    replication_book_id: str
    actor: str
    url: str
    method: str
    body: dict[str, Any]
    received_at_utc_ms: int
    replayed: bool = False

    def envelope(self) -> dict[str, Any]:
        return {
            "contract": REPLICATION_COMMAND_CONTRACT,
            "deviceId": self.device_id,
            "replicationBookId": self.replication_book_id,
            "actor": self.actor,
            "op": {
                "mutationId": self.mutation_id,
                "url": self.url,
                "method": self.method,
                "body": self.body,
            },
        }


def default_ledger_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"
    return root / REPLICATION_COMMAND_LEDGER_FILE_NAME


class ReplicationCommandLedger:
    """接收端命令账本：落账即分配游标（照 reader_sync_relay 的 SQLite 模式）。

    单进程消费。这张表是活动账本的原始层 —— **永远不要直接喂给 AI**。
    """

    def __init__(
        self,
        path: Path,
        *,
        clock_utc_ms: Callable[[], int] | None = None,
    ) -> None:
        self._clock = clock_utc_ms or (lambda: int(time.time() * 1000))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
              cursor INTEGER PRIMARY KEY AUTOINCREMENT,
              mutation_id TEXT NOT NULL UNIQUE,
              payload_sha256 TEXT NOT NULL,
              device_id TEXT NOT NULL,
              replication_book_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              url TEXT NOT NULL,
              method TEXT NOT NULL,
              body_json TEXT NOT NULL,
              received_at_utc_ms INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def append(self, envelope: object) -> LedgerEntry:
        """落账。同 mutationId 同内容 = 重放（返回原行）；不同内容 = 出声冲突。"""
        value = validate_command_envelope(envelope)
        op = value["op"]
        digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT cursor, payload_sha256 FROM commands WHERE mutation_id = ?",
                (op["mutationId"],),
            ).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise ReplicationCommandConflict(
                        f"mutationId {op['mutationId']} 已用于不同内容，拒绝静默覆盖"
                    )
                return self._entry_at(row[0], replayed=True)
            now = self._clock()
            inserted = self._db.execute(
                """
                INSERT INTO commands (
                  mutation_id, payload_sha256, device_id, replication_book_id,
                  actor, url, method, body_json, received_at_utc_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op["mutationId"], digest, value["deviceId"],
                    value["replicationBookId"], value["actor"],
                    op["url"], op["method"], _canonical_json(op["body"]), now,
                ),
            )
            return self._entry_at(int(inserted.lastrowid), replayed=False)

    def head_cursor(self) -> int:
        row = self._db.execute("SELECT MAX(cursor) FROM commands").fetchone()
        return int(row[0] or 0)

    def entries_after(self, cursor: int, limit: int = MAX_PULL_LIMIT) -> list[LedgerEntry]:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ReplicationCommandError("游标非法")
        limit = max(1, min(int(limit), MAX_PULL_LIMIT))
        rows = self._db.execute(
            "SELECT cursor FROM commands WHERE cursor > ? ORDER BY cursor LIMIT ?",
            (cursor, limit),
        ).fetchall()
        return [self._entry_at(int(row[0]), replayed=False) for row in rows]

    def _entry_at(self, cursor: int, *, replayed: bool) -> LedgerEntry:
        row = self._db.execute(
            """
            SELECT cursor, mutation_id, device_id, replication_book_id, actor,
                   url, method, body_json, received_at_utc_ms
            FROM commands WHERE cursor = ?
            """,
            (cursor,),
        ).fetchone()
        if row is None:
            raise ReplicationCommandError(f"账本行 {cursor} 不存在")
        return LedgerEntry(
            cursor=int(row[0]),
            mutation_id=str(row[1]),
            device_id=str(row[2]),
            replication_book_id=str(row[3]),
            actor=str(row[4]),
            url=str(row[5]),
            method=str(row[6]),
            body=json.loads(row[7]),
            received_at_utc_ms=int(row[8]),
            replayed=replayed,
        )
