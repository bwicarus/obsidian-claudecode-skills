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

import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

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

_ITEM_ID_RE = re.compile(r"^(?:c_[a-f0-9]{8,32}|h_[a-f0-9]{6,32}|e[a-f0-9]{6,16})$")

# PATCH 允许改的字段，照 App 路由的允许集（native-local-runtime.js 的
# localPDFHighlights / localEPUBHighlights）。POST 是整条 upsert 不看这张表。
_PATCH_FIELDS = {
    "pdf-highlights": frozenset(("color", "text", "note", "kind", "sentence", "body")),
    "epub-highlights": frozenset(("color", "note", "kind", "sentence", "body")),
}

_EXECUTORS: dict[tuple[str, str], tuple[str, str]] = {
    # (url, method) -> (domain, operation)
    ("/pdf/api/highlights", "POST"): ("pdf-highlights", "upsert"),
    ("/pdf/api/highlights", "PATCH"): ("pdf-highlights", "patch"),
    ("/pdf/api/highlights", "DELETE"): ("pdf-highlights", "tombstone"),
    ("/pdf/api/epub-highlights", "POST"): ("epub-highlights", "upsert"),
    ("/pdf/api/epub-highlights", "PATCH"): ("epub-highlights", "patch"),
    ("/pdf/api/epub-highlights", "DELETE"): ("epub-highlights", "tombstone"),
}


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
    ) -> None:
        self._ledger = ledger
        self._data = data_store
        self._state_path = state_path
        self._dead_letter_path = dead_letter_path

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
            )
            apply_report = applier.apply_pending()
            status.update(
                ok=True,
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
