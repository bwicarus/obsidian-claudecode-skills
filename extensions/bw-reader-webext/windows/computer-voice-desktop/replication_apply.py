"""两节点复制：Windows 侧数据副本 + 命令分发（apply）。

规格：references/reader-two-node-replication.md §2/§9 步骤 2 的最后一块。

链路：C# fsync 落 spool → :func:`ReplicationCommandLedger.ingest_spool_directory`
入账 → 本模块按游标顺序把命令**应用**到 Windows 数据副本。

数据副本形状（``replication-book-data/1``，一书一域一个 JSON 文件）::

    %LOCALAPPDATA%/BWReader/replication-data/<repbookId>/<domain>.json
    { "contract": "replication-book-data/1",
      "items": { "<id>": {…完整条目…} },
      "tombstones": { "<id>": { "time": <秒> } },
      "order": [ "<id>", … ] }

与 App 侧前提 B 的拆分形状同构（条目 + 墓碑 + 序），两端可对账。

设计要点：

- **执行端就是端点白名单的把守者**（账本层刻意不抄副本）：
  ``_EXECUTORS`` 就是 url→apply 的执行映射。表外端点进死信 ——
  命令仍在账本里，执行器升级后可按游标重放（redrive）。
- **apply 幂等**：POST=按 id 整条 upsert、PATCH=改字段、DELETE=写墓碑，
  三者重放无害 —— 游标推进和数据落盘之间崩了，重启重放即收敛。
  游标只在数据落盘**之后**推进。
- **POST 的 body 必须是发送端已落库的完整条目**（含 id/time），
  不是原始请求 —— 否则两端条目内容永远对不齐（对账会一直红）。
  这一条已写进规格，App 发送端（步骤 3）按此实现。
- **毒命令进死信但不堵管**：死信 jsonl 逐条出声（cursor/error 全记），
  游标照常推进；静默跳过和整管堵死都是错的。
- **诊断出口先行**（silent-failure-lessons #4）：每轮跑完写
  ``replication-apply.status.json``，无控制台的机器上也能看到
  最近一轮做了什么、死信几条、错在哪。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from replication_book_links import (
    ReplicationBookLinkStore,
    ReplicationLinkStoreError,
    default_links_path,
)
from replication_command_ledger import (
    LedgerEntry,
    ReplicationCommandError,
    ReplicationCommandLedger,
)


REPLICATION_BOOK_DATA_CONTRACT = "replication-book-data/1"
REPLICATION_DATA_DIRECTORY_NAME = "replication-data"
APPLY_STATE_FILE_NAME = "replication-apply-state.json"
APPLY_STATUS_FILE_NAME = "replication-apply.status.json"
DEAD_LETTER_FILE_NAME = "replication-dead-letter.jsonl"

# 条目 id 形状：高亮 c_/h_/e…、便签 n<hex>（App 路由 'n'+randomHex(6).slice(0,11)）。
_ITEM_ID_RE = re.compile(
    r"^(?:c_[a-f0-9]{8,32}|h_[a-f0-9]{6,32}|e[a-f0-9]{6,16}|n[a-f0-9]{6,16})$"
)

# PATCH 允许改的字段，照 App 路由的允许集（native-local-runtime.js 的
# localPDFHighlights / localEPUBHighlights / localNotes）。
# POST 是整条 upsert 不看这张表。
_PATCH_FIELDS = {
    "pdf-highlights": frozenset(("color", "text", "note", "kind", "sentence", "body")),
    "epub-highlights": frozenset(("color", "note", "kind", "sentence", "body")),
    "document-notes": frozenset((
        "anchor", "text", "color", "w", "h", "collapsed",
        "strokes", "video", "card", "html", "iar",
    )),
}

_EXECUTORS: dict[tuple[str, str], tuple[str, str]] = {
    # (url, method) -> (domain, operation)
    ("/pdf/api/highlights", "POST"): ("pdf-highlights", "upsert"),
    ("/pdf/api/highlights", "PATCH"): ("pdf-highlights", "patch"),
    ("/pdf/api/highlights", "DELETE"): ("pdf-highlights", "tombstone"),
    ("/pdf/api/epub-highlights", "POST"): ("epub-highlights", "upsert"),
    ("/pdf/api/epub-highlights", "PATCH"): ("epub-highlights", "patch"),
    ("/pdf/api/epub-highlights", "DELETE"): ("epub-highlights", "tombstone"),
    ("/pdf/api/notes", "POST"): ("document-notes", "upsert"),
    ("/pdf/api/notes", "PATCH"): ("document-notes", "patch"),
    ("/pdf/api/notes", "DELETE"): ("document-notes", "tombstone"),
}

# 配对公告（前提 A 的 App 半边）：App 铸 replicationBookId 后作为一条命令
# 发来登记。它操作链接表而不是某个域的数据文件，单列不进 _EXECUTORS。
PAIR_URL = "/replication/pair"
_PAIR_BODY_KEYS = frozenset(("peerBookId", "replicationBookId", "displayName"))

# 整域重同步（对账不一致后的显式收敛，规格 §6）：App 把该域**全部存活条目**
# 按序重发，服务端整域替换（全量 upsert + 差集写墓碑）。幂等。
RESYNC_URL = "/replication/resync"
_RESYNC_BODY_KEYS = frozenset(("domain", "items"))
_RESYNC_DOMAINS = frozenset(("pdf-highlights", "epub-highlights", "document-notes"))

REPLICATION_DIGESTS_FILE_NAME = "replication-digests.json"
REPLICATION_DIGESTS_CONTRACT = "replication-digests/1"


def canonical_json_for_digest(value: Any) -> str:
    """与 JS 端 canonicalJSONString 逐位一致的序列化（对账的前提）。

    JS 端 = JSON.stringify(canonicalJSONValue(v))：键排序、紧凑分隔、
    非 ASCII 不转义。两端从同一 JSON 文本往返时数字格式一致
    （int 保持 int、float 最短往返），一致性由 parity 契约测试钉住。
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def materialize_domain_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """order → 存活条目（排除墓碑；序里没有的存活条目按 id 排序补末尾）。
    规则与 App 门面 readHighlightCollection 逐位一致。"""
    items = data.get("items") or {}
    order = [str(value) for value in (data.get("order") or [])]
    used: set[str] = set()
    ordered: list[str] = []
    for item_id in order:
        if item_id in items and item_id not in used:
            used.add(item_id)
            ordered.append(item_id)
    for item_id in sorted(items):
        if item_id not in used:
            ordered.append(item_id)
    return [items[item_id] for item_id in ordered]


