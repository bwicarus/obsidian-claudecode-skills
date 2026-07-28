#!/usr/bin/env python3
"""唯一的 emergent KG 节点写入服务。

目标：
- 新节点在第一次持久化前得到不可变稳定 ID；
- canonical key、手工 alias、已有 authored KG 与机器概念笔记共同去重；
- page brief 的标签本身永远不是证据，必须携带可在原文中逐字复核的 quote；
- 每次节点变更先写 prepare journal，再原子替换图，最后写 commit；
- rollback 不物理删除新 ID，而写 tombstone，后续自动任务不能把它复活。

边、掌握度和 UI 投影仍由现有 KG 模块负责；本服务只拥有节点身份与节点证据。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import errno
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

# 允许 `python3 scripts/kg/concept_node_service.py` 和独立后台子进程直接加载。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config
import attention_profile as AP


CONTRACT = "concept-node-service/1"
LOG_CONTRACT = "kg-node-mutation-log/1"
HISTORY_CONTRACT = "kg-node-history/1"
_PROJECT_DIR = Path(config.PROJECT_DIR)
DEFAULT_GRAPH = _PROJECT_DIR / "state" / "attention" / "emergent-graph.json"
DEFAULT_JOURNAL = (
    _PROJECT_DIR / "state" / "attention" / "kg-node-mutations.jsonl"
)
DEFAULT_ALIASES = (
    _PROJECT_DIR / "state" / "attention" / "concept-aliases.json"
)
DEFAULT_CONFIRMATIONS = (
    _PROJECT_DIR / "state" / "attention" / "emergent-confirmations.json"
)
DEFAULT_KG_DIR = _PROJECT_DIR / "knowledge_graph"
DEFAULT_CONCEPT_ROOT = Path(AP.VAULT_ROOT) / "资源" / "概念"

_SOURCE_KINDS_WITH_QUOTE = {"page-brief", "book-occurrence"}
_SOURCE_KINDS_WITH_REFERENCE = {
    "note", "diagnostic", "autonote", "page-brief", "book-occurrence",
}
_MAX_MUTATIONS = 5000
_MAX_PROVENANCE = 64
_process_lock = threading.RLock()
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
_LOCK_CONTENTION_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    getattr(errno, "EDEADLK", errno.EACCES),
}


class ConceptNodeError(RuntimeError):
    def __init__(self, message: str, code: str = "BW_KG_NODE_ERROR", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _evidence_id(
    *,
    node_key: str,
    source_kind: str,
    source_id: str,
    document_ref: str,
    page: int,
    quote: str,
) -> str:
    basis = {
        "sourceKind": source_kind,
        "sourceId": source_id,
        "documentRef": document_ref,
        "page": int(page or 0),
        "quote": _text_key(quote),
        "key": node_key,
    }
    return "kgev:" + hashlib.sha256(
        _canonical_json(basis).encode("utf-8")
    ).hexdigest()[:24]


def _legacy_reference_identity(source_kind: str, ref: str) -> tuple[str, str]:
    if source_kind == "note":
        return "note:" + ref, "vault-note:" + ref
    if source_kind == "diagnostic":
        return "diagnostic:" + ref, "diagnostic:" + ref
    if source_kind == "autonote":
        return "autonote:" + Path(ref).name, "vault:" + ref
    return source_kind + ":" + ref, ref


def _page_brief_occurrence(
    *,
    node_key: str,
    document_ref: str,
    page: int,
    quote: str,
) -> dict:
    quote_key = _evidence_match_key(quote)
    if not document_ref or int(page or 0) <= 0 or not quote_key:
        raise ConceptNodeError(
            "PageBrief 历史证据无法规范化",
            "BW_KG_NODE_HISTORY_INCOMPLETE",
            {"nodeKey": node_key},
        )
    return {
        "kind": "page-brief",
        "nodeKey": node_key,
        "documentRef": document_ref,
        "page": int(page),
        "quoteMatchSha256": hashlib.sha256(
            quote_key.encode("utf-8")
        ).hexdigest(),
    }


def _candidate_occurrence(node_key: str, candidate: dict) -> dict:
    if candidate.get("sourceKind") == "page-brief":
        return _page_brief_occurrence(
            node_key=node_key,
            document_ref=str(candidate.get("documentRef") or ""),
            page=int(candidate.get("page") or 0),
            quote=str(candidate.get("quote") or ""),
        )
    evidence_id = str(candidate.get("evidenceId") or "")
    if not evidence_id:
        raise ConceptNodeError(
            "候选证据缺少稳定身份",
            "BW_KG_NODE_HISTORY_INCOMPLETE",
            {"nodeKey": node_key},
        )
    return {
        "kind": "evidence-id",
        "nodeKey": node_key,
        "evidenceId": evidence_id,
    }


def _stored_occurrence(node_key: str, evidence: dict) -> dict:
    source_kind = str(evidence.get("type") or "")
    if source_kind == "page-brief":
        return _page_brief_occurrence(
            node_key=node_key,
            document_ref=str(evidence.get("documentRef") or ""),
            page=int(evidence.get("page") or 0),
            quote=str(evidence.get("quote") or ""),
        )
    evidence_id = str(evidence.get("id") or "")
    if not evidence_id:
        ref = str(evidence.get("ref") or "")
        source_id = str(evidence.get("sourceId") or "")
        document_ref = str(evidence.get("documentRef") or "")
        if ref and source_kind:
            source_id, document_ref = _legacy_reference_identity(
                source_kind,
                ref,
            )
        if not source_kind or not source_id or not document_ref:
            raise ConceptNodeError(
                "存量 KG 证据缺少可补铸的因果身份",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
                {"nodeKey": node_key, "sourceKind": source_kind},
            )
        evidence_id = _evidence_id(
            node_key=node_key,
            source_kind=source_kind,
            source_id=source_id,
            document_ref=document_ref,
            page=int(evidence.get("page") or 0),
            quote=str(evidence.get("quote") or ""),
        )
    return {
        "kind": "evidence-id",
        "nodeKey": node_key,
        "evidenceId": evidence_id,
    }


def _occurrence_map_for_nodes(nodes: dict) -> dict[str, dict]:
    occurrences = {}
    for raw_key, raw_node in (nodes or {}).items():
        if not isinstance(raw_node, dict):
            continue
        node_key = unicodedata.normalize(
            "NFKC",
            str(raw_key or ""),
        ).strip().casefold()
        if not node_key:
            raise ConceptNodeError(
                "KG 节点缺少可补铸的 canonical key",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
            )
        for evidence in raw_node.get("provenance") or []:
            if not isinstance(evidence, dict):
                raise ConceptNodeError(
                    "KG provenance 格式无效",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                    {"nodeKey": node_key},
                )
            occurrence = _stored_occurrence(node_key, evidence)
            occurrences[_canonical_json(occurrence)] = occurrence
    return occurrences


def _receipt_result(raw: dict) -> dict:
    result = copy.deepcopy(raw)
    result.pop("_kgRequestDigest", None)
    result.pop("_kgOperationContract", None)
    return result


def _receipt_record(raw: dict) -> dict:
    return {
        "requestDigest": str(raw.get("_kgRequestDigest") or ""),
        "operationContract": str(raw.get("_kgOperationContract") or ""),
        "result": _receipt_result(raw),
    }


def _ledger_receipt(
    result: dict,
    *,
    request_digest: str,
    operation_contract: str,
) -> dict:
    receipt = copy.deepcopy(result)
    receipt["_kgRequestDigest"] = request_digest
    receipt["_kgOperationContract"] = operation_contract
    return receipt


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_key(value: Any) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")),
    )


def _evidence_match_key(value: Any) -> str:
    """与 PageBrief 生成侧一致的连续证据比较键。

    只做 NFKC、移除空白和 Unicode casefold；标点、词序和所有非空白字符均保留，
    因此后续普通子串判断仍要求证据连续出现，不接受改写、缺词或非连续拼接。
    持久化身份继续使用区分大小写的 `_text_key`，避免改变既有 evidence ID/摘要。
    """
    return _text_key(value).casefold()


def _page_brief_occurrence_replayed(
    candidate: dict,
    provenance: Iterable[dict],
) -> bool:
    """同一页的同一逐字证据只能为节点贡献一次 signal。

    旧版 PageBrief 把整页文字摘要混入 sourceId；OCR、空白或无关正文变化会让同一证据
    得到新 sourceId/evidenceId。这里仅对 page-brief 动态识别同一文档、同一页、同一
    连续证据；不改写存量 provenance，也不放宽其他 sourceKind 的身份语义。
    """
    if candidate.get("sourceKind") != "page-brief":
        return False
    quote_key = _evidence_match_key(candidate.get("quote"))
    if not quote_key:
        return False
    for evidence in provenance:
        if not isinstance(evidence, dict):
            continue
        if (
            evidence.get("type") == "page-brief"
            and evidence.get("documentRef") == candidate.get("documentRef")
            and evidence.get("page") == candidate.get("page")
            and _evidence_match_key(evidence.get("quote")) == quote_key
        ):
            return True
    return False


def stable_node_id(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        raise ConceptNodeError("concept key 不能为空", "BW_KG_NODE_KEY")
    return "em:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _read_json_optional_strict(path: Path, fallback, *, code: str):
    if not path.exists():
        return copy.deepcopy(fallback)
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ConceptNodeError(
            "KG 依赖文件无法读取，拒绝在不完整索引上自动建点",
            code,
            {"path": str(path), "error": str(exc)},
        ) from exc
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(str(path), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_durable_directory(path: Path) -> None:
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        # Several independent KG writers can discover the same missing state
        # directory before either has created it.  ``exist_ok`` only absorbs
        # the safe "the peer created this directory first" race; a file or an
        # otherwise invalid entry at the path still raises and keeps writes
        # fail closed.
        directory.mkdir(exist_ok=True)
        _fsync_directory(directory.parent)


def _write_json_atomic(path: Path, value: Any) -> None:
    _ensure_durable_directory(path.parent)
    tmp = path.with_name(
        "." + path.name + "." + str(os.getpid()) + "." + uuid.uuid4().hex + ".tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _append_jsonl(path: Path, value: dict) -> None:
    _ensure_durable_directory(path.parent)
    line = _canonical_json(value) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _lock_failure(
    path: Path,
    *,
    code: str,
    message: str,
    error: BaseException | None = None,
) -> ConceptNodeError:
    details = {"path": str(path)}
    if error is not None:
        details["error"] = str(error)
    return ConceptNodeError(message, code, details)


def _wait_for_file_lock(
    path: Path,
    *,
    deadline: float,
    poll_seconds: float,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_TIMEOUT",
            message="KG 写锁等待超时，拒绝无锁写入",
        )
    time.sleep(min(poll_seconds, remaining))


def _acquire_posix_file_lock(
    handle,
    path: Path,
    *,
    deadline: float,
    poll_seconds: float,
):
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - normal POSIX has fcntl
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_UNAVAILABLE",
            message="当前平台缺少 POSIX 文件锁，拒绝无锁写入",
            error=exc,
        ) from exc

    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                raise _lock_failure(
                    path,
                    code="BW_KG_NODE_LOCK_FAILED",
                    message="KG 写锁获取失败，拒绝继续写入",
                    error=exc,
                ) from exc
            _wait_for_file_lock(
                path,
                deadline=deadline,
                poll_seconds=poll_seconds,
            )

    def unlock() -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return unlock


def _acquire_windows_file_lock(
    handle,
    path: Path,
    *,
    deadline: float,
    poll_seconds: float,
):
    try:
        import msvcrt
    except ImportError as exc:  # pragma: no cover - normal Windows has msvcrt
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_UNAVAILABLE",
            message="当前平台缺少 Windows 文件锁，拒绝无锁写入",
            error=exc,
        ) from exc

    # msvcrt.locking 从当前文件位置起锁定指定字节；确保第 0 字节存在，
    # 所有进程都锁同一范围。锁文件只承担同步用途，不保存业务数据。
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_FAILED",
            message="Windows KG 锁文件无法初始化，拒绝继续写入",
            error=exc,
        ) from exc

    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                raise _lock_failure(
                    path,
                    code="BW_KG_NODE_LOCK_FAILED",
                    message="Windows KG 写锁获取失败，拒绝继续写入",
                    error=exc,
                ) from exc
            _wait_for_file_lock(
                path,
                deadline=deadline,
                poll_seconds=poll_seconds,
            )

    def unlock() -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    return unlock


@contextlib.contextmanager
def _exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = _LOCK_POLL_SECONDS,
):
    """进程内 + 内核跨进程写锁；任何降级或超时都 fail-closed。"""
    path = Path(path)
    try:
        timeout_seconds = float(timeout_seconds)
        poll_seconds = float(poll_seconds)
    except (TypeError, ValueError) as exc:
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_CONFIG",
            message="KG 写锁等待参数无效",
            error=exc,
        ) from exc
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
        or timeout_seconds > threading.TIMEOUT_MAX
        or not math.isfinite(poll_seconds)
        or poll_seconds <= 0
    ):
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_CONFIG",
            message="KG 写锁等待参数无效",
        )

    deadline = time.monotonic() + timeout_seconds
    process_locked = _process_lock.acquire(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if not process_locked:
        raise _lock_failure(
            path,
            code="BW_KG_NODE_LOCK_TIMEOUT",
            message="KG 进程内写锁等待超时，拒绝继续写入",
        )

    try:
        try:
            _ensure_durable_directory(path.parent)
            handle = path.open("a+b")
        except OSError as exc:
            raise _lock_failure(
                path,
                code="BW_KG_NODE_LOCK_FAILED",
                message="KG 锁文件无法打开，拒绝继续写入",
                error=exc,
            ) from exc

        with handle:
            if os.name == "nt":
                unlock = _acquire_windows_file_lock(
                    handle,
                    path,
                    deadline=deadline,
                    poll_seconds=poll_seconds,
                )
            elif os.name == "posix":
                unlock = _acquire_posix_file_lock(
                    handle,
                    path,
                    deadline=deadline,
                    poll_seconds=poll_seconds,
                )
            else:  # pragma: no cover - Python targets here are Windows/POSIX
                raise _lock_failure(
                    path,
                    code="BW_KG_NODE_LOCK_UNAVAILABLE",
                    message="当前平台没有受支持的内核文件锁，拒绝无锁写入",
                )

            body_error: BaseException | None = None
            try:
                yield
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                try:
                    unlock()
                except Exception as exc:
                    release_error = _lock_failure(
                        path,
                        code="BW_KG_NODE_LOCK_RELEASE",
                        message="KG 写锁释放失败",
                        error=exc,
                    )
                    if body_error is None:
                        raise release_error from exc
                    if hasattr(body_error, "add_note"):
                        body_error.add_note(str(release_error))
    finally:
        _process_lock.release()


def _empty_graph() -> dict:
    return {
        "nodes": {},
        "edges": [],
        "edge_claims": {},
        "edge_audits": {},
        "meta": {},
    }


def _normalize_graph(value: Any) -> dict:
    graph = value if isinstance(value, dict) else _empty_graph()
    graph = copy.deepcopy(graph)
    graph.setdefault("nodes", {})
    graph.setdefault("edges", [])
    graph.setdefault("edge_claims", {})
    graph.setdefault("edge_audits", {})
    graph.setdefault("meta", {})
    if not isinstance(graph["nodes"], dict):
        raise ConceptNodeError("emergent graph nodes 无效", "BW_KG_NODE_GRAPH")
    if not isinstance(graph["meta"], dict):
        graph["meta"] = {}
    return graph


class ConceptNodeService:
    def __init__(
        self,
        *,
        graph_path: Path = DEFAULT_GRAPH,
        journal_path: Path = DEFAULT_JOURNAL,
        aliases_path: Path = DEFAULT_ALIASES,
        confirmations_path: Path = DEFAULT_CONFIRMATIONS,
        kg_dir: Path = DEFAULT_KG_DIR,
        concept_root: Path = DEFAULT_CONCEPT_ROOT,
        normalizer: Callable[[str], str] | None = None,
        clock: Callable[[], float] | None = None,
        tx_factory: Callable[[], str] | None = None,
    ):
        self.graph_path = Path(graph_path)
        self.journal_path = Path(journal_path)
        self.aliases_path = Path(aliases_path)
        self.confirmations_path = Path(confirmations_path)
        self.kg_dir = Path(kg_dir)
        self.concept_root = Path(concept_root)
        self.lock_path = self.graph_path.with_suffix(self.graph_path.suffix + ".lock")
        self.normalizer = normalizer or AP.norm_key
        self.clock = clock or time.time
        self.tx_factory = tx_factory or (
            lambda: "kgntx-" + uuid.uuid4().hex
        )

    def normalize(self, value: Any) -> str:
        surface = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not surface:
            return ""
        try:
            key = str(self.normalizer(surface) or surface)
        except Exception:
            key = surface
        return unicodedata.normalize("NFKC", key).strip().casefold()

    def load_graph(self) -> dict:
        if not self.graph_path.exists():
            return _empty_graph()
        try:
            value = json.loads(self.graph_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ConceptNodeError(
                "emergent graph 无法读取，拒绝按空图覆盖",
                "BW_KG_NODE_GRAPH_CORRUPT",
                {"path": str(self.graph_path), "error": str(exc)},
            ) from exc
        return _normalize_graph(value)

    @staticmethod
    def graph_digest(graph: dict) -> str:
        return _digest(_normalize_graph(graph))

    def _journal_rows(self, *, repair_torn_tail: bool = False) -> list[dict]:
        if not self.journal_path.exists():
            return []
        try:
            payload = self.journal_path.read_bytes()
        except OSError as exc:
            raise ConceptNodeError(
                "KG mutation journal 无法读取",
                "BW_KG_NODE_JOURNAL_CORRUPT",
                {"path": str(self.journal_path), "error": str(exc)},
            ) from exc
        final_newline = payload.rfind(b"\n")
        prefix = payload[: final_newline + 1] if final_newline >= 0 else b""
        tail = payload[final_newline + 1:]
        try:
            prefix_text = prefix.decode("utf-8")
        except UnicodeError as exc:
            raise ConceptNodeError(
                "KG mutation journal 中间记录编码损坏",
                "BW_KG_NODE_JOURNAL_CORRUPT",
                {"path": str(self.journal_path), "error": str(exc)},
            ) from exc
        tail_text = ""
        if tail:
            try:
                tail_text = tail.decode("utf-8")
            except UnicodeError as exc:
                if not repair_torn_tail:
                    raise ConceptNodeError(
                        "KG mutation journal 尾部编码不完整",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"path": str(self.journal_path), "error": str(exc)},
                    ) from exc
                self._repair_journal_tail(len(prefix), append_newline=False)

        rows = []
        # JSONL 记录只以物理 ASCII LF 分隔。str.splitlines() 还会把
        # JSON 字符串内合法的 U+0085/U+2028/U+2029 当成换行，进而把
        # 一次已经提交的 mutation 误判为中段损坏。
        chunks = [
            content + "\n"
            for content in prefix_text.split("\n")[:-1]
        ]
        if tail_text:
            chunks.append(tail_text)
        offset = 0
        for index, chunk in enumerate(chunks):
            has_newline = chunk.endswith("\n")
            content = chunk[:-1] if has_newline else chunk
            if content.endswith("\r"):
                content = content[:-1]
            is_last = index == len(chunks) - 1
            try:
                row = json.loads(content)
            except (TypeError, ValueError) as exc:
                if is_last and not has_newline and repair_torn_tail:
                    self._repair_journal_tail(offset, append_newline=False)
                    break
                raise ConceptNodeError(
                    (
                        "KG mutation journal 尾部记录不完整"
                        if is_last and not has_newline
                        else "KG mutation journal 中间记录损坏"
                    ),
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {
                        "path": str(self.journal_path),
                        "line": index + 1,
                        "error": str(exc),
                    },
                ) from exc
            if not isinstance(row, dict) or row.get("contract") != LOG_CONTRACT:
                raise ConceptNodeError(
                    "KG mutation journal 合同无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"path": str(self.journal_path), "line": index + 1},
                )
            rows.append(row)
            offset += len(chunk.encode("utf-8"))
            if is_last and not has_newline and repair_torn_tail:
                self._repair_journal_tail(offset, append_newline=True)
        return rows

    def _repair_journal_tail(self, offset: int, *, append_newline: bool) -> None:
        try:
            with self.journal_path.open("r+b") as handle:
                handle.truncate(max(0, int(offset)))
                handle.seek(0, os.SEEK_END)
                if append_newline:
                    handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ConceptNodeError(
                "KG mutation journal 尾部无法安全修复",
                "BW_KG_NODE_JOURNAL_CORRUPT",
                {"path": str(self.journal_path), "error": str(exc)},
            ) from exc

    @staticmethod
    def _operation_identity(
        operation_contract: str,
        operation_payload: dict,
    ) -> tuple[str, str]:
        operation_contract = str(operation_contract or "").strip()
        if (
            not operation_contract
            or len(operation_contract) > 160
            or not isinstance(operation_payload, dict)
        ):
            raise ConceptNodeError(
                "KG operation contract/payload 无效",
                "BW_KG_NODE_OPERATION",
            )
        return operation_contract, _digest({
            "operationContract": operation_contract,
            "payload": operation_payload,
        })

    def _baseline_occurrences(self, graph: dict) -> dict[str, dict]:
        occurrences = _occurrence_map_for_nodes(graph.get("nodes") or {})
        for raw_key, raw_node in (graph.get("nodes") or {}).items():
            if not isinstance(raw_node, dict):
                continue
            node_key = self.normalize(raw_key)
            retained = {
                key: value
                for key, value in occurrences.items()
                if value.get("nodeKey") == node_key
            }
            raw_signal = raw_node.get("signal", 0)
            if type(raw_signal) is not int or raw_signal < 0:
                raise ConceptNodeError(
                    "KG signal 类型无效，无法补铸历史",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                    {"nodeKey": node_key, "signal": raw_signal},
                )
            signal = raw_signal
            if signal != len(retained):
                raise ConceptNodeError(
                    "KG signal 无法由当前唯一 provenance 完整证明",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                    {
                        "nodeKey": node_key,
                        "signal": signal,
                        "provenance": len(retained),
                    },
                )
        return occurrences

    def _validated_history_occurrences(
        self,
        values: Any,
        *,
        label: str,
    ) -> dict[str, dict]:
        if not isinstance(values, list):
            raise ConceptNodeError(
                f"KG history {label} 必须是 occurrence 数组",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        occurrences: dict[str, dict] = {}
        for occurrence in values:
            if not isinstance(occurrence, dict):
                raise ConceptNodeError(
                    f"KG history {label} occurrence 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                )
            kind = str(occurrence.get("kind") or "")
            node_key = str(occurrence.get("nodeKey") or "")
            valid = bool(node_key) and self.normalize(node_key) == node_key
            if kind == "page-brief":
                valid = (
                    valid
                    and bool(str(occurrence.get("documentRef") or ""))
                    and isinstance(occurrence.get("page"), int)
                    and int(occurrence.get("page") or 0) > 0
                    and bool(re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(occurrence.get("quoteMatchSha256") or ""),
                    ))
                )
            elif kind == "evidence-id":
                valid = (
                    valid
                    and bool(str(occurrence.get("evidenceId") or ""))
                )
            else:
                valid = False
            occurrence_key = _canonical_json(occurrence)
            if not valid or occurrence_key in occurrences:
                raise ConceptNodeError(
                    f"KG history {label} occurrence 结构无效或重复",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"nodeKey": node_key, "kind": kind},
                )
            occurrences[occurrence_key] = copy.deepcopy(occurrence)
        return occurrences

    @staticmethod
    def _prove_v1_journal_for_baseline(
        rows: list[dict],
        *,
        graph_digest: str,
    ) -> dict:
        """Prove the legacy prepare/terminal chain before minting history/1.

        The migration may preserve old receipts only when every committed v1
        mutation is tied to one preceding prepare and the serialized graph
        digest chain ends at the graph being migrated.  A syntactically valid
        orphan commit, duplicate terminal, overlapping prepare, or conflict is
        therefore never upgraded into trusted cold history.
        """
        prepared: dict[str, dict] = {}
        committed_by_mutation: dict[str, str] = {}
        committed_tx_ids: set[str] = set()
        rollback_tx_ids: set[str] = set()
        rollback_targets_by_tx: dict[str, str] = {}
        rolled_back: set[str] = set()
        all_tx_ids: set[str] = set()
        open_tx = ""
        current_digest = ""

        def incomplete(message: str, *, line: int, tx_id: str = "") -> None:
            raise ConceptNodeError(
                message,
                "BW_KG_NODE_HISTORY_INCOMPLETE",
                {"line": line, "txId": tx_id},
            )

        for index, row in enumerate(rows):
            phase = str(row.get("phase") or "")
            tx_id = str(row.get("txId") or "")
            mutation_id = str(row.get("mutationId") or "")
            if phase == "prepare":
                if (
                    not tx_id
                    or not mutation_id
                    or tx_id in all_tx_ids
                    or open_tx
                    or not isinstance(row.get("beforeNodes"), dict)
                    or not isinstance(row.get("afterNodes"), dict)
                    or set(row.get("beforeNodes") or {})
                    != set(row.get("afterNodes") or {})
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(row.get("graphBeforeDigest") or ""),
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(row.get("graphAfterDigest") or ""),
                    )
                    or row.get("history") is not None
                ):
                    incomplete(
                        "v1 KG prepare 无法形成唯一串行证明",
                        line=index + 1,
                        tx_id=tx_id,
                    )
                before_digest = str(row["graphBeforeDigest"])
                if not current_digest:
                    current_digest = before_digest
                elif before_digest != current_digest:
                    incomplete(
                        "v1 KG prepare 的前图摘要不连续",
                        line=index + 1,
                        tx_id=tx_id,
                    )
                prepared[tx_id] = row
                all_tx_ids.add(tx_id)
                open_tx = tx_id
                continue

            if phase not in {"commit", "abort", "conflict"}:
                incomplete(
                    "v1 KG journal 含未知或越界 phase",
                    line=index + 1,
                    tx_id=tx_id,
                )
            prepare = prepared.get(tx_id)
            if (
                not tx_id
                or not mutation_id
                or prepare is None
                or open_tx != tx_id
                or str(prepare.get("mutationId") or "") != mutation_id
            ):
                incomplete(
                    "v1 KG terminal 缺少唯一配对 prepare",
                    line=index + 1,
                    tx_id=tx_id,
                )
            if phase == "conflict":
                raise ConceptNodeError(
                    "v1 KG journal 含未裁定冲突，拒绝补铸历史",
                    "BW_KG_NODE_JOURNAL_CONFLICT",
                    {"line": index + 1, "txId": tx_id},
                )
            terminal_digest = str(row.get("graphDigest") or "")
            if phase == "commit":
                after_digest = str(prepare.get("graphAfterDigest") or "")
                if terminal_digest != after_digest:
                    incomplete(
                        "v1 KG commit 与 prepare 后图摘要不一致",
                        line=index + 1,
                        tx_id=tx_id,
                    )
                if mutation_id in committed_by_mutation:
                    incomplete(
                        "v1 KG mutationId 存在多个 commit",
                        line=index + 1,
                        tx_id=tx_id,
                    )
                rollback_of = str(row.get("rollbackOf") or "")
                if rollback_of:
                    if (
                        rollback_of == tx_id
                        or rollback_of not in committed_tx_ids
                        or rollback_of in rollback_tx_ids
                        or rollback_of in rolled_back
                    ):
                        incomplete(
                            "v1 KG rollback 目标无效、重复或为 rollback",
                            line=index + 1,
                            tx_id=tx_id,
                        )
                    rolled_back.add(rollback_of)
                    rollback_tx_ids.add(tx_id)
                    rollback_targets_by_tx[tx_id] = rollback_of
                committed_by_mutation[mutation_id] = tx_id
                committed_tx_ids.add(tx_id)
                current_digest = after_digest
            elif terminal_digest and terminal_digest != current_digest:
                incomplete(
                    "v1 KG abort 与当前图摘要不一致",
                    line=index + 1,
                    tx_id=tx_id,
                )
            open_tx = ""

        if open_tx:
            incomplete(
                "v1 KG journal 留有未完成 prepare",
                line=len(rows),
                tx_id=open_tx,
            )
        if rows and current_digest != graph_digest:
            raise ConceptNodeError(
                "当前 KG 图无法由完整 v1 journal 链证明",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
                {
                    "graphDigest": graph_digest,
                    "journalDigest": current_digest,
                },
            )
        return {
            "committedByMutation": committed_by_mutation,
            "rolledBackTxIds": rolled_back,
            "rollbackTxIds": rollback_tx_ids,
            "rollbackTargetsByTx": rollback_targets_by_tx,
            "allTxIds": all_tx_ids,
        }

    def _ensure_history_baseline_locked(
        self,
        graph: dict,
        rows: list[dict],
    ) -> list[dict]:
        baselines = [
            row
            for row in rows
            if row.get("phase") == "history-baseline"
        ]
        if len(baselines) > 1:
            raise ConceptNodeError(
                "KG history baseline 重复",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        if baselines:
            return rows

        occurrences = self._baseline_occurrences(graph)
        mutation_map = (
            (graph.get("meta") or {}).get("node_mutations", {})
            if isinstance(graph.get("meta"), dict) else {}
        )
        if not isinstance(mutation_map, dict):
            raise ConceptNodeError(
                "KG hot mutation ledger 无效",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
            )
        graph_digest = _digest(graph)
        proof = self._prove_v1_journal_for_baseline(
            rows,
            graph_digest=graph_digest,
        )
        committed_by_mutation = proof["committedByMutation"]
        committed_ids = set(committed_by_mutation)
        if not committed_ids and (mutation_map or occurrences):
            raise ConceptNodeError(
                "非空 KG 历史缺少 journal 证明",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
            )
        if (graph.get("meta") or {}).get("kg_history"):
            raise ConceptNodeError(
                "KG history head 存在但 baseline 缺失",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        receipts = {}
        for mutation_id, raw in mutation_map.items():
            if not isinstance(raw, dict) or mutation_id not in committed_ids:
                raise ConceptNodeError(
                    "KG hot receipt 缺少 journal commit 证明",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                    {"mutationId": mutation_id},
                )
            result = _receipt_result(raw)
            if str(result.get("txId") or "") != committed_by_mutation[mutation_id]:
                raise ConceptNodeError(
                    "KG hot receipt 与 v1 commit 事务不一致",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                    {"mutationId": mutation_id},
                )
            receipt = _receipt_record(raw)
            tx_id = committed_by_mutation[mutation_id]
            rollback_target = proof["rollbackTargetsByTx"].get(tx_id)
            is_proven_receipt = (
                bool(re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(receipt.get("requestDigest") or ""),
                ))
                and bool(str(receipt.get("operationContract") or ""))
                and isinstance(receipt.get("result"), dict)
                and str(receipt["result"].get("txId") or "") == tx_id
                and str(receipt["result"].get("mutationId") or "")
                == str(mutation_id)
                and str(receipt["result"].get("contract") or "") == CONTRACT
            )
            if rollback_target:
                is_proven_receipt = (
                    is_proven_receipt
                    and receipt.get("operationContract") == "kg-op/rollback/1"
                    and str(receipt["result"].get("rollbackOf") or "")
                    == rollback_target
                )
            else:
                is_proven_receipt = (
                    is_proven_receipt
                    and receipt.get("operationContract") != "kg-op/rollback/1"
                    and receipt["result"].get("rollbackOf") in (None, "")
                )
            # v1 journal 可以证明 mutationId/tx 已占用，却未必能证明
            # hot receipt 的请求身份。无法证明时只预约 mutationId，
            # 不把它升级成可冷重放的 success receipt。
            if is_proven_receipt:
                receipts[str(mutation_id)] = receipt
        rolled_back = sorted(proof["rolledBackTxIds"])
        baseline = {
            "contract": LOG_CONTRACT,
            "phase": "history-baseline",
            "historyContract": HISTORY_CONTRACT,
            "ts": int(self.clock()),
            "graphDigest": graph_digest,
            "journalPrefixDigest": _digest(rows),
            "receipts": receipts,
            "legacyMutationIds": sorted(committed_ids),
            "legacyCommittedTransactions": {
                mutation_id: committed_by_mutation[mutation_id]
                for mutation_id in sorted(committed_by_mutation)
            },
            "legacyRollbackTransactions": sorted(proof["rollbackTxIds"]),
            "legacyRollbackTargets": {
                tx_id: proof["rollbackTargetsByTx"][tx_id]
                for tx_id in sorted(proof["rollbackTargetsByTx"])
            },
            "legacyTransactionIds": sorted(proof["allTxIds"]),
            "rolledBackTxIds": rolled_back,
            "occurrences": [
                occurrences[key]
                for key in sorted(occurrences)
            ],
        }
        baseline["baselineDigest"] = _digest(baseline)
        candidate_rows = rows + [baseline]
        # 补铸本身也是一次耐久写：先以内存候选完整验证 receipt、
        # v1 映射、occurrence 与当前图，避免写入 baseline 后才发现
        # 某个 legacy hot receipt 无法形成可信历史。
        self._history_index_locked(candidate_rows, graph)
        _append_jsonl(self.journal_path, baseline)
        return candidate_rows

    def _materialize_history_projection(
        self,
        occurrences: dict[str, dict],
        projection: dict,
    ) -> dict:
        if (
            not isinstance(projection, dict)
            or projection.get("kind") != "page-brief-document-ref"
        ):
            raise ConceptNodeError(
                "KG history projection 无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        old_ref = str(projection.get("oldDocumentRef") or "")
        new_ref = str(projection.get("newDocumentRef") or "")
        if not old_ref or not new_ref or old_ref == new_ref:
            raise ConceptNodeError(
                "KG history PageBrief projection 无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        moves = []
        for key in sorted(occurrences):
            occurrence = occurrences[key]
            if (
                occurrence.get("kind") == "page-brief"
                and occurrence.get("documentRef") == old_ref
            ):
                updated = copy.deepcopy(occurrence)
                updated["documentRef"] = new_ref
                target_key = _canonical_json(updated)
                if target_key in occurrences and target_key != key:
                    raise ConceptNodeError(
                        "PageBrief rename 会合并两条历史证据，拒绝猜测 signal",
                        "BW_KG_NODE_HISTORY_PROJECTION_CONFLICT",
                        {
                            "nodeKey": occurrence.get("nodeKey"),
                            "oldDocumentRef": old_ref,
                            "newDocumentRef": new_ref,
                            "page": occurrence.get("page"),
                        },
                    )
                moves.append({
                    "from": copy.deepcopy(occurrence),
                    "to": updated,
                })
        materialized = {
            "kind": "page-brief-document-ref",
            "oldDocumentRef": old_ref,
            "newDocumentRef": new_ref,
            "scope": "all-source-occurrences",
            "moves": moves,
        }
        probe = copy.deepcopy(occurrences)
        self._apply_history_projection(probe, materialized)
        return materialized

    def _apply_history_projection(
        self,
        occurrences: dict[str, dict],
        projection: dict,
    ) -> None:
        if (
            not isinstance(projection, dict)
            or projection.get("kind") != "page-brief-document-ref"
        ):
            raise ConceptNodeError(
                "KG history projection 无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        old_ref = str(projection.get("oldDocumentRef") or "")
        new_ref = str(projection.get("newDocumentRef") or "")
        scope = str(projection.get("scope") or "")
        moves = projection.get("moves")
        if (
            not old_ref
            or not new_ref
            or old_ref == new_ref
            or scope not in {
                "all-source-occurrences",
                "explicit-moves",
            }
            or not isinstance(moves, list)
        ):
            raise ConceptNodeError(
                "KG history PageBrief projection 无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        migrated = []
        source_keys = set()
        target_keys = set()
        for move in moves:
            if not isinstance(move, dict):
                raise ConceptNodeError(
                    "KG history PageBrief projection move 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                )
            source = move.get("from")
            target = move.get("to")
            source_map = self._validated_history_occurrences(
                [source],
                label="projection.from",
            )
            target_map = self._validated_history_occurrences(
                [target],
                label="projection.to",
            )
            source_key, source_value = next(iter(source_map.items()))
            target_key, target_value = next(iter(target_map.items()))
            expected_target = copy.deepcopy(source_value)
            expected_target["documentRef"] = new_ref
            if (
                source_value.get("kind") != "page-brief"
                or source_value.get("documentRef") != old_ref
                or target_value != expected_target
                or source_key in source_keys
                or target_key in target_keys
            ):
                raise ConceptNodeError(
                    "KG history PageBrief projection move 不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                )
            source_keys.add(source_key)
            target_keys.add(target_key)
            migrated.append(
                (source_key, target_key, source_value, target_value)
            )
        if len(target_keys) != len(migrated):
            raise ConceptNodeError(
                "PageBrief rename 的历史证据投影不唯一",
                "BW_KG_NODE_HISTORY_PROJECTION_CONFLICT",
                {"oldDocumentRef": old_ref, "newDocumentRef": new_ref},
            )
        expected_source_keys = {
            key
            for key, occurrence in occurrences.items()
            if (
                occurrence.get("kind") == "page-brief"
                and occurrence.get("documentRef") == old_ref
            )
        }
        if (
            scope == "all-source-occurrences"
            and source_keys != expected_source_keys
        ):
            raise ConceptNodeError(
                "PageBrief rename 的历史证据 move 不完整",
                "BW_KG_NODE_JOURNAL_CORRUPT",
                {"oldDocumentRef": old_ref, "newDocumentRef": new_ref},
            )
        for source_key, target_key, source, _ in migrated:
            if occurrences.get(source_key) != source:
                raise ConceptNodeError(
                    "PageBrief rename 的历史来源证据不存在",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"oldDocumentRef": old_ref, "newDocumentRef": new_ref},
                )
            if (
                target_key in occurrences
                and target_key not in source_keys
            ):
                raise ConceptNodeError(
                    "PageBrief rename 会合并两条历史证据，拒绝猜测 signal",
                    "BW_KG_NODE_HISTORY_PROJECTION_CONFLICT",
                    {
                        "nodeKey": source.get("nodeKey"),
                        "oldDocumentRef": old_ref,
                        "newDocumentRef": new_ref,
                        "page": source.get("page"),
                    },
                )
        for source_key in source_keys:
            occurrences.pop(source_key)
        for _, target_key, _, target in migrated:
            occurrences[target_key] = target

    def _history_index_locked(
        self,
        rows: list[dict],
        graph: dict | None = None,
    ) -> dict:
        baseline_indexes = [
            index
            for index, row in enumerate(rows)
            if row.get("phase") == "history-baseline"
        ]
        if len(baseline_indexes) != 1:
            raise ConceptNodeError(
                "KG history baseline 缺失或重复",
                "BW_KG_NODE_HISTORY_INCOMPLETE",
            )
        baseline_index = baseline_indexes[0]
        baseline = rows[baseline_index]
        baseline_body = {
            key: value
            for key, value in baseline.items()
            if key != "baselineDigest"
        }
        if (
            baseline.get("historyContract") != HISTORY_CONTRACT
            or baseline.get("journalPrefixDigest")
            != _digest(rows[:baseline_index])
            or str(baseline.get("baselineDigest") or "")
            != _digest(baseline_body)
        ):
            raise ConceptNodeError(
                "KG history baseline 前缀证明不一致",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        raw_occurrences = baseline.get("occurrences")
        raw_receipts = baseline.get("receipts")
        if not isinstance(raw_occurrences, list) or not isinstance(
            raw_receipts,
            dict,
        ):
            raise ConceptNodeError(
                "KG history baseline 内容无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        occurrences = self._validated_history_occurrences(
            raw_occurrences,
            label="baseline",
        )
        receipts = copy.deepcopy(raw_receipts)
        for mutation_id, receipt in receipts.items():
            if (
                not str(mutation_id)
                or not isinstance(receipt, dict)
                or not isinstance(receipt.get("result"), dict)
            ):
                raise ConceptNodeError(
                    "KG history baseline receipt 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id},
                )
        raw_legacy_mutations = baseline.get("legacyMutationIds")
        if (
            not isinstance(raw_legacy_mutations, list)
            or len(raw_legacy_mutations)
            != len({str(value) for value in raw_legacy_mutations})
        ):
            raise ConceptNodeError(
                "KG history baseline legacy mutation 列表无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        legacy_mutations = {
            str(value)
            for value in raw_legacy_mutations
            if str(value)
        }
        raw_legacy_transactions = baseline.get(
            "legacyCommittedTransactions"
        )
        if not isinstance(raw_legacy_transactions, dict):
            raise ConceptNodeError(
                "KG history baseline 缺少 legacy transaction 映射",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        mutation_tx_ids = {
            str(mutation_id): str(tx_id)
            for mutation_id, tx_id in raw_legacy_transactions.items()
            if str(mutation_id) and str(tx_id)
        }
        if (
            set(mutation_tx_ids) != legacy_mutations
            or len(set(mutation_tx_ids.values())) != len(mutation_tx_ids)
        ):
            raise ConceptNodeError(
                "KG history baseline 的 legacy transaction 映射不完整",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        for mutation_id, receipt in receipts.items():
            result = receipt.get("result") if isinstance(receipt, dict) else {}
            if (
                mutation_id not in mutation_tx_ids
                or not isinstance(result, dict)
                or str(result.get("txId") or "")
                != mutation_tx_ids[mutation_id]
                or str(result.get("mutationId") or "") != mutation_id
                or str(result.get("contract") or "") != CONTRACT
            ):
                raise ConceptNodeError(
                    "KG history baseline receipt 与 legacy transaction 不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id},
                )
        raw_rolled_back = baseline.get("rolledBackTxIds")
        if (
            not isinstance(raw_rolled_back, list)
            or len(raw_rolled_back)
            != len({str(value) for value in raw_rolled_back})
        ):
            raise ConceptNodeError(
                "KG history baseline rolled-back 列表无效",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        rolled_back = {
            str(value)
            for value in raw_rolled_back
            if str(value)
        }
        raw_rollback_transactions = baseline.get(
            "legacyRollbackTransactions"
        )
        if not isinstance(raw_rollback_transactions, list):
            raise ConceptNodeError(
                "KG history baseline 缺少 rollback transaction 映射",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        if len(raw_rollback_transactions) != len({
            str(value) for value in raw_rollback_transactions
        }):
            raise ConceptNodeError(
                "KG history baseline rollback transaction 重复",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        rollback_tx_ids = {
            str(value)
            for value in raw_rollback_transactions
            if str(value)
        }
        raw_rollback_targets = baseline.get("legacyRollbackTargets")
        if not isinstance(raw_rollback_targets, dict):
            raise ConceptNodeError(
                "KG history baseline 缺少 rollback target 映射",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        rollback_targets_by_tx = {
            str(tx_id): str(target_tx_id)
            for tx_id, target_tx_id in raw_rollback_targets.items()
            if str(tx_id) and str(target_tx_id)
        }
        if set(rollback_targets_by_tx) != rollback_tx_ids:
            raise ConceptNodeError(
                "KG history baseline rollback target 映射不完整",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        raw_legacy_tx_ids = baseline.get("legacyTransactionIds")
        if (
            not isinstance(raw_legacy_tx_ids, list)
            or len(raw_legacy_tx_ids)
            != len({str(value) for value in raw_legacy_tx_ids})
        ):
            raise ConceptNodeError(
                "KG history baseline 缺少完整 legacy transaction 列表",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        legacy_tx_ids = {
            str(value)
            for value in raw_legacy_tx_ids
            if str(value)
        }
        committed_tx_ids = set(mutation_tx_ids.values())
        prefix_proof = self._prove_v1_journal_for_baseline(
            rows[:baseline_index],
            graph_digest=str(baseline.get("graphDigest") or ""),
        )
        if (
            prefix_proof["committedByMutation"] != mutation_tx_ids
            or prefix_proof["rolledBackTxIds"] != rolled_back
            or prefix_proof["rollbackTxIds"] != rollback_tx_ids
            or prefix_proof["rollbackTargetsByTx"]
            != rollback_targets_by_tx
            or prefix_proof["allTxIds"] != legacy_tx_ids
            or not rollback_tx_ids <= committed_tx_ids
            or not rolled_back <= committed_tx_ids
            or rollback_tx_ids & rolled_back
        ):
            raise ConceptNodeError(
                "KG history baseline 与 legacy journal 证明不一致",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
        for mutation_id, receipt in receipts.items():
            tx_id = mutation_tx_ids[mutation_id]
            result = receipt["result"]
            rollback_target = rollback_targets_by_tx.get(tx_id)
            request_digest = str(receipt.get("requestDigest") or "")
            operation_contract = str(
                receipt.get("operationContract") or ""
            )
            if (
                not re.fullmatch(r"[0-9a-f]{64}", request_digest)
                or not operation_contract
                or (
                    rollback_target
                    and (
                        operation_contract != "kg-op/rollback/1"
                        or str(result.get("rollbackOf") or "")
                        != rollback_target
                    )
                )
                or (
                    not rollback_target
                    and (
                        operation_contract == "kg-op/rollback/1"
                        or result.get("rollbackOf") not in (None, "")
                    )
                )
            ):
                raise ConceptNodeError(
                    "KG history baseline receipt 缺少 v1 事务语义绑定",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id, "txId": tx_id},
                )
        prepared = {}
        terminal = set()
        all_tx_ids = set(legacy_tx_ids)
        post_baseline_committed_tx_ids: set[str] = set()
        open_tx = ""
        last_seq = 0
        last_tx = ""
        last_graph_digest = str(baseline.get("graphDigest") or "")
        if not last_graph_digest:
            raise ConceptNodeError(
                "KG history baseline 缺少图摘要",
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )

        def register_receipt(mutation_id: str, receipt: dict) -> None:
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("result"), dict)
            ):
                raise ConceptNodeError(
                    "KG history receipt 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id},
                )
            existing = receipts.get(mutation_id)
            if existing is not None and existing != receipt:
                raise ConceptNodeError(
                    "KG mutationId 对应多个历史请求",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id},
                )
            receipts[mutation_id] = copy.deepcopy(receipt)

        for row in rows[baseline_index + 1:]:
            phase = str(row.get("phase") or "")
            tx_id = str(row.get("txId") or "")
            if phase == "prepare":
                if (
                    not tx_id
                    or tx_id in all_tx_ids
                    or open_tx
                ):
                    raise ConceptNodeError(
                        "KG history prepare 事务身份重复或缺失",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                history = row.get("history")
                prepare_body = {
                    key: value
                    for key, value in row.items()
                    if key != "prepareDigest"
                }
                if (
                    not isinstance(history, dict)
                    or str(row.get("prepareDigest") or "")
                    != _digest(prepare_body)
                    or str(row.get("graphBeforeDigest") or "")
                    != last_graph_digest
                ):
                    raise ConceptNodeError(
                        "KG history prepare 摘要或前图链不一致",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                prepared[tx_id] = row
                all_tx_ids.add(tx_id)
                open_tx = tx_id
                continue
            if phase in {"abort", "conflict"}:
                prepare = prepared.get(tx_id)
                history = (
                    prepare.get("history")
                    if isinstance(prepare, dict) else None
                )
                if (
                    not tx_id
                    or prepare is None
                    or open_tx != tx_id
                    or tx_id in terminal
                    or str(row.get("mutationId") or "")
                    != str(prepare.get("mutationId") or "")
                    or not isinstance(history, dict)
                    or str(row.get("prepareDigest") or "")
                    != str(prepare.get("prepareDigest") or "")
                    or str(row.get("historyDigest") or "")
                    != str(prepare.get("historyDigest") or "")
                    or (
                        phase == "abort"
                        and str(row.get("graphDigest") or "")
                        != str(prepare.get("graphBeforeDigest") or "")
                    )
                    or (
                        phase == "conflict"
                        and not re.fullmatch(
                            r"[0-9a-f]{64}",
                            str(row.get("graphDigest") or ""),
                        )
                    )
                ):
                    raise ConceptNodeError(
                        "KG history terminal 缺少唯一 prepare",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "phase": phase},
                    )
                terminal.add(tx_id)
                open_tx = ""
                continue
            if phase != "commit":
                raise ConceptNodeError(
                    "KG history 含未知 phase",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id, "phase": phase},
                )
            prepare = prepared.get(tx_id)
            history = (
                prepare.get("history")
                if isinstance(prepare, dict) else None
            )
            if (
                not isinstance(history, dict)
                or history.get("contract") != HISTORY_CONTRACT
                or history.get("mutationKind") not in {
                    "node-upsert",
                    "graph-mutation",
                    "rollback",
                }
                or open_tx != tx_id
                or tx_id in terminal
                or str(row.get("mutationId") or "")
                != str((prepare or {}).get("mutationId") or "")
            ):
                raise ConceptNodeError(
                    "baseline 后的 KG commit 缺少 history 证明",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            try:
                sequence = int(history.get("sequence"))
            except (TypeError, ValueError) as exc:
                raise ConceptNodeError(
                    "KG history sequence 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                ) from exc
            previous_tx = str(history.get("previousTx") or "")
            if sequence != last_seq + 1 or previous_tx != last_tx:
                raise ConceptNodeError(
                    "KG history 链不连续",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {
                        "txId": tx_id,
                        "sequence": sequence,
                        "expectedSequence": last_seq + 1,
                        "previousTx": previous_tx,
                        "expectedPreviousTx": last_tx,
                    },
                )
            mutation_id = str(prepare.get("mutationId") or "")
            history_receipt = history.get("receipt")
            history_digest = _digest(history)
            commit_extra = history.get("commitExtra")
            if (
                not isinstance(commit_extra, dict)
                or str(prepare.get("historyDigest") or "") != history_digest
                or str(row.get("historyDigest") or "") != history_digest
                or str(row.get("prepareDigest") or "")
                != str(prepare.get("prepareDigest") or "")
                or prepare.get("commitExtra") != commit_extra
                or row.get("historySequence") != sequence
                or str(row.get("requestDigest") or "")
                != str((history_receipt or {}).get("requestDigest") or "")
                or str(row.get("resultDigest") or "")
                != str(history.get("resultDigest") or "")
                or _digest((history_receipt or {}).get("result"))
                != str(history.get("resultDigest") or "")
                or str(row.get("graphDigest") or "")
                != str(prepare.get("graphAfterDigest") or "")
                or str(prepare.get("graphBeforeDigest") or "")
                != last_graph_digest
                or not isinstance(history_receipt, dict)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(history_receipt.get("requestDigest") or ""),
                )
                or not str(history_receipt.get("operationContract") or "")
                or not isinstance(history_receipt.get("result"), dict)
                or str(history_receipt["result"].get("txId") or "") != tx_id
                or str(history_receipt["result"].get("mutationId") or "")
                != mutation_id
                or str(history_receipt["result"].get("contract") or "")
                != CONTRACT
            ):
                raise ConceptNodeError(
                    "KG history commit 与 prepare 证明不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            for key, value in commit_extra.items():
                if row.get(key) != value:
                    raise ConceptNodeError(
                        "KG history commitExtra 与 prepare 不一致",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "key": key},
                    )
            if (
                "rollbackOf" not in commit_extra
                and row.get("rollbackOf") not in (None, "")
            ):
                raise ConceptNodeError(
                    "KG history commit 含未绑定 rollbackOf",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            existing_tx = mutation_tx_ids.get(mutation_id)
            if existing_tx is not None and existing_tx != tx_id:
                raise ConceptNodeError(
                    "KG mutationId 对应多个 transaction",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"mutationId": mutation_id},
                )
            removed_occurrences = self._validated_history_occurrences(
                history.get("occurrencesRemoved"),
                label="occurrencesRemoved",
            )
            added_occurrences = self._validated_history_occurrences(
                history.get("occurrencesAdded"),
                label="occurrencesAdded",
            )
            evicted_occurrences = self._validated_history_occurrences(
                history.get("occurrenceEvictions"),
                label="occurrenceEvictions",
            )
            before_nodes = prepare.get("beforeNodes")
            after_nodes = prepare.get("afterNodes")
            projections = history.get("projections")
            if (
                not isinstance(before_nodes, dict)
                or not isinstance(after_nodes, dict)
                or set(before_nodes) != set(after_nodes)
                or not isinstance(projections, list)
            ):
                raise ConceptNodeError(
                    "KG history 节点快照或 projection 数组无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            if (
                history.get("mutationKind") != "rollback"
                and any(
                    isinstance(projection, dict)
                    and projection.get("scope") == "explicit-moves"
                    for projection in projections
                )
            ):
                raise ConceptNodeError(
                    "只有 rollback 可使用显式 occurrence move",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            if (
                history.get("mutationKind") == "node-upsert"
                and projections
            ):
                raise ConceptNodeError(
                    "node-upsert 不得携带 history projection",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            before_counts = {}
            for occurrence in occurrences.values():
                node_key = str(occurrence.get("nodeKey") or "")
                before_counts[node_key] = before_counts.get(node_key, 0) + 1
            for node_key, node in before_nodes.items():
                if node is None:
                    expected_signal = 0
                elif isinstance(node, dict):
                    raw_signal = node.get("signal", 0)
                    if type(raw_signal) is not int or raw_signal < 0:
                        raise ConceptNodeError(
                            "KG history beforeNodes signal 无效",
                            "BW_KG_NODE_JOURNAL_CORRUPT",
                            {"txId": tx_id, "nodeKey": node_key},
                        )
                    expected_signal = raw_signal
                else:
                    raise ConceptNodeError(
                        "KG history beforeNodes 节点无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "nodeKey": node_key},
                    )
                if before_counts.get(node_key, 0) != expected_signal:
                    raise ConceptNodeError(
                        "KG history beforeNodes signal 缺少 occurrence 证明",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {
                            "txId": tx_id,
                            "nodeKey": node_key,
                            "signal": expected_signal,
                            "occurrences": before_counts.get(node_key, 0),
                        },
                    )
            snapshot_before_occurrences = _occurrence_map_for_nodes(
                before_nodes
            )
            snapshot_after_occurrences = _occurrence_map_for_nodes(
                after_nodes
            )
            projection_target_keys = {
                _canonical_json(move.get("to"))
                for projection in projections
                if isinstance(projection, dict)
                for move in (
                    projection.get("moves")
                    if isinstance(projection.get("moves"), list)
                    else []
                )
                if isinstance(move, dict) and isinstance(move.get("to"), dict)
            }
            projection_source_keys = {
                _canonical_json(move.get("from"))
                for projection in projections
                if isinstance(projection, dict)
                for move in (
                    projection.get("moves")
                    if isinstance(projection.get("moves"), list)
                    else []
                )
                if (
                    isinstance(move, dict)
                    and isinstance(move.get("from"), dict)
                )
            }
            expected_added_keys = (
                set(snapshot_after_occurrences)
                - set(snapshot_before_occurrences)
            )
            expected_removed_keys = (
                set(snapshot_before_occurrences)
                - set(snapshot_after_occurrences)
            )
            if (
                set(removed_occurrences)
                | set(evicted_occurrences)
                | (projection_source_keys & expected_removed_keys)
                != expected_removed_keys
                or bool(
                    set(removed_occurrences) & set(evicted_occurrences)
                )
                or bool(
                    set(evicted_occurrences) & projection_source_keys
                )
                or not set(added_occurrences) <= expected_added_keys
                or not expected_added_keys <= (
                    set(added_occurrences) | projection_target_keys
                )
                or (
                    history.get("mutationKind") == "node-upsert"
                    and bool(removed_occurrences)
                )
                or (
                    history.get("mutationKind") != "node-upsert"
                    and bool(evicted_occurrences)
                )
            ):
                raise ConceptNodeError(
                    "KG history occurrence 增删与节点快照不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            for occurrence_key in evicted_occurrences:
                if occurrence_key not in occurrences:
                    raise ConceptNodeError(
                        "KG history 淘汰了不存在的 display occurrence",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
            for occurrence_key in removed_occurrences:
                if occurrence_key not in occurrences:
                    raise ConceptNodeError(
                        "KG history 删除了不存在的 occurrence",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                occurrences.pop(occurrence_key)
            for projection in projections:
                self._apply_history_projection(occurrences, projection)
            for occurrence_key, occurrence in added_occurrences.items():
                if occurrence_key in occurrences:
                    raise ConceptNodeError(
                        "KG history 重复加入已存在的 occurrence",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                occurrences[occurrence_key] = occurrence
            rollback_of = str(commit_extra.get("rollbackOf") or "")
            mutation_kind = str(history.get("mutationKind") or "")
            if bool(rollback_of) != (mutation_kind == "rollback"):
                raise ConceptNodeError(
                    "KG rollback mutationKind 与 rollbackOf 不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id, "rollbackOf": rollback_of},
                )
            if rollback_of:
                receipt_contract = str(
                    (history_receipt or {}).get("operationContract") or ""
                )
                receipt_result = (
                    (history_receipt or {}).get("result")
                    if isinstance(history_receipt, dict) else {}
                )
                if (
                    rollback_of == tx_id
                    or rollback_of not in post_baseline_committed_tx_ids
                    or rollback_of in rollback_tx_ids
                    or rollback_of in rolled_back
                    or receipt_contract != "kg-op/rollback/1"
                    or not isinstance(receipt_result, dict)
                    or str(receipt_result.get("rollbackOf") or "")
                    != rollback_of
                ):
                    raise ConceptNodeError(
                        "KG history rollback 目标或合同无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "rollbackOf": rollback_of},
                    )
                target_history = (
                    (prepared.get(rollback_of) or {}).get("history")
                    if isinstance(prepared.get(rollback_of), dict)
                    else None
                )
                target_prepare = prepared.get(rollback_of) or {}
                target_before_nodes = target_prepare.get("beforeNodes")
                target_after_nodes = target_prepare.get("afterNodes")
                target_projections = (
                    target_history.get("projections")
                    if isinstance(target_history, dict) else None
                )
                receipt_keys = receipt_result.get("keys")
                if (
                    not isinstance(target_before_nodes, dict)
                    or not isinstance(target_after_nodes, dict)
                    or set(target_before_nodes) != set(target_after_nodes)
                    or not isinstance(target_projections, list)
                    or (
                        not target_after_nodes
                        and not target_projections
                    )
                    or set(before_nodes) != set(target_after_nodes)
                    or before_nodes != target_after_nodes
                    or not isinstance(receipt_keys, list)
                    or receipt_keys != sorted(target_after_nodes)
                ):
                    raise ConceptNodeError(
                        "KG rollback 节点范围未与目标事务快照绑定",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "rollbackOf": rollback_of},
                    )
                for node_key, target_before in target_before_nodes.items():
                    rollback_after = after_nodes.get(node_key)
                    if target_before is not None:
                        valid_inverse = rollback_after == target_before
                    else:
                        valid_inverse = isinstance(rollback_after, dict)
                        tombstone = (
                            rollback_after.get("tombstone")
                            if isinstance(rollback_after, dict) else None
                        )
                        tombstone_ts = (
                            tombstone.get("ts")
                            if isinstance(tombstone, dict) else None
                        )
                        updated_at = (
                            rollback_after.get("updatedAt")
                            if isinstance(rollback_after, dict) else None
                        )
                        if (
                            not isinstance(tombstone, dict)
                            or type(tombstone_ts) is not int
                            or type(updated_at) is not int
                            or tombstone_ts != updated_at
                        ):
                            valid_inverse = False
                        else:
                            expected_tombstone = copy.deepcopy(
                                target_after_nodes[node_key]
                            )
                            expected_tombstone["deleted"] = True
                            expected_tombstone["status"] = "rolled_back"
                            expected_tombstone["tombstone"] = {
                                "rollbackOf": rollback_of,
                                "mutationId": mutation_id,
                                "ts": tombstone_ts,
                            }
                            expected_tombstone["updatedAt"] = updated_at
                            valid_inverse = rollback_after == expected_tombstone
                    if not valid_inverse:
                        raise ConceptNodeError(
                            "KG rollback 节点变换不是目标事务的精确逆变换",
                            "BW_KG_NODE_JOURNAL_CORRUPT",
                            {
                                "txId": tx_id,
                                "rollbackOf": rollback_of,
                                "nodeKey": node_key,
                            },
                        )
                expected_inverse = []
                for projection in reversed(
                    (target_history or {}).get("projections") or []
                ):
                    expected_inverse.append({
                        "kind": "page-brief-document-ref",
                        "oldDocumentRef": projection.get("newDocumentRef"),
                        "newDocumentRef": projection.get("oldDocumentRef"),
                        "scope": "explicit-moves",
                        "moves": [
                            {
                                "from": copy.deepcopy(move.get("to")),
                                "to": copy.deepcopy(move.get("from")),
                            }
                            for move in reversed(
                                projection.get("moves") or []
                            )
                            if isinstance(move, dict)
                        ],
                    })
                if projections != expected_inverse:
                    raise ConceptNodeError(
                        "KG rollback 历史投影不是目标事务的精确逆变换",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "rollbackOf": rollback_of},
                    )
                rolled_back.add(rollback_of)
                rollback_tx_ids.add(tx_id)
            elif (
                str((history_receipt or {}).get("operationContract") or "")
                == "kg-op/rollback/1"
                or (
                    isinstance((history_receipt or {}).get("result"), dict)
                    and (history_receipt or {}).get("result", {}).get(
                        "rollbackOf"
                    )
                )
            ):
                raise ConceptNodeError(
                    "KG rollback receipt 缺少绑定的 rollbackOf",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            after_counts = {}
            for occurrence in occurrences.values():
                node_key = str(occurrence.get("nodeKey") or "")
                after_counts[node_key] = after_counts.get(node_key, 0) + 1
            for node_key, node in after_nodes.items():
                if node is None:
                    expected_signal = 0
                elif isinstance(node, dict):
                    raw_signal = node.get("signal", 0)
                    if type(raw_signal) is not int or raw_signal < 0:
                        raise ConceptNodeError(
                            "KG history afterNodes signal 无效",
                            "BW_KG_NODE_JOURNAL_CORRUPT",
                            {"txId": tx_id, "nodeKey": node_key},
                        )
                    expected_signal = raw_signal
                else:
                    raise ConceptNodeError(
                        "KG history afterNodes 节点无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id, "nodeKey": node_key},
                    )
                if after_counts.get(node_key, 0) != expected_signal:
                    raise ConceptNodeError(
                        "KG history afterNodes signal 缺少 occurrence 证明",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {
                            "txId": tx_id,
                            "nodeKey": node_key,
                            "signal": expected_signal,
                            "occurrences": after_counts.get(node_key, 0),
                        },
                    )
            register_receipt(mutation_id, history_receipt)
            mutation_tx_ids[mutation_id] = tx_id
            last_seq = sequence
            last_tx = tx_id
            last_graph_digest = str(row.get("graphDigest") or "")
            committed_tx_ids.add(tx_id)
            post_baseline_committed_tx_ids.add(tx_id)
            terminal.add(tx_id)
            open_tx = ""
        unfinished = sorted(set(prepared) - terminal)
        if unfinished:
            raise ConceptNodeError(
                "KG history 留有未完成 prepare",
                "BW_KG_NODE_JOURNAL_CORRUPT",
                {"txIds": unfinished[-10:]},
            )
        index = {
            "receipts": receipts,
            "legacyMutationIds": legacy_mutations,
            "rolledBackTxIds": rolled_back,
            "occurrences": occurrences,
            "lastSequence": last_seq,
            "lastTx": last_tx,
            "lastGraphDigest": last_graph_digest,
            "mutationTxIds": mutation_tx_ids,
            "legacyTransactionIds": legacy_tx_ids,
            "allTransactionIds": all_tx_ids,
            "postBaselineCommittedTxIds": post_baseline_committed_tx_ids,
        }
        if graph is not None:
            graph_nodes = graph.get("nodes")
            graph_meta = graph.get("meta")
            if not isinstance(graph_nodes, dict):
                raise ConceptNodeError(
                    "当前 KG 图 nodes 结构无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                )
            graph_occurrence_counts = {}
            for occurrence in occurrences.values():
                node_key = str(occurrence.get("nodeKey") or "")
                if not isinstance(graph_nodes.get(node_key), dict):
                    raise ConceptNodeError(
                        "KG occurrence 指向不存在的当前节点",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"nodeKey": node_key},
                    )
                graph_occurrence_counts[node_key] = (
                    graph_occurrence_counts.get(node_key, 0) + 1
                )
            for node_key, node in graph_nodes.items():
                if not isinstance(node, dict):
                    raise ConceptNodeError(
                        "当前 KG 图节点结构无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"nodeKey": node_key},
                    )
                raw_signal = node.get("signal", 0)
                if type(raw_signal) is not int or raw_signal < 0:
                    raise ConceptNodeError(
                        "当前 KG 节点 signal 无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"nodeKey": node_key},
                    )
                signal = raw_signal
                if graph_occurrence_counts.get(node_key, 0) != signal:
                    raise ConceptNodeError(
                        "当前 KG 节点 signal 缺少 durable occurrence 证明",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {
                            "nodeKey": node_key,
                            "signal": signal,
                            "occurrences": graph_occurrence_counts.get(
                                node_key,
                                0,
                            ),
                        },
                    )
            hot_receipts = (
                graph_meta.get("node_mutations")
                if isinstance(graph_meta, dict) else {}
            )
            if hot_receipts is None:
                hot_receipts = {}
            if not isinstance(hot_receipts, dict):
                raise ConceptNodeError(
                    "当前 KG hot receipt ledger 无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                )
            for mutation_id, hot_receipt in hot_receipts.items():
                cold_receipt = receipts.get(str(mutation_id))
                # v1 补铸时无法证明的 legacy hot receipt 可以保留作
                # 人工取证，但绝不能作为成功重放依据。凡已存在可信
                # cold receipt，hot 副本必须逐字段完全一致。
                if (
                    cold_receipt is not None
                    and (
                        not isinstance(hot_receipt, dict)
                        or _receipt_record(hot_receipt) != cold_receipt
                    )
                ):
                    raise ConceptNodeError(
                        "当前 KG hot receipt 与 durable history 不一致",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"mutationId": str(mutation_id)},
                    )
            current_graph_digest = _digest(graph)
            head = (
                (graph.get("meta") or {}).get("kg_history")
                if isinstance(graph.get("meta"), dict) else None
            )
            if last_seq == 0:
                if head not in (None, {}):
                    raise ConceptNodeError(
                        "KG graph head 无 journal 证明",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                    )
            elif not isinstance(head, dict) or (
                head.get("contract") != HISTORY_CONTRACT
                or int(head.get("lastSequence") or 0) != last_seq
                or str(head.get("lastTx") or "") != last_tx
            ):
                raise ConceptNodeError(
                    "KG graph head 与 journal history 不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"journalTx": last_tx, "journalSequence": last_seq},
                )
            if current_graph_digest != last_graph_digest:
                raise ConceptNodeError(
                    "当前 KG 图摘要与 journal history head 不一致",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {
                        "graphDigest": current_graph_digest,
                        "journalDigest": last_graph_digest,
                    },
                )
        return index

    def _history_replay(
        self,
        *,
        history: dict,
        mutation_map: dict,
        mutation_id: str,
        request_digest: str,
        operation_contract: str,
    ) -> dict | None:
        receipt = history["receipts"].get(mutation_id)
        if receipt is None:
            if mutation_id in history["legacyMutationIds"]:
                raise ConceptNodeError(
                    "旧 mutation 缺少可验证的请求摘要，拒绝猜测重放",
                    "BW_KG_NODE_HISTORY_LEGACY_MUTATION",
                    {"mutationId": mutation_id},
                )
            return None
        stored_digest = str(receipt.get("requestDigest") or "")
        stored_contract = str(receipt.get("operationContract") or "")
        if not stored_digest or not stored_contract:
            raise ConceptNodeError(
                "旧 mutation receipt 无法证明请求身份",
                "BW_KG_NODE_HISTORY_LEGACY_MUTATION",
                {"mutationId": mutation_id},
            )
        if (
            stored_digest != request_digest
            or stored_contract != operation_contract
        ):
            raise ConceptNodeError(
                "mutationId 已绑定到不同 KG 请求",
                "BW_KG_NODE_MUTATION_REUSE",
                {
                    "mutationId": mutation_id,
                    "storedContract": stored_contract,
                    "requestedContract": operation_contract,
                },
            )
        result = copy.deepcopy(receipt["result"])
        tx_id = str(result.get("txId") or "")
        if tx_id and tx_id in history["rolledBackTxIds"]:
            raise ConceptNodeError(
                "原 KG mutation 已回滚，拒绝伪报旧成功",
                "BW_KG_NODE_MUTATION_ROLLED_BACK",
                {"mutationId": mutation_id, "txId": tx_id},
            )
        result["replay"] = True
        result["coldReplay"] = mutation_id not in mutation_map
        return result

    def _recover_locked(self, graph: dict) -> list[dict]:
        rows = self._journal_rows(repair_torn_tail=True)
        prepared = {}
        terminal = {}
        for index, row in enumerate(rows):
            tx_id = str(row.get("txId") or "")
            phase = str(row.get("phase") or "")
            if phase == "history-baseline":
                continue
            if phase == "prepare":
                if not tx_id or tx_id in prepared or tx_id in terminal:
                    raise ConceptNodeError(
                        "KG journal prepare 事务身份重复或缺失",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"line": index + 1, "txId": tx_id},
                    )
                prepared[tx_id] = row
            elif phase in {"commit", "abort", "conflict"}:
                if (
                    not tx_id
                    or tx_id not in prepared
                    or tx_id in terminal
                    or str(row.get("mutationId") or "")
                    != str(prepared[tx_id].get("mutationId") or "")
                ):
                    raise ConceptNodeError(
                        "KG journal terminal 缺少唯一 prepare",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"line": index + 1, "txId": tx_id, "phase": phase},
                    )
                terminal[tx_id] = phase
            else:
                raise ConceptNodeError(
                    "KG journal 含未知 phase",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"line": index + 1, "txId": tx_id, "phase": phase},
                )
        unfinished = [
            (tx_id, row)
            for tx_id, row in prepared.items()
            if tx_id not in terminal
        ]
        if len(unfinished) > 1:
            raise ConceptNodeError(
                "KG journal 同时存在多个未完成 prepare，拒绝猜测恢复顺序",
                "BW_KG_NODE_JOURNAL_CONFLICT",
                {"txIds": [tx_id for tx_id, _ in unfinished]},
            )
        has_baseline = any(
            row.get("phase") == "history-baseline"
            for row in rows
        )
        if has_baseline:
            unfinished_ids = {tx_id for tx_id, _ in unfinished}
            stable_rows = [
                row
                for row in rows
                if not (
                    row.get("phase") == "prepare"
                    and str(row.get("txId") or "") in unfinished_ids
                )
            ]
            # 任何恢复写入前，先证明既有完整历史。否则一次合法的
            # unfinished prepare 会掩盖更早的 baseline/history 损坏，
            # 使 recover() 先追加 terminal、下一次调用才失败。
            self._history_index_locked(
                stable_rows,
                None if any(
                    row.get("phase") == "conflict"
                    for row in stable_rows
                ) else graph if not unfinished else None,
            )
        elif unfinished:
            unfinished_ids = {tx_id for tx_id, _ in unfinished}
            stable_rows = [
                row
                for row in rows
                if not (
                    row.get("phase") == "prepare"
                    and str(row.get("txId") or "") in unfinished_ids
                )
            ]
            # v1 也必须先证明 unfinished 之前的完整串行前缀，才能
            # 追加 recovery terminal。当前图可能已在 prepare 前/后，
            # 因而以前图摘要作为稳定前缀的终点证明。
            self._prove_v1_journal_for_baseline(
                stable_rows,
                graph_digest=str(
                    unfinished[0][1].get("graphBeforeDigest") or ""
                ),
            )
        current_digest = _digest(graph)
        recovered = []
        for tx_id, row in unfinished:
            history = row.get("history")
            if not isinstance(history, dict) and (
                row.get("contract") != LOG_CONTRACT
                or row.get("phase") != "prepare"
                or not str(row.get("mutationId") or "")
                or not isinstance(row.get("beforeNodes"), dict)
                or not isinstance(row.get("afterNodes"), dict)
                or set(row.get("beforeNodes") or {})
                != set(row.get("afterNodes") or {})
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(row.get("graphBeforeDigest") or ""),
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(row.get("graphAfterDigest") or ""),
                )
                or row.get("history") is not None
            ):
                raise ConceptNodeError(
                    "KG unfinished v1 prepare 结构无效",
                    "BW_KG_NODE_JOURNAL_CORRUPT",
                    {"txId": tx_id},
                )
            if isinstance(history, dict):
                prepare_body = {
                    key: value
                    for key, value in row.items()
                    if key != "prepareDigest"
                }
                if str(row.get("prepareDigest") or "") != _digest(
                    prepare_body
                ):
                    raise ConceptNodeError(
                        "KG unfinished prepare 摘要不一致",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
            if current_digest == row.get("graphAfterDigest"):
                phase = "commit"
                reason = "recovered-after-atomic-replace"
            elif current_digest == row.get("graphBeforeDigest"):
                phase = "abort"
                reason = "recovered-before-atomic-replace"
            else:
                phase = "conflict"
                reason = "graph-changed-after-unfinished-prepare"
            result = {
                "contract": LOG_CONTRACT,
                "phase": phase,
                "txId": tx_id,
                "mutationId": row.get("mutationId"),
                "ts": int(self.clock()),
                "recovery": True,
                "reason": reason,
                "graphDigest": current_digest,
            }
            if isinstance(history, dict):
                result["historyDigest"] = _digest(history)
                result["prepareDigest"] = row.get("prepareDigest")
            if phase == "commit":
                if isinstance(history, dict):
                    result["historySequence"] = history.get("sequence")
                    result["requestDigest"] = (
                        (history.get("receipt") or {}).get("requestDigest")
                        if isinstance(history.get("receipt"), dict) else ""
                    )
                    result["resultDigest"] = history.get("resultDigest")
                    if str(row.get("historyDigest") or "") != result["historyDigest"]:
                        raise ConceptNodeError(
                            "KG prepare 的 history 摘要不一致",
                            "BW_KG_NODE_JOURNAL_CORRUPT",
                            {"txId": tx_id},
                        )
                commit_extra = row.get("commitExtra") or {}
                if not isinstance(commit_extra, dict):
                    raise ConceptNodeError(
                        "KG prepare 的 commitExtra 无效",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                if isinstance(history, dict) and history.get(
                    "commitExtra"
                ) != commit_extra:
                    raise ConceptNodeError(
                        "KG prepare 的 commitExtra 未被 history 绑定",
                        "BW_KG_NODE_JOURNAL_CORRUPT",
                        {"txId": tx_id},
                    )
                for key, value in commit_extra.items():
                    if key not in result:
                        result[key] = value
            if has_baseline:
                # 用尚未写入的 terminal 先完整跑一遍 schema、history、
                # occurrence 与 rollback 语义。验证通过后才允许 append。
                self._history_index_locked(
                    rows + [result],
                    graph if phase != "conflict" else None,
                )
            elif phase != "conflict":
                # v1 也先用尚未写入的 terminal 证明完整串行链和图终点；
                # 特别防止首条畸形 unfinished prepare 被先提交、后失败。
                self._prove_v1_journal_for_baseline(
                    rows + [result],
                    graph_digest=current_digest,
                )
            _append_jsonl(self.journal_path, result)
            recovered.append(result)
        return recovered

    @staticmethod
    def _assert_journal_writable(rows: list[dict]) -> None:
        conflicts = [
            str(row.get("txId") or "")
            for row in rows
            if row.get("phase") == "conflict"
        ]
        if conflicts:
            raise ConceptNodeError(
                "KG journal 存在无法判定的未完成事务，拒绝继续写入",
                "BW_KG_NODE_JOURNAL_CONFLICT",
                {"txIds": conflicts[-10:]},
            )

    def recover(self) -> list[dict]:
        with _exclusive_file_lock(self.lock_path):
            recovered = self._recover_locked(self.load_graph())
            graph = self.load_graph()
            rows = self._journal_rows(repair_torn_tail=True)
            self._assert_journal_writable(rows)
            rows = self._ensure_history_baseline_locked(graph, rows)
            self._history_index_locked(rows, graph)
            return recovered

    def mutation_status(self, mutation_id: str) -> dict:
        """Return the durable outcome of one mutation after journal recovery.

        Callers that coordinate a second durable resource (for example a PDF
        rename) must not infer "not committed" merely because the mutation call
        raised: the graph may already have been atomically replaced while the
        commit row was still pending.  This method resolves that window under
        the same cross-process lock and reports only three states:

        - ``applied``: a graph receipt or committed journal row proves success;
        - ``absent``: no commit/conflict exists after recovery;
        - ``ambiguous``: a conflict row prevents a safe rollback decision.
        """
        mutation_id = str(mutation_id or "").strip()
        if not mutation_id or len(mutation_id) > 300:
            raise ConceptNodeError("mutationId 无效", "BW_KG_NODE_MUTATION")
        with _exclusive_file_lock(self.lock_path):
            recovered = self._recover_locked(self.load_graph())
            graph = self.load_graph()
            rows = self._journal_rows(repair_torn_tail=True)
            mutation_rows = [
                row
                for row in rows
                if str(row.get("mutationId") or "") == mutation_id
            ]
            phases = {
                str(row.get("phase") or "")
                for row in mutation_rows
            }
            if "conflict" in phases and "commit" not in phases:
                return {
                    "contract": CONTRACT,
                    "mutationId": mutation_id,
                    "status": "ambiguous",
                    "receipt": None,
                    "recovered": recovered,
                }
            rows = self._ensure_history_baseline_locked(graph, rows)
            history = self._history_index_locked(rows, graph)
            cold_receipt = history["receipts"].get(mutation_id)
            receipt_result = (
                copy.deepcopy((cold_receipt or {}).get("result"))
                if isinstance(cold_receipt, dict)
                else None
            )
            receipt_tx = (
                str(receipt_result.get("txId") or "")
                if isinstance(receipt_result, dict) else ""
            )
            if not receipt_tx:
                receipt_tx = str(
                    history["mutationTxIds"].get(mutation_id) or ""
                )
            if (
                isinstance(cold_receipt, dict)
                or "commit" in phases
            ):
                status = (
                    "rolled_back"
                    if receipt_tx in history["rolledBackTxIds"]
                    else "applied"
                )
            elif "conflict" in phases:
                status = "ambiguous"
            else:
                status = "absent"
            return {
                "contract": CONTRACT,
                "mutationId": mutation_id,
                "status": status,
                "receipt": receipt_result,
                "recovered": recovered,
            }

    def _authored_terms(self) -> dict[str, str]:
        out = {}
        for path in sorted(self.kg_dir.glob("*.json")):
            if ".bak." in path.name or "pre" in path.stem or "scan" in path.stem:
                continue
            value = _read_json_optional_strict(
                path,
                {},
                code="BW_KG_NODE_AUTHORED_CORRUPT",
            )
            if not isinstance(value, dict):
                raise ConceptNodeError(
                    "authored KG 格式无效",
                    "BW_KG_NODE_AUTHORED_CORRUPT",
                    {"path": str(path)},
                )
            book = str(value.get("book") or path.stem)
            for node in value.get("nodes") or []:
                if not isinstance(node, dict) or node.get("level") != 2:
                    continue
                key = self.normalize(node.get("name"))
                if key:
                    out[key] = book + "#" + str(node.get("id") or "")
        return out

    def _manual_aliases(self) -> dict[str, set[str]]:
        raw = _read_json_optional_strict(
            self.aliases_path,
            {},
            code="BW_KG_NODE_ALIASES_CORRUPT",
        )
        out: dict[str, set[str]] = {}
        if not isinstance(raw, dict):
            raise ConceptNodeError(
                "concept aliases 格式无效",
                "BW_KG_NODE_ALIASES_CORRUPT",
                {"path": str(self.aliases_path)},
            )
        for canonical, aliases in raw.items():
            key = self.normalize(canonical)
            if not key:
                continue
            values = aliases if isinstance(aliases, list) else []
            out.setdefault(key, set()).update(
                alias
                for alias in (self.normalize(value) for value in values)
                if alias
            )
        return out

    def _note_aliases(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        if not self.concept_root.exists():
            return out
        for path in self.concept_root.glob("**/*.md"):
            name = re.sub(r"^[0-9A-Fa-f]{3}-", "", path.stem)
            key = self.normalize(name)
            if not key:
                continue
            out.setdefault(key, set())
            try:
                head = path.read_text("utf-8", errors="ignore")[:1000]
            except OSError as exc:
                raise ConceptNodeError(
                    "概念笔记 alias 无法读取",
                    "BW_KG_NODE_ALIASES_CORRUPT",
                    {"path": str(path), "error": str(exc)},
                ) from exc
            match = re.search(r"^aliases:\s*\[([^\]]*)\]", head, flags=re.M)
            if not match:
                continue
            for raw_alias in match.group(1).split(","):
                alias = self.normalize(raw_alias.strip().strip("\"'"))
                if alias:
                    out[key].add(alias)
        return out

    def _alias_index(self, graph: dict) -> dict[str, str | None]:
        claims: dict[str, set[str]] = {}

        def add(alias: Any, key: str):
            normalized = self.normalize(alias)
            if normalized:
                claims.setdefault(normalized, set()).add(key)

        for raw_key, raw_node in graph["nodes"].items():
            key = self.normalize(raw_key)
            if not key or not isinstance(raw_node, dict):
                continue
            add(raw_key, key)
            add(raw_node.get("surface"), key)
            for alias in raw_node.get("aliases") or []:
                add(alias, key)
        for source in (self._manual_aliases(), self._note_aliases()):
            for canonical, aliases in source.items():
                add(canonical, canonical)
                for alias in aliases:
                    add(alias, canonical)
        return {
            alias: (next(iter(keys)) if len(keys) == 1 else None)
            for alias, keys in claims.items()
        }

    def _confirmations(self) -> dict:
        raw = _read_json_optional_strict(
            self.confirmations_path,
            {},
            code="BW_KG_NODE_CONFIRMATIONS_CORRUPT",
        )
        if not isinstance(raw, dict):
            raise ConceptNodeError(
                "emergent confirmations 格式无效",
                "BW_KG_NODE_CONFIRMATIONS_CORRUPT",
                {"path": str(self.confirmations_path)},
            )
        nodes = raw.get("nodes") if isinstance(raw, dict) else {}
        if nodes is None:
            return {}
        if not isinstance(nodes, dict):
            raise ConceptNodeError(
                "emergent confirmations.nodes 格式无效",
                "BW_KG_NODE_CONFIRMATIONS_CORRUPT",
                {"path": str(self.confirmations_path)},
            )
        return nodes

    def _validate_candidate(self, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ConceptNodeError("candidate 必须是对象", "BW_KG_NODE_CANDIDATE")
        surface = unicodedata.normalize(
            "NFKC", str(raw.get("surface") or raw.get("name") or "")
        ).strip()
        source_kind = str(raw.get("sourceKind") or raw.get("source_kind") or "").strip()
        source_id = str(raw.get("sourceId") or raw.get("source_id") or "").strip()
        document_ref = str(
            raw.get("documentRef") or raw.get("document_ref") or raw.get("ref") or ""
        ).strip()
        if not surface or len(surface) > 120:
            raise ConceptNodeError("概念名无效", "BW_KG_NODE_NAME")
        if source_kind not in _SOURCE_KINDS_WITH_REFERENCE:
            raise ConceptNodeError(
                "未知或无证据的节点来源",
                "BW_KG_NODE_SOURCE",
                {"sourceKind": source_kind},
            )
        if not source_id or len(source_id) > 300:
            raise ConceptNodeError("sourceId 无效", "BW_KG_NODE_SOURCE")
        if not document_ref or len(document_ref) > 1000:
            raise ConceptNodeError("documentRef 无效", "BW_KG_NODE_EVIDENCE")

        page = int(raw.get("page") or 0)
        quote = str(raw.get("quote") or raw.get("evidence") or "").strip()
        source_text = str(raw.get("sourceText") or raw.get("source_text") or "")
        if source_kind in _SOURCE_KINDS_WITH_QUOTE:
            if page <= 0 or not quote or not source_text:
                raise ConceptNodeError(
                    "自动页面节点必须携带页码、逐字 quote 与原文",
                    "BW_KG_NODE_EVIDENCE",
                )
            quote_key = _text_key(quote)
            quote_match_key = _evidence_match_key(quote)
            if (
                not quote_match_key
                or quote_match_key not in _evidence_match_key(source_text)
            ):
                raise ConceptNodeError(
                    "节点 quote 无法在来源原文中逐字复核",
                    "BW_KG_NODE_EVIDENCE",
                )
            surface_key = _evidence_match_key(surface)
            meaningful = re.sub(
                r"[^\w\u3400-\u9fff\u3040-\u30ff]",
                "",
                quote_match_key,
            )
            if (
                not surface_key
                or len(meaningful) < 4
                or surface_key not in quote_match_key
            ):
                raise ConceptNodeError(
                    "概念名必须是 quote 中逐字出现的术语",
                    "BW_KG_NODE_EVIDENCE",
                )
        elif not document_ref:
            raise ConceptNodeError("节点来源引用缺失", "BW_KG_NODE_EVIDENCE")

        key = self.normalize(surface)
        if not key:
            raise ConceptNodeError("概念归一化后为空", "BW_KG_NODE_KEY")
        return {
            "surface": surface,
            "key": key,
            "sourceKind": source_kind,
            "sourceId": source_id,
            "documentRef": document_ref,
            "page": page,
            "quote": quote[:1000],
            "quoteSha256": (
                hashlib.sha256(_text_key(quote).encode("utf-8")).hexdigest()
                if quote else ""
            ),
            "sourceSha256": (
                hashlib.sha256(source_text.encode("utf-8")).hexdigest()
                if source_text else ""
            ),
            "evidenceId": _evidence_id(
                node_key=key,
                source_kind=source_kind,
                source_id=source_id,
                document_ref=document_ref,
                page=page,
                quote=quote,
            ),
            "brief": str(raw.get("brief") or "").strip()[:500],
            "book": str(raw.get("book") or "").strip()[:1000],
            "subject": str(raw.get("subject") or "").strip()[:200],
            "aliases": [
                str(value).strip()[:120]
                for value in (raw.get("aliases") or [])
                if str(value).strip()
            ][:20],
        }

    def _commit_graph_locked(
        self,
        *,
        before_graph: dict,
        after_graph: dict,
        mutation_id: str,
        source: str,
        before_nodes: dict,
        after_nodes: dict,
        operation_contract: str,
        request_digest: str,
        receipt: dict,
        mutation_kind: str,
        history_sequence: int,
        history_previous_tx: str,
        occurrences_added: Iterable[dict] = (),
        occurrences_removed: Iterable[dict] = (),
        projections: Iterable[dict] = (),
        tx_id: str | None = None,
        commit_extra: dict | None = None,
    ) -> str:
        tx_id = str(tx_id or self.tx_factory())
        if not tx_id or len(tx_id) > 300:
            raise ConceptNodeError(
                "KG transaction 身份无效",
                "BW_KG_NODE_TRANSACTION",
            )
        existing_tx_ids = {
            str(row.get("txId") or "")
            for row in self._journal_rows(repair_torn_tail=False)
            if row.get("phase") == "prepare"
        }
        if tx_id in existing_tx_ids:
            raise ConceptNodeError(
                "KG transactionId 已被历史事务占用",
                "BW_KG_NODE_TRANSACTION_REUSE",
                {"txId": tx_id},
            )
        history_sequence = int(history_sequence)
        after_graph.setdefault("meta", {})["kg_history"] = {
            "contract": HISTORY_CONTRACT,
            "lastSequence": history_sequence,
            "lastTx": tx_id,
        }
        commit_extra = copy.deepcopy(commit_extra or {})
        projected_history = [
            copy.deepcopy(value)
            for value in projections
        ]
        projected_from = {
            _canonical_json(move["from"])
            for projection in projected_history
            if isinstance(projection, dict)
            for move in (projection.get("moves") or [])
            if isinstance(move, dict) and isinstance(move.get("from"), dict)
        }
        projected_to = {
            _canonical_json(move["to"])
            for projection in projected_history
            if isinstance(projection, dict)
            for move in (projection.get("moves") or [])
            if isinstance(move, dict) and isinstance(move.get("to"), dict)
        }
        history_added = [
            copy.deepcopy(value)
            for value in occurrences_added
            if _canonical_json(value) not in projected_to
        ]
        history_removed = [
            copy.deepcopy(value)
            for value in occurrences_removed
            if _canonical_json(value) not in projected_from
        ]
        before_occurrence_map = _occurrence_map_for_nodes(before_nodes)
        after_occurrence_map = _occurrence_map_for_nodes(after_nodes)
        removed_keys = {
            _canonical_json(value)
            for value in history_removed
        }
        eviction_keys = (
            set(before_occurrence_map)
            - set(after_occurrence_map)
            - removed_keys
            - projected_from
        )
        occurrence_evictions = [
            copy.deepcopy(before_occurrence_map[key])
            for key in sorted(eviction_keys)
        ]
        receipt_record = {
            "requestDigest": request_digest,
            "operationContract": operation_contract,
            "result": copy.deepcopy(receipt),
        }
        history = {
            "contract": HISTORY_CONTRACT,
            "mutationKind": str(mutation_kind or ""),
            "sequence": history_sequence,
            "previousTx": str(history_previous_tx or ""),
            "receipt": receipt_record,
            "resultDigest": _digest(receipt),
            "occurrencesAdded": history_added,
            "occurrencesRemoved": history_removed,
            "occurrenceEvictions": occurrence_evictions,
            "projections": projected_history,
            "commitExtra": commit_extra,
        }
        history_digest = _digest(history)
        prepared = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": tx_id,
            "mutationId": mutation_id,
            "source": source,
            "ts": int(self.clock()),
            "graphBeforeDigest": _digest(before_graph),
            "graphAfterDigest": _digest(after_graph),
            "beforeNodes": copy.deepcopy(before_nodes),
            "afterNodes": copy.deepcopy(after_nodes),
            "history": history,
            "historyDigest": history_digest,
            "commitExtra": commit_extra,
        }
        prepared["prepareDigest"] = _digest(prepared)
        _append_jsonl(self.journal_path, prepared)
        _write_json_atomic(self.graph_path, after_graph)
        committed = {
            "contract": LOG_CONTRACT,
            "phase": "commit",
            "txId": tx_id,
            "mutationId": mutation_id,
            "source": source,
            "ts": int(self.clock()),
            "graphDigest": prepared["graphAfterDigest"],
            "historySequence": history_sequence,
            "requestDigest": request_digest,
            "resultDigest": history["resultDigest"],
            "historyDigest": history_digest,
            "prepareDigest": prepared["prepareDigest"],
        }
        for key, value in commit_extra.items():
            if key not in committed:
                committed[key] = value
        _append_jsonl(self.journal_path, committed)
        return tx_id

    def _next_transaction_id(self, history: dict) -> str:
        tx_id = str(self.tx_factory() or "").strip()
        if not tx_id or len(tx_id) > 300:
            raise ConceptNodeError(
                "KG transaction 身份无效",
                "BW_KG_NODE_TRANSACTION",
            )
        if tx_id in history.get("allTransactionIds", set()):
            raise ConceptNodeError(
                "KG transactionId 已被历史事务占用",
                "BW_KG_NODE_TRANSACTION_REUSE",
                {"txId": tx_id},
            )
        return tx_id

    def upsert_candidates(
        self,
        candidates: Iterable[dict],
        *,
        mutation_id: str,
        source: str = "automatic",
        operation_contract: str = "kg-op/node-upsert/1",
        operation_payload: dict | None = None,
    ) -> dict:
        mutation_id = str(mutation_id or "").strip()
        if not mutation_id or len(mutation_id) > 300:
            raise ConceptNodeError("mutationId 无效", "BW_KG_NODE_MUTATION")
        checked = [self._validate_candidate(candidate) for candidate in candidates]
        if not checked:
            raise ConceptNodeError(
                "空 KG candidate batch 不构成可持久化 mutation",
                "BW_KG_NODE_CANDIDATE",
                {"mutationId": mutation_id},
            )
        canonical_candidates = []
        for candidate in checked:
            canonical_candidates.append({
                "surface": candidate["surface"],
                "key": candidate["key"],
                "sourceKind": candidate["sourceKind"],
                "sourceId": candidate["sourceId"],
                "documentRef": candidate["documentRef"],
                "page": candidate["page"],
                "quoteSha256": candidate["quoteSha256"],
                "brief": candidate["brief"],
                "book": candidate["book"],
                "subject": candidate["subject"],
                "aliases": sorted(candidate["aliases"]),
            })
        canonical_candidates.sort(key=_canonical_json)
        operation_contract, request_digest = self._operation_identity(
            operation_contract,
            {
                "caller": operation_payload or {},
                "source": str(source or ""),
                "candidates": canonical_candidates,
            },
        )

        with _exclusive_file_lock(self.lock_path):
            graph = self.load_graph()
            self._recover_locked(graph)
            graph = self.load_graph()
            rows = self._journal_rows(repair_torn_tail=True)
            self._assert_journal_writable(rows)
            rows = self._ensure_history_baseline_locked(graph, rows)
            history = self._history_index_locked(rows, graph)
            before_graph = copy.deepcopy(graph)
            meta = graph.setdefault("meta", {})
            mutation_map = meta.setdefault("node_mutations", {})
            if not isinstance(mutation_map, dict):
                mutation_map = {}
                meta["node_mutations"] = mutation_map
            replay = self._history_replay(
                history=history,
                mutation_map=mutation_map,
                mutation_id=mutation_id,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            if replay is not None:
                return replay
            tx_id = self._next_transaction_id(history)

            alias_index = self._alias_index(graph)
            authored = self._authored_terms()
            confirmations = self._confirmations()
            id_to_key = {
                str(node.get("id") or ""): str(key)
                for key, node in graph["nodes"].items()
                if isinstance(node, dict) and node.get("id")
            }
            result = {
                "contract": CONTRACT,
                "mutationId": mutation_id,
                "txId": None,
                "created": [],
                "anchored": [],
                "updated": [],
                "deduplicated": [],
                "rejected": [],
            }
            changed_keys = set()
            before_nodes = {}
            accepted_occurrences = {}
            now = int(self.clock())

            for candidate in checked:
                input_key = candidate["key"]
                resolved = alias_index.get(input_key, input_key)
                if resolved is None:
                    result["rejected"].append({
                        "surface": candidate["surface"],
                        "reason": "ambiguous-alias",
                    })
                    continue
                key = resolved
                existing = graph["nodes"].get(key)
                authored_ref = authored.get(key, "")
                if existing is not None and not isinstance(existing, dict):
                    raise ConceptNodeError(
                        "已有节点记录无效",
                        "BW_KG_NODE_GRAPH",
                        {"key": key},
                    )
                node_id = (
                    str(existing.get("id") or stable_node_id(key))
                    if existing is not None else stable_node_id(key)
                )
                collision = id_to_key.get(node_id)
                if collision and collision != key:
                    raise ConceptNodeError(
                        "稳定 nodeId 冲突",
                        "BW_KG_NODE_ID_COLLISION",
                        {"nodeId": node_id, "left": collision, "right": key},
                    )
                if (
                    existing
                    and (
                        existing.get("deleted") is True
                        or existing.get("tombstone")
                    )
                ):
                    result["rejected"].append({
                        "surface": candidate["surface"],
                        "key": key,
                        "nodeId": node_id,
                        "reason": "tombstoned",
                    })
                    continue
                if confirmations.get(key) is False or confirmations.get(node_id) is False:
                    result["rejected"].append({
                        "surface": candidate["surface"],
                        "key": key,
                        "nodeId": node_id,
                        "reason": "user-rejected",
                    })
                    continue

                evidence = {
                    "id": candidate["evidenceId"],
                    "type": candidate["sourceKind"],
                    "sourceId": candidate["sourceId"],
                    "documentRef": candidate["documentRef"],
                    "page": candidate["page"],
                    "quote": candidate["quote"],
                    "quoteSha256": candidate["quoteSha256"],
                    "sourceSha256": candidate["sourceSha256"],
                    "brief": candidate["brief"],
                    "ts": now,
                }
                previous_provenance = [
                    item
                    for item in ((existing or {}).get("provenance") or [])
                    if isinstance(item, dict)
                ]
                previous_evidence = {
                    str(item.get("id") or "")
                    for item in previous_provenance
                }
                occurrence = _candidate_occurrence(key, candidate)
                occurrence_key = _canonical_json(occurrence)
                if (
                    evidence["id"] in previous_evidence
                    or _page_brief_occurrence_replayed(
                        candidate,
                        previous_provenance,
                    )
                    or occurrence_key in history["occurrences"]
                ):
                    result["deduplicated"].append({
                        "surface": candidate["surface"],
                        "key": key,
                        "nodeId": node_id,
                        "reason": "evidence-replay",
                    })
                    continue

                before_nodes.setdefault(key, copy.deepcopy(existing))
                if existing is None:
                    node = {
                        "id": node_id,
                        "surface": candidate["surface"],
                        "key": key,
                        "aliases": [],
                        "sources": [],
                        "signal": 0,
                        "provenance": [],
                        "in_authored_kg": bool(authored_ref),
                        "authored_ref": authored_ref,
                        "books": [],
                        "subject": candidate["subject"],
                        "kind": "concept",
                        "origin": "emergent",
                        "confirmed": None,
                        "createdAt": now,
                    }
                    if authored_ref:
                        result["anchored"].append({
                            "key": key,
                            "nodeId": node_id,
                            "surface": candidate["surface"],
                            "authoredRef": authored_ref,
                        })
                    else:
                        result["created"].append({
                            "key": key, "nodeId": node_id, "surface": candidate["surface"],
                        })
                else:
                    node = copy.deepcopy(existing)
                    result["updated"].append({
                        "key": key, "nodeId": node_id, "surface": candidate["surface"],
                    })

                node["id"] = node_id
                aliases = set(str(value) for value in (node.get("aliases") or []))
                aliases.update(candidate["aliases"])
                if candidate["surface"] != node.get("surface"):
                    aliases.add(candidate["surface"])
                aliases.discard(str(node.get("surface") or ""))
                node["aliases"] = sorted(value for value in aliases if value)
                node["sources"] = sorted(
                    set(node.get("sources") or []) | {candidate["sourceKind"]}
                )
                node["signal"] = int(node.get("signal") or 0) + 1
                node["provenance"] = (
                    list(node.get("provenance") or []) + [evidence]
                )[-_MAX_PROVENANCE:]
                books = set(str(value) for value in (node.get("books") or []) if value)
                if authored_ref:
                    node["in_authored_kg"] = True
                    node["authored_ref"] = authored_ref
                    books.add(authored_ref.split("#", 1)[0])
                if candidate["book"]:
                    books.add(candidate["book"])
                node["books"] = sorted(books)
                if not node.get("subject") and candidate["subject"]:
                    node["subject"] = candidate["subject"]
                node["updatedAt"] = now
                graph["nodes"][key] = node
                id_to_key[node_id] = key
                for alias in (
                    [input_key, candidate["surface"]]
                    + list(candidate["aliases"])
                    + list(node.get("aliases") or [])
                ):
                    normalized_alias = self.normalize(alias)
                    if not normalized_alias:
                        continue
                    previous_key = alias_index.get(normalized_alias)
                    if previous_key in (None, key) and normalized_alias in alias_index:
                        # None 代表已有歧义，不能由自动候选静默消解。
                        if previous_key is None:
                            continue
                    if previous_key and previous_key != key:
                        alias_index[normalized_alias] = None
                    else:
                        alias_index[normalized_alias] = key
                changed_keys.add(key)
                accepted_occurrences[occurrence_key] = occurrence

            after_nodes = {
                key: copy.deepcopy(graph["nodes"].get(key))
                for key in sorted(changed_keys)
            }
            result["txId"] = tx_id
            result_for_ledger = _ledger_receipt(
                result,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            mutation_map[mutation_id] = result_for_ledger
            if len(mutation_map) > _MAX_MUTATIONS:
                for old in list(mutation_map)[: len(mutation_map) - _MAX_MUTATIONS]:
                    mutation_map.pop(old, None)
            meta["node_service_contract"] = CONTRACT
            meta["n"] = len([
                node for node in graph["nodes"].values()
                if isinstance(node, dict) and not node.get("deleted")
            ])
            meta["n_new"] = len([
                node for node in graph["nodes"].values()
                if isinstance(node, dict)
                and not node.get("deleted")
                and not node.get("in_authored_kg")
            ])
            meta["node_updated"] = now

            self._commit_graph_locked(
                before_graph=before_graph,
                after_graph=graph,
                mutation_id=mutation_id,
                source=source,
                before_nodes=before_nodes,
                after_nodes=after_nodes,
                operation_contract=operation_contract,
                request_digest=request_digest,
                receipt=result,
                mutation_kind="node-upsert",
                history_sequence=history["lastSequence"] + 1,
                history_previous_tx=history["lastTx"],
                occurrences_added=[
                    accepted_occurrences[key]
                    for key in sorted(accepted_occurrences)
                ],
                tx_id=tx_id,
            )
            return result

    def mutate_graph(
        self,
        *,
        mutation_id: str,
        source: str,
        mutator: Callable[[dict], dict | None],
        operation_contract: str,
        operation_payload: dict,
        history_projections: Iterable[dict] = (),
        expected_graph_digest: str | None = None,
    ) -> dict:
        """在同一锁和 journal 下修改边/overlay/meta，且禁止绕过节点身份规则。"""
        mutation_id = str(mutation_id or "").strip()
        if not mutation_id or len(mutation_id) > 300:
            raise ConceptNodeError("mutationId 无效", "BW_KG_NODE_MUTATION")
        expected_graph_digest = str(expected_graph_digest or "").strip()
        if expected_graph_digest and not re.fullmatch(
            r"[0-9a-f]{64}",
            expected_graph_digest,
        ):
            raise ConceptNodeError(
                "KG expectedGraphDigest 无效",
                "BW_KG_NODE_OPERATION",
            )
        operation_contract, request_digest = self._operation_identity(
            operation_contract,
            operation_payload,
        )
        history_projections = [
            copy.deepcopy(value)
            for value in history_projections
        ]
        with _exclusive_file_lock(self.lock_path):
            graph = self.load_graph()
            self._recover_locked(graph)
            graph = self.load_graph()
            rows = self._journal_rows(repair_torn_tail=True)
            self._assert_journal_writable(rows)
            rows = self._ensure_history_baseline_locked(graph, rows)
            history = self._history_index_locked(rows, graph)
            before_graph = copy.deepcopy(graph)
            meta = graph.setdefault("meta", {})
            mutation_map = meta.setdefault("node_mutations", {})
            if not isinstance(mutation_map, dict):
                mutation_map = {}
                meta["node_mutations"] = mutation_map
            replay = self._history_replay(
                history=history,
                mutation_map=mutation_map,
                mutation_id=mutation_id,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            if replay is not None:
                return replay
            if (
                expected_graph_digest
                and self.graph_digest(before_graph) != expected_graph_digest
            ):
                raise ConceptNodeError(
                    "KG 图已在调用快照后变化，拒绝把旧输入应用到新图",
                    "BW_KG_NODE_STALE_GRAPH",
                    {
                        "expectedGraphDigest": expected_graph_digest,
                        "actualGraphDigest": self.graph_digest(before_graph),
                    },
                )
            tx_id = self._next_transaction_id(history)

            for projection in history_projections:
                if projection.get("kind") != "page-brief-document-ref":
                    continue
                old_ref = str(projection.get("oldDocumentRef") or "")
                new_ref = str(projection.get("newDocumentRef") or "")
                if not old_ref.startswith("book:") or not new_ref.startswith("book:"):
                    continue
                historical_node_keys = {
                    str(occurrence.get("nodeKey") or "")
                    for occurrence in history["occurrences"].values()
                    if (
                        occurrence.get("kind") == "page-brief"
                        and occurrence.get("documentRef") == old_ref
                    )
                }
                for node_key in historical_node_keys:
                    node = graph.get("nodes", {}).get(node_key)
                    if not isinstance(node, dict):
                        continue
                    books = {
                        str(value)
                        for value in (node.get("books") or [])
                        if str(value)
                    }
                    books.add(new_ref[5:])
                    node["books"] = sorted(books)

            payload = mutator(graph)
            graph = _normalize_graph(graph)
            # _normalize_graph 会深拷贝；后续 receipt/meta 必须重新绑定到规范化后的图，
            # 否则 mutation 结果只写进旧对象，落盘图没有 receipt，重放会再次执行。
            meta = graph.setdefault("meta", {})
            mutation_map = meta.setdefault("node_mutations", {})
            if not isinstance(mutation_map, dict):
                mutation_map = {}
                meta["node_mutations"] = mutation_map
            before_nodes_all = before_graph.get("nodes") or {}
            after_nodes_all = graph.get("nodes") or {}
            unexpected_nodes = sorted(set(after_nodes_all) - set(before_nodes_all))
            if unexpected_nodes:
                raise ConceptNodeError(
                    "通用图事务不得绕过节点服务新增节点",
                    "BW_KG_NODE_IDENTITY",
                    {"keys": unexpected_nodes},
                )
            for key, previous in before_nodes_all.items():
                current = after_nodes_all.get(key)
                if current is None:
                    raise ConceptNodeError(
                        "通用图事务不得物理删除节点",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )
                previous_id = (
                    str(previous.get("id") or "")
                    if isinstance(previous, dict) else ""
                )
                current_id = (
                    str(current.get("id") or "")
                    if isinstance(current, dict) else ""
                )
                if previous_id and current_id != previous_id:
                    raise ConceptNodeError(
                        "通用图事务不得改变稳定 nodeId",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key, "before": previous_id, "after": current_id},
                    )
                if (
                    isinstance(previous, dict)
                    and (previous.get("deleted") or previous.get("tombstone"))
                    and isinstance(current, dict)
                    and not (current.get("deleted") or current.get("tombstone"))
                ):
                    raise ConceptNodeError(
                        "通用图事务不得复活 tombstone",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )

            changed_keys = {
                key
                for key in set(before_nodes_all) | set(after_nodes_all)
                if _digest(before_nodes_all.get(key)) != _digest(after_nodes_all.get(key))
            }
            if changed_keys and not history_projections:
                raise ConceptNodeError(
                    "通用图事务不得修改节点内容",
                    "BW_KG_NODE_IDENTITY",
                    {"keys": sorted(changed_keys)},
                )
            if history_projections and operation_contract != (
                "kg-op/page-brief-document-rename/1"
            ):
                raise ConceptNodeError(
                    "节点路径投影只允许 PageBrief rename 合同",
                    "BW_KG_NODE_IDENTITY",
                )
            for key in sorted(changed_keys):
                previous = copy.deepcopy(before_nodes_all.get(key))
                current = copy.deepcopy(after_nodes_all.get(key))
                if not isinstance(previous, dict) or not isinstance(current, dict):
                    raise ConceptNodeError(
                        "PageBrief 路径投影不得新增或删除节点",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )
                if int(previous.get("signal") or 0) != int(
                    current.get("signal") or 0
                ):
                    raise ConceptNodeError(
                        "PageBrief 路径投影不得改变 signal",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )
                expected = copy.deepcopy(previous)
                for projection in history_projections:
                    old_ref = str(projection.get("oldDocumentRef") or "")
                    new_ref = str(projection.get("newDocumentRef") or "")
                    for evidence in expected.get("provenance") or []:
                        if (
                            isinstance(evidence, dict)
                            and evidence.get("type") == "page-brief"
                            and evidence.get("documentRef") == old_ref
                        ):
                            evidence["documentRef"] = new_ref
                previous_books = {
                    str(value)
                    for value in (previous.get("books") or [])
                    if str(value)
                }
                current_books = {
                    str(value)
                    for value in (current.get("books") or [])
                    if str(value)
                }
                allowed_added = {
                    str(projection.get("newDocumentRef") or "")[5:]
                    for projection in history_projections
                    if str(projection.get("newDocumentRef") or "").startswith(
                        "book:"
                    )
                }
                allowed_removed = {
                    str(projection.get("oldDocumentRef") or "")[5:]
                    for projection in history_projections
                    if str(projection.get("oldDocumentRef") or "").startswith(
                        "book:"
                    )
                }
                if (
                    current_books - previous_books - allowed_added
                    or previous_books - current_books - allowed_removed
                ):
                    raise ConceptNodeError(
                        "PageBrief 路径投影越界修改 books",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )
                expected.pop("books", None)
                current_without_books = copy.deepcopy(current)
                current_without_books.pop("books", None)
                if _digest(expected) != _digest(current_without_books):
                    raise ConceptNodeError(
                        "PageBrief 路径投影越界修改节点",
                        "BW_KG_NODE_IDENTITY",
                        {"key": key},
                    )
            before_nodes = {
                key: copy.deepcopy(before_nodes_all.get(key))
                for key in sorted(changed_keys)
            }
            after_nodes = {
                key: copy.deepcopy(after_nodes_all.get(key))
                for key in sorted(changed_keys)
            }
            before_occurrences = _occurrence_map_for_nodes(before_nodes)
            after_occurrences = _occurrence_map_for_nodes(after_nodes)
            occurrences_removed = [
                before_occurrences[key]
                for key in sorted(set(before_occurrences) - set(after_occurrences))
            ]
            occurrences_added = [
                after_occurrences[key]
                for key in sorted(set(after_occurrences) - set(before_occurrences))
            ]
            projected_check = copy.deepcopy(history["occurrences"])
            materialized_projections = []
            for projection in history_projections:
                materialized = self._materialize_history_projection(
                    projected_check,
                    projection,
                )
                self._apply_history_projection(
                    projected_check,
                    materialized,
                )
                materialized_projections.append(materialized)
            history_projections = materialized_projections
            result = {
                "contract": CONTRACT,
                "mutationId": mutation_id,
                "txId": tx_id,
                "source": source,
                "changedNodes": sorted(changed_keys),
                "payload": payload if isinstance(payload, dict) else {},
            }
            mutation_map[mutation_id] = _ledger_receipt(
                result,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            if len(mutation_map) > _MAX_MUTATIONS:
                for old in list(mutation_map)[: len(mutation_map) - _MAX_MUTATIONS]:
                    mutation_map.pop(old, None)
            meta["node_service_contract"] = CONTRACT
            meta["node_updated"] = int(self.clock())
            self._commit_graph_locked(
                before_graph=before_graph,
                after_graph=graph,
                mutation_id=mutation_id,
                source=source,
                before_nodes=before_nodes,
                after_nodes=after_nodes,
                operation_contract=operation_contract,
                request_digest=request_digest,
                receipt=result,
                mutation_kind="graph-mutation",
                history_sequence=history["lastSequence"] + 1,
                history_previous_tx=history["lastTx"],
                occurrences_added=occurrences_added,
                occurrences_removed=occurrences_removed,
                projections=history_projections,
                tx_id=tx_id,
            )
            return result

    def rollback(self, tx_id: str, *, mutation_id: str) -> dict:
        tx_id = str(tx_id or "").strip()
        mutation_id = str(mutation_id or "").strip()
        if not tx_id or not mutation_id:
            raise ConceptNodeError("rollback 缺少稳定编号", "BW_KG_NODE_ROLLBACK")
        operation_contract, request_digest = self._operation_identity(
            "kg-op/rollback/1",
            {"rollbackOfTxId": tx_id},
        )

        with _exclusive_file_lock(self.lock_path):
            graph = self.load_graph()
            self._recover_locked(graph)
            graph = self.load_graph()
            rows = self._journal_rows(repair_torn_tail=True)
            self._assert_journal_writable(rows)
            rows = self._ensure_history_baseline_locked(graph, rows)
            history = self._history_index_locked(rows, graph)
            meta_view = graph.get("meta")
            mutation_map_view = (
                meta_view.get("node_mutations")
                if isinstance(meta_view, dict) else None
            )
            if mutation_map_view is not None and not isinstance(
                mutation_map_view,
                dict,
            ):
                raise ConceptNodeError(
                    "KG hot mutation ledger 无效",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                )
            mutation_map_view = mutation_map_view or {}
            replay = self._history_replay(
                history=history,
                mutation_map=mutation_map_view,
                mutation_id=mutation_id,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            if replay is not None:
                return replay
            rollback_tx = self._next_transaction_id(history)
            if tx_id in history["rolledBackTxIds"]:
                raise ConceptNodeError(
                    "该事务已由另一个 mutationId 回滚",
                    "BW_KG_NODE_ROLLBACK_REUSE",
                    {"txId": tx_id, "mutationId": mutation_id},
                )
            prepared = next(
                (
                    row for row in rows
                    if row.get("txId") == tx_id and row.get("phase") == "prepare"
                ),
                None,
            )
            committed = any(
                row.get("txId") == tx_id and row.get("phase") == "commit"
                for row in rows
            )
            if not prepared or not committed:
                raise ConceptNodeError(
                    "找不到已提交的节点事务",
                    "BW_KG_NODE_ROLLBACK",
                    {"txId": tx_id},
                )
            target_commit = next(
                (
                    row
                    for row in rows
                    if row.get("txId") == tx_id
                    and row.get("phase") == "commit"
                ),
                {},
            )
            if target_commit.get("rollbackOf"):
                raise ConceptNodeError(
                    "不支持 rollback-of-rollback",
                    "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
                    {"txId": tx_id},
                )
            target_history = prepared.get("history")
            target_projections = (
                target_history.get("projections") or []
                if isinstance(target_history, dict) else []
            )
            if (
                tx_id in history["legacyTransactionIds"]
                or not isinstance(target_history, dict)
            ):
                raise ConceptNodeError(
                    "baseline 前事务缺少可验证的反向快照",
                    "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
                    {"txId": tx_id},
                )
            if (
                not (prepared.get("afterNodes") or {})
                and not target_projections
            ):
                raise ConceptNodeError(
                    "该事务不含可安全回滚的节点或历史投影变化",
                    "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
                    {"txId": tx_id},
                )
            inverse_projections = []
            for projection in reversed(target_projections):
                if (
                    not isinstance(projection, dict)
                    or projection.get("kind")
                    != "page-brief-document-ref"
                ):
                    raise ConceptNodeError(
                        "目标事务含无法安全反演的历史投影",
                        "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
                        {"txId": tx_id},
                    )
                moves = projection.get("moves")
                if not isinstance(moves, list):
                    raise ConceptNodeError(
                        "目标事务缺少可验证的历史投影 move",
                        "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
                        {"txId": tx_id},
                    )
                inverse_projections.append({
                    "kind": "page-brief-document-ref",
                    "oldDocumentRef": projection.get("newDocumentRef"),
                    "newDocumentRef": projection.get("oldDocumentRef"),
                    "scope": "explicit-moves",
                    "moves": [
                        {
                            "from": copy.deepcopy(move.get("to")),
                            "to": copy.deepcopy(move.get("from")),
                        }
                        for move in reversed(moves)
                        if isinstance(move, dict)
                    ],
                })
            projected_history = copy.deepcopy(history["occurrences"])
            for projection in inverse_projections:
                self._apply_history_projection(
                    projected_history,
                    projection,
                )
            before_graph = copy.deepcopy(graph)
            changed_before = {}
            changed_after = {}
            now = int(self.clock())
            for key, expected_after in (prepared.get("afterNodes") or {}).items():
                current = graph["nodes"].get(key)
                if _digest(current) != _digest(expected_after):
                    raise ConceptNodeError(
                        "节点在事务后已被继续修改，拒绝覆盖回滚",
                        "BW_KG_NODE_ROLLBACK_CONFLICT",
                        {"txId": tx_id, "key": key},
                    )
                changed_before[key] = copy.deepcopy(current)
                previous = (prepared.get("beforeNodes") or {}).get(key)
                if previous is None:
                    tombstone = copy.deepcopy(current or {})
                    tombstone["deleted"] = True
                    tombstone["status"] = "rolled_back"
                    tombstone["tombstone"] = {
                        "rollbackOf": tx_id,
                        "mutationId": mutation_id,
                        "ts": now,
                    }
                    tombstone["updatedAt"] = now
                    graph["nodes"][key] = tombstone
                else:
                    graph["nodes"][key] = copy.deepcopy(previous)
                changed_after[key] = copy.deepcopy(graph["nodes"][key])

            graph.setdefault("meta", {})["node_updated"] = now
            mutation_map = graph["meta"].get("node_mutations")
            if mutation_map is None:
                mutation_map = {}
                graph["meta"]["node_mutations"] = mutation_map
            elif not isinstance(mutation_map, dict):
                raise ConceptNodeError(
                    "KG hot mutation ledger 无效",
                    "BW_KG_NODE_HISTORY_INCOMPLETE",
                )
            result = {
                "contract": CONTRACT,
                "rollbackOf": tx_id,
                "txId": rollback_tx,
                "mutationId": mutation_id,
                "replay": False,
                "keys": sorted(changed_after),
            }
            mutation_map[mutation_id] = _ledger_receipt(
                result,
                request_digest=request_digest,
                operation_contract=operation_contract,
            )
            if len(mutation_map) > _MAX_MUTATIONS:
                for old in list(mutation_map)[: len(mutation_map) - _MAX_MUTATIONS]:
                    mutation_map.pop(old, None)
            before_occurrences = _occurrence_map_for_nodes(changed_before)
            after_occurrences = _occurrence_map_for_nodes(changed_after)
            self._commit_graph_locked(
                before_graph=before_graph,
                after_graph=graph,
                mutation_id=mutation_id,
                source="rollback:" + tx_id,
                before_nodes=changed_before,
                after_nodes=changed_after,
                operation_contract=operation_contract,
                request_digest=request_digest,
                receipt=result,
                mutation_kind="rollback",
                history_sequence=history["lastSequence"] + 1,
                history_previous_tx=history["lastTx"],
                occurrences_added=[
                    after_occurrences[key]
                    for key in sorted(
                        set(after_occurrences) - set(before_occurrences)
                    )
                ],
                occurrences_removed=[
                    before_occurrences[key]
                    for key in sorted(
                        set(before_occurrences) - set(after_occurrences)
                    )
                ],
                projections=inverse_projections,
                tx_id=rollback_tx,
                commit_extra={"rollbackOf": tx_id},
            )
            return result


def page_brief_candidates(
    *,
    file_rel: str,
    page: int,
    page_text: str,
    brief: dict,
    source_id: str,
) -> list[dict]:
    """PageBrief adapter：只把已经带逐字 evidence 的 concept 交给节点服务。"""
    if (
        not isinstance(brief, dict)
        or brief.get("page_type") != "knowledge"
        or int(page or 0) <= 0
    ):
        return []
    concepts = brief.get("concepts") or []
    if not isinstance(concepts, list):
        return []
    document_ref = _page_brief_document_ref(file_rel)
    out = []
    for item in concepts[:5]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        quote = str(item.get("evidence") or "").strip()
        if not name or not quote:
            continue
        out.append({
            "surface": name,
            "sourceKind": "page-brief",
            "sourceId": source_id,
            "documentRef": document_ref,
            "book": str(file_rel),
            "page": int(page),
            "quote": quote,
            "sourceText": page_text,
            "brief": str(brief.get("brief") or ""),
        })
    return out


def _page_brief_document_ref(file_rel: str) -> str:
    file_rel = str(file_rel or "").strip()
    if (
        not file_rel
        or len(file_rel) > 900
        or file_rel.startswith("/")
        or "\\" in file_rel
        or any(part in {"", ".", ".."} for part in file_rel.split("/"))
        or any(ord(character) < 32 or ord(character) == 127
               for character in file_rel)
    ):
        raise ConceptNodeError(
            "PageBrief 文档路径无效",
            "BW_KG_NODE_DOCUMENT",
            {"file": file_rel},
        )
    return "book:" + file_rel


def migrate_page_brief_document(
    *,
    old_file_rel: str,
    new_file_rel: str,
    mutation_id: str,
    service: ConceptNodeService | None = None,
) -> dict:
    """在节点服务事务内迁移 PDF 改名产生的可变路径投影。

    evidenceId/sourceId/nodeId 是已分配的历史身份，改名时保持不变；documentRef 和
    node.books 是当前位置投影，随路径迁移。prepare/graph replace/commit 仍由唯一服务
    与跨进程锁负责，signal/provenance 数量不变。
    """
    old_ref = _page_brief_document_ref(old_file_rel)
    new_ref = _page_brief_document_ref(new_file_rel)
    if old_ref == new_ref:
        raise ConceptNodeError(
            "PageBrief 新旧文档路径相同",
            "BW_KG_NODE_DOCUMENT",
        )

    def migrate(graph: dict) -> dict:
        changed_nodes = 0
        migrated_evidence = 0
        migrated_book_refs = 0
        for node in graph.get("nodes", {}).values():
            if not isinstance(node, dict):
                continue
            node_changed = False
            node_evidence = [
                evidence
                for evidence in (node.get("provenance") or [])
                if isinstance(evidence, dict)
            ]
            node_migrated_evidence = 0
            for evidence in node_evidence:
                if (
                    evidence.get("type") == "page-brief"
                    and evidence.get("documentRef") == old_ref
                ):
                    evidence["documentRef"] = new_ref
                    migrated_evidence += 1
                    node_migrated_evidence += 1
                    node_changed = True
            books = [
                str(value)
                for value in (node.get("books") or [])
                if str(value)
            ]
            if node_migrated_evidence:
                projected_books = set(books)
                projected_books.add(new_file_rel)
                old_ref_still_used = any(
                    evidence.get("documentRef") == old_ref
                    for evidence in node_evidence
                )
                authored_ref = str(node.get("authored_ref") or "")
                authored_uses_old = (
                    authored_ref == old_file_rel
                    or authored_ref.startswith(old_file_rel + "#")
                )
                sources = {
                    str(value)
                    for value in (node.get("sources") or [])
                    if str(value)
                }
                signal = int(node.get("signal") or 0)
                # `books` historically is a node-level projection.  Autonotes
                # and other non-PageBrief sources may name their book only
                # there while their documentRef points at a vault note.  A
                # bounded provenance list may also have evicted that source.
                # Remove the old projection only when the retained node state
                # completely proves it came from PageBrief evidence.
                old_book_is_page_brief_only = (
                    bool(sources)
                    and sources <= {"page-brief"}
                    and signal <= len(node_evidence)
                    and all(
                        evidence.get("type") == "page-brief"
                        for evidence in node_evidence
                    )
                )
                if (
                    old_book_is_page_brief_only
                    and not old_ref_still_used
                    and not authored_uses_old
                ):
                    projected_books.discard(old_file_rel)
                normalized_books = sorted(projected_books)
                if normalized_books != sorted(set(books)):
                    node["books"] = normalized_books
                    migrated_book_refs += 1
                    node_changed = True
            if node_changed:
                changed_nodes += 1
        return {
            "contract": "page-brief-document-rename/1",
            "oldDocumentRef": old_ref,
            "newDocumentRef": new_ref,
            "changedNodes": changed_nodes,
            "migratedEvidence": migrated_evidence,
            "migratedBookRefs": migrated_book_refs,
        }

    return (service or ConceptNodeService()).mutate_graph(
        mutation_id=mutation_id,
        source="page-brief-document-rename",
        mutator=migrate,
        operation_contract="kg-op/page-brief-document-rename/1",
        operation_payload={
            "oldFileRel": old_file_rel,
            "newFileRel": new_file_rel,
        },
        history_projections=[{
            "kind": "page-brief-document-ref",
            "oldDocumentRef": old_ref,
            "newDocumentRef": new_ref,
        }],
    )


def promote_page_brief(
    *,
    file_rel: str,
    page: int,
    page_text: str,
    brief: dict,
    service: ConceptNodeService | None = None,
) -> dict:
    """把已生成且已复核的 PageBrief 概念幂等写入唯一节点服务。"""
    semantic_brief = {
        "brief": str((brief or {}).get("brief") or ""),
        "tags": list((brief or {}).get("tags") or []),
        "concepts": list((brief or {}).get("concepts") or []),
        "page_type": str((brief or {}).get("page_type") or ""),
        "subtype": str((brief or {}).get("subtype") or ""),
    }
    source_basis = {
        "file": str(file_rel),
        "page": int(page or 0),
        # 整页文字只用于下方逐字证据复核，不属于 PageBrief 来源身份。
        # 否则 OCR/空白/无关正文漂移会让同一页同一语义结果重复计 signal。
        # model/ts/kg_status/kg_error 都是传输状态，不得改变来源身份。
        "brief": semantic_brief,
    }
    source_digest = hashlib.sha256(
        _canonical_json(source_basis).encode("utf-8")
    ).hexdigest()[:32]
    source_id = "page-brief:" + source_digest
    candidates = page_brief_candidates(
        file_rel=file_rel,
        page=page,
        page_text=page_text,
        brief=semantic_brief,
        source_id=source_id,
    )
    if not candidates:
        return {
            "contract": CONTRACT,
            "mutationId": source_id,
            "txId": None,
            "created": [],
            "anchored": [],
            "updated": [],
            "deduplicated": [],
            "rejected": [],
            "notApplicable": True,
        }
    return (service or ConceptNodeService()).upsert_candidates(
        candidates,
        mutation_id=source_id,
        source="page-brief",
        operation_contract="kg-op/page-brief-promote/1",
        operation_payload=source_basis,
    )


def _main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    promote = subparsers.add_parser("promote-page-brief")
    promote.add_argument("--file", required=True)
    promote.add_argument("--page", type=int, required=True)
    migrate = subparsers.add_parser("migrate-page-brief-document")
    migrate.add_argument("--old-file", required=True)
    migrate.add_argument("--new-file", required=True)
    migrate.add_argument("--mutation-id", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--tx-id", required=True)
    rollback.add_argument("--mutation-id", required=True)
    subparsers.add_parser("recover")
    args = parser.parse_args()
    try:
        if args.command == "promote-page-brief":
            from gen_page_brief import _abs_from_rel, _page_text

            brief = json.loads(sys.stdin.read() or "{}")
            abs_path = _abs_from_rel(args.file)
            if abs_path is None:
                raise ConceptNodeError(
                    "page brief 来源文件不存在",
                    "BW_KG_NODE_SOURCE",
                    {"file": args.file},
                )
            result = promote_page_brief(
                file_rel=args.file,
                page=args.page,
                page_text=_page_text(abs_path, args.file, args.page),
                brief=brief,
            )
        elif args.command == "migrate-page-brief-document":
            result = migrate_page_brief_document(
                old_file_rel=args.old_file,
                new_file_rel=args.new_file,
                mutation_id=args.mutation_id,
            )
        elif args.command == "rollback":
            result = ConceptNodeService().rollback(
                args.tx_id,
                mutation_id=args.mutation_id,
            )
        else:
            result = {
                "contract": CONTRACT,
                "recovered": ConceptNodeService().recover(),
            }
    except ConceptNodeError as exc:
        print(json.dumps({
            "ok": False,
            "contract": CONTRACT,
            "error": str(exc),
            "code": exc.code,
            "details": exc.details,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


__all__ = [
    "CONTRACT",
    "LOG_CONTRACT",
    "ConceptNodeError",
    "ConceptNodeService",
    "migrate_page_brief_document",
    "page_brief_candidates",
    "promote_page_brief",
    "stable_node_id",
]


if __name__ == "__main__":
    raise SystemExit(_main())