def domain_digest(data: dict[str, Any]) -> str:
    materialized = materialize_domain_items(data)
    return hashlib.sha256(
        canonical_json_for_digest(materialized).encode("utf-8")
    ).hexdigest()


class ReplicationApplyError(RuntimeError):
    pass


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ReplicationDataStore:
    """一书一域一个 JSON 文件的数据副本。损坏必须出声，不静默重置。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, replication_book_id: str, domain: str) -> Path:
        if not re.fullmatch(r"^repbook-[a-f0-9]{32}$", replication_book_id):
            raise ReplicationApplyError("replicationBookId 形状非法")
        if not re.fullmatch(r"^[a-z][a-z-]{1,64}$", domain):
            raise ReplicationApplyError("domain 形状非法")
        return self._directory / replication_book_id / f"{domain}.json"

    def load(self, replication_book_id: str, domain: str) -> dict[str, Any]:
        path = self._path(replication_book_id, domain)
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {
                "contract": REPLICATION_BOOK_DATA_CONTRACT,
                "items": {},
                "tombstones": {},
                "order": [],
            }
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReplicationApplyError(
                f"数据副本 JSON 损坏（{path}），拒绝静默重置：{error}"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("contract") != REPLICATION_BOOK_DATA_CONTRACT
            or not isinstance(value.get("items"), dict)
            or not isinstance(value.get("tombstones"), dict)
            or not isinstance(value.get("order"), list)
        ):
            raise ReplicationApplyError(f"数据副本 contract 不符（{path}）")
        return value

    def save(
        self, replication_book_id: str, domain: str, value: dict[str, Any]
    ) -> None:
        _atomic_write_json(self._path(replication_book_id, domain), value)


def _apply_to_domain(
    data: dict[str, Any], operation: str, domain: str, body: dict[str, Any]
) -> None:
    item_id = body.get("id")
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise ReplicationApplyError(
            "命令 body 缺少合法条目 id（POST 必须带发送端已落库的完整条目）"
        )
    if operation == "upsert":
        item = {key: value for key, value in body.items() if key != "file"}
        data["items"][item_id] = item
        data["tombstones"].pop(item_id, None)
        if item_id not in data["order"]:
            data["order"].append(item_id)
        return
    if operation == "patch":
        current = data["items"].get(item_id)
        if current is None:
            raise ReplicationApplyError(f"PATCH 的条目不存在：{item_id}")
        fields = {
            key: value
            for key, value in body.items()
            if key not in ("file", "id")
        }
        unknown = set(fields) - _PATCH_FIELDS[domain]
        if unknown:
            raise ReplicationApplyError(
                f"PATCH 含表外字段：{sorted(unknown)}"
            )
        current.update(fields)
        return
    if operation == "tombstone":
        data["items"].pop(item_id, None)
        data["order"] = [value for value in data["order"] if value != item_id]
        # 幂等：条目不存在也写墓碑 —— 重放删除无害，且"删了什么"必须留痕。
        data["tombstones"][item_id] = {"time": int(time.time())}
        return
    raise ReplicationApplyError(f"未知操作：{operation}")


class ReplicationCommandApplier:
    """把账本里的命令按游标顺序应用到数据副本。单进程消费。"""

    def __init__(
        self,
        ledger: ReplicationCommandLedger,
        data_store: ReplicationDataStore,
        state_path: Path,
        dead_letter_path: Path,
        link_store: ReplicationBookLinkStore | None = None,
    ) -> None:
        self._ledger = ledger
        self._data = data_store
        self._state_path = state_path
        self._dead_letter_path = dead_letter_path
        self._links = link_store

    def _apply_pair(self, entry: LedgerEntry) -> None:
        if self._links is None:
            raise ReplicationApplyError("链接表未接线，无法处理配对公告")
        body = entry.body
        if not isinstance(body, dict) or set(body.keys()) != _PAIR_BODY_KEYS:
            raise ReplicationApplyError("配对公告 body 字段不符")
        if body["replicationBookId"] != entry.replication_book_id:
            raise ReplicationApplyError(
                "配对公告 body 与信封的 replicationBookId 不一致"
            )
        try:
            self._links.register_minted(
                peer_book_id=str(body["peerBookId"]),
                replication_book_id=str(body["replicationBookId"]),
                display_name=str(body["displayName"]),
            )
        except ReplicationLinkStoreError as error:
            raise ReplicationApplyError(str(error)) from error

    def _apply_resync(self, entry: LedgerEntry) -> None:
        body = entry.body
        if not isinstance(body, dict) or set(body.keys()) != _RESYNC_BODY_KEYS:
            raise ReplicationApplyError("重同步 body 字段不符")
        domain = body["domain"]
        items = body["items"]
        if domain not in _RESYNC_DOMAINS or not isinstance(items, list):
            raise ReplicationApplyError("重同步 domain/items 非法")
        data = self._data.load(entry.replication_book_id, domain)
        next_items: dict[str, Any] = {}
        next_order: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise ReplicationApplyError("重同步条目不是对象")
            item_id = item.get("id")
            if (
                not isinstance(item_id, str)
                or not _ITEM_ID_RE.fullmatch(item_id)
                or item_id in next_items
            ):
                raise ReplicationApplyError("重同步条目 id 非法或重复")
            next_items[item_id] = {
                key: value for key, value in item.items() if key != "file"
            }
            next_order.append(item_id)
        # 差集写墓碑：本端多出的存活条目就是"App 已删而命令没到"的那部分。
        for item_id in data["items"]:
            if item_id not in next_items:
                data["tombstones"][item_id] = {"time": int(time.time())}
        data["items"] = next_items
        data["order"] = next_order
        for item_id in next_order:
            data["tombstones"].pop(item_id, None)
        self._data.save(entry.replication_book_id, domain, data)

    def applied_cursor(self) -> int:
        try:
            raw = self._state_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return 0
        try:
            value = json.loads(raw)
            cursor = value["appliedCursor"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ReplicationApplyError(
                f"apply 状态文件损坏（{self._state_path}），拒绝静默重置：{error}"
            ) from error
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ReplicationApplyError("appliedCursor 非法")
        return cursor

    def _advance(self, cursor: int) -> None:
        _atomic_write_json(self._state_path, {"appliedCursor": cursor})

    def _dead_letter(self, entry: LedgerEntry, error: Exception) -> dict[str, Any]:
        record = {
            "cursor": entry.cursor,
            "mutationId": entry.mutation_id,
            "url": entry.url,
            "method": entry.method,
            "replicationBookId": entry.replication_book_id,
            "error": str(error),
            "atUtcMs": int(time.time() * 1000),
        }
        self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
        with self._dead_letter_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def apply_pending(self, limit: int = 100) -> dict[str, Any]:
        report: dict[str, Any] = {"applied": 0, "deadLetters": []}
        while True:
            entries = self._ledger.entries_after(self.applied_cursor(), limit)
            if not entries:
                return report
            for entry in entries:
                special = None
                if entry.url == PAIR_URL and entry.method == "POST":
                    special = self._apply_pair
                elif entry.url == RESYNC_URL and entry.method == "POST":
                    special = self._apply_resync
                if special is not None:
                    try:
                        special(entry)
                        report["applied"] += 1
                    except ReplicationApplyError as error:
                        report["deadLetters"].append(
                            self._dead_letter(entry, error)
                        )
                    self._advance(entry.cursor)
                    continue
                executor = _EXECUTORS.get((entry.url, entry.method))
                if executor is None:
                    # 表外端点：命令留在账本（可按游标 redrive），死信出声，
                    # 游标照常推进 —— 一条不认识的命令不能堵住整条管。
                    report["deadLetters"].append(self._dead_letter(
                        entry,
                        ReplicationApplyError("执行映射表外的端点"),
                    ))
                    self._advance(entry.cursor)
                    continue
                domain, operation = executor
                try:
                    data = self._data.load(entry.replication_book_id, domain)
                    _apply_to_domain(data, operation, domain, entry.body)
                    # 先落数据、后推游标：中间崩了重放这条命令，apply 幂等。
                    self._data.save(entry.replication_book_id, domain, data)
                    report["applied"] += 1
                except ReplicationApplyError as error:
                    report["deadLetters"].append(
                        self._dead_letter(entry, error)
                    )
                self._advance(entry.cursor)


def export_replication_digests(
    data_directory: Path, output_path: Path
) -> dict[str, Any]:
    """把每书每域的物化摘要导出成一个文件，供 C# 桥回答 App 的对账查询。

    对账（规格 §6）是命令复制的必需品：这个文件就是 Windows 端的
    "状态摘要"。App 拿它与本端物化摘要比对，不一致出声并触发整域重同步。
    """
    store = ReplicationDataStore(data_directory)
    books: dict[str, Any] = {}
    if data_directory.is_dir():
        for book_dir in sorted(data_directory.iterdir()):
            if not book_dir.is_dir() or not re.fullmatch(
                r"^repbook-[a-f0-9]{32}$", book_dir.name
            ):
                continue
            domains: dict[str, Any] = {}
            for path in sorted(book_dir.glob("*.json")):
                domain = path.stem
                data = store.load(book_dir.name, domain)
                domains[domain] = {
                    "digest": domain_digest(data),
                    "count": len(materialize_domain_items(data)),
                }
            if domains:
                books[book_dir.name] = domains
    value = {
        "contract": REPLICATION_DIGESTS_CONTRACT,
        "atUtcMs": int(time.time() * 1000),
        "books": books,
    }
    _atomic_write_json(output_path, value)
    return value


def run_once(
    local_root: Path, spool_directory: Path | None = None
) -> dict[str, Any]:
    """一轮：spool 摄取 → apply。每轮结束写状态文件（诊断出口）。

    ⚠ spool 在 **C# 桥的 runtime 目录**（`~/bw-computer-voice-bridge/runtime/
    replication-spool`，即 RuntimeStatusPath 的父目录），不在 BWReader 下 ——
    两个根不是一个地方，传错了整条管道会静默空转、状态文件却一直 ok。
    生产调用方必须显式传 spool_directory（launcher 从 BridgePaths 取）。
    """
    if spool_directory is None:
        spool_directory = local_root / "runtime" / "replication-spool"
    ledger_path = local_root / "replication-command-ledger.sqlite3"
    status_path = local_root / APPLY_STATUS_FILE_NAME
    status: dict[str, Any] = {
        "contract": "replication-apply-status/1",
        "atUtcMs": int(time.time() * 1000),
        "ok": False,
    }
    try:
        ledger = ReplicationCommandLedger(ledger_path)
        try:
            ingest = (
                ledger.ingest_spool_directory(spool_directory)
                if spool_directory.is_dir()
                else {"files": [], "ingested": 0, "replayed": 0,
                      "conflicts": [], "invalid": []}
            )
            applier = ReplicationCommandApplier(
                ledger,
                ReplicationDataStore(local_root / REPLICATION_DATA_DIRECTORY_NAME),
                local_root / APPLY_STATE_FILE_NAME,
                local_root / DEAD_LETTER_FILE_NAME,
                link_store=ReplicationBookLinkStore(
                    local_root / default_links_path().name
                ),
            )
            apply_report = applier.apply_pending()
            # 摘要文件放桥的 runtime 目录（spool 的父目录），C# 能直接读；
            # 测试/无桥场景退到 local_root。
            digests_path = (
                spool_directory.parent / REPLICATION_DIGESTS_FILE_NAME
                if spool_directory.name == "replication-spool"
                else local_root / REPLICATION_DIGESTS_FILE_NAME
            )
            digests = export_replication_digests(
                local_root / REPLICATION_DATA_DIRECTORY_NAME, digests_path
            )
            status.update(
                ok=True,
                digestBooks=len(digests["books"]),
                digestsPath=str(digests_path),
                spoolDirectory=str(spool_directory),
                ingested=ingest["ingested"],
                replayed=ingest["replayed"],
                spoolConflicts=len(ingest["conflicts"]),
                spoolInvalid=len(ingest["invalid"]),
                applied=apply_report["applied"],
                deadLetters=len(apply_report["deadLetters"]),
                appliedCursor=applier.applied_cursor(),
                headCursor=ledger.head_cursor(),
            )
        finally:
            ledger.close()
    except (ReplicationCommandError, ReplicationApplyError, OSError) as error:
        status["error"] = str(error)
    _atomic_write_json(status_path, status)
    return status


def worker_loop(
    local_root: Path,
    spool_directory: Path | None = None,
    *,
    interval_seconds: float = 30.0,
    sleeper: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """readerpc 的常驻摄取线程入口。任何一轮的失败都被状态文件记录，
    绝不让异常杀死线程（那是静默失败的经典形态）。"""
    while not should_stop():
        try:
            run_once(local_root, spool_directory)
        except Exception:  # noqa: BLE001 - 最后防线，状态文件已尽力
            pass
        sleeper(interval_seconds)


if __name__ == "__main__":
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"
    print(json.dumps(run_once(root), ensure_ascii=False, indent=2))
