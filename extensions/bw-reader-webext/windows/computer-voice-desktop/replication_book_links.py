"""Windows 侧的跨设备书身份链接表（两节点复制的前提 A）。

规格：references/reader-two-node-replication.md §8.5；
调查依据：references/book-identity-investigation-20260824.md 方向一/二。

设计要点（全部来自已拍板规格，不要在实现里放宽）：

- **身份 = 配对时铸的内容无关 GUID**（``repbook-<32hex>``），铸后永不重算。
  contentSha256 只做首次配对的会合信号与合并基线，**绝不进入身份**——
  两端各自插入页后摘要必然分叉且永不会合。
- **摘要分叉不断链**。``verifiedNativeRemoteBookBinding`` 用"两端 sha 逐字节
  相等"当门闩，导致每次插入页后 Pi 通道断到重新上传为止；这里刻意不设
  这样的门闩，lastSyncedSha256 只是基线，不是有效性条件。
- **身份断裂（App 改名+改字节同扫描间隔 → 铸新 localbook-id）的重配对是
  App 侧的责任**（只有它有旧记录的指纹/大小与新候选），App 断言后经
  :meth:`ReplicationBookLinkStore.rebind_peer` 显式改绑；Windows 侧只提供
  :meth:`ReplicationBookLinkStore.find_rendezvous_candidates` 供上层判断，
  **不做自动窃取**（同字节两份拷贝是合法状态，自动配错比配不上更糟）。
- 存储：``%LOCALAPPDATA%/BWReader/replication-book-links.json``，
  原子替换写（与 bridge_core / readerpc 同一套约定）。
- **损坏的存储文件必须出声**（抛 :class:`ReplicationLinkStoreError`），
  不能静默当空表重来——静默分叉是这套机制唯一的致命伤
  （references/silent-failure-lessons.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import uuid


REPLICATION_BOOK_LINKS_CONTRACT = "replication-book-links/1"
REPLICATION_BOOK_LINKS_FILE_NAME = "replication-book-links.json"

# 链接数硬上限：远高于任何真实书库，防失控循环把存储文件写爆。
# 超限抛错而不是静默丢弃（no silent caps）。
MAX_LINKS = 4096

_REPLICATION_BOOK_ID_RE = re.compile(r"^repbook-[a-f0-9]{32}$")
# 与 native-local-runtime.js 的 opaqueBookId() 同一形状。
_PEER_BOOK_ID_RE = re.compile(r"^localbook-[A-Za-z0-9_-]{8,160}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

_LINK_KEYS = frozenset(
    (
        "replicationBookId",
        "peerBookId",
        "localRef",
        "displayName",
        "pairedSha256",
        "pairedFileSize",
        "lastSyncedSha256",
        "pairedAtUtcMs",
        "updatedAtUtcMs",
    )
)


class ReplicationLinkStoreError(RuntimeError):
    pass


def _valid_local_ref(value: str) -> bool:
    """Windows 侧书引用 = vault 相对路径，与既有命令通道闸同一判据。"""
    if not value or len(value) > 1024:
        return False
    if value.startswith(("/", "\\")) or "\x00" in value:
        return False
    if ":" in value or ".." in value:
        return False
    return True


def _valid_display_name(value: str) -> bool:
    return 0 < len(value) <= 512 and "\x00" not in value


@dataclass(frozen=True)
class ReplicationBookLink:
    replication_book_id: str
    peer_book_id: str
    paired_sha256: str | None
    paired_file_size: int | None
    last_synced_sha256: str | None
    display_name: str
    local_ref: str | None
    paired_at_utc_ms: int
    updated_at_utc_ms: int

    def to_json(self) -> dict[str, Any]:
        return {
            "replicationBookId": self.replication_book_id,
            "peerBookId": self.peer_book_id,
            "localRef": self.local_ref,
            "displayName": self.display_name,
            "pairedSha256": self.paired_sha256,
            "pairedFileSize": self.paired_file_size,
            "lastSyncedSha256": self.last_synced_sha256,
            "pairedAtUtcMs": self.paired_at_utc_ms,
            "updatedAtUtcMs": self.updated_at_utc_ms,
        }

    @classmethod
    def from_json(cls, value: object) -> "ReplicationBookLink":
        if not isinstance(value, dict) or set(value.keys()) != _LINK_KEYS:
            raise ReplicationLinkStoreError("链接记录字段不符合 replication-book-links/1")
        replication_book_id = value["replicationBookId"]
        peer_book_id = value["peerBookId"]
        paired_sha256 = value["pairedSha256"]
        last_synced_sha256 = value["lastSyncedSha256"]
        display_name = value["displayName"]
        local_ref = value["localRef"]
        paired_file_size = value["pairedFileSize"]
        paired_at = value["pairedAtUtcMs"]
        updated_at = value["updatedAtUtcMs"]
        if (
            not isinstance(replication_book_id, str)
            or not _REPLICATION_BOOK_ID_RE.fullmatch(replication_book_id)
            or not isinstance(peer_book_id, str)
            or not _PEER_BOOK_ID_RE.fullmatch(peer_book_id)
            # sha 允许 None：App 铸 id 的公告式配对（register_minted）没有
            # 内容会合材料；内容会合只在"两端独立看到同一本书"的重配对场景需要。
            or not (paired_sha256 is None
                    or (isinstance(paired_sha256, str)
                        and _SHA256_RE.fullmatch(paired_sha256)))
            or not (last_synced_sha256 is None
                    or (isinstance(last_synced_sha256, str)
                        and _SHA256_RE.fullmatch(last_synced_sha256)))
            or not isinstance(display_name, str)
            or not _valid_display_name(display_name)
            or not (local_ref is None or (isinstance(local_ref, str) and _valid_local_ref(local_ref)))
            or not (paired_file_size is None
                    or (not isinstance(paired_file_size, bool)
                        and isinstance(paired_file_size, int)
                        and paired_file_size >= 0))
            or isinstance(paired_at, bool)
            or not isinstance(paired_at, int)
            or paired_at < 0
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or updated_at < 0
        ):
            raise ReplicationLinkStoreError("链接记录字段值非法")
        return cls(
            replication_book_id=replication_book_id,
            peer_book_id=peer_book_id,
            paired_sha256=paired_sha256,
            paired_file_size=paired_file_size,
            last_synced_sha256=last_synced_sha256,
            display_name=display_name,
            local_ref=local_ref,
            paired_at_utc_ms=paired_at,
            updated_at_utc_ms=updated_at,
        )


def default_links_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"
    return root / REPLICATION_BOOK_LINKS_FILE_NAME


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
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


class ReplicationBookLinkStore:
    """磁盘持久化的链接表。单进程消费（Windows 服务端），每次变更原子落盘。"""

    def __init__(
        self,
        path: Path,
        *,
        clock_utc_ms: Callable[[], int] | None = None,
        mint_id: Callable[[], str] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock_utc_ms or (lambda: int(time.time() * 1000))
        self._mint = mint_id or (lambda: "repbook-" + uuid.uuid4().hex)
        self._links: dict[str, ReplicationBookLink] = {}
        self._load()

    # -- 读取 --------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as error:
            raise ReplicationLinkStoreError(
                f"链接表读取失败（{self._path}）：{error}"
            ) from error
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            # 损坏必须出声：静默当空表重来会让已配对的书全部重新铸 id，
            # 两端从此各说各话且无人知道。
            raise ReplicationLinkStoreError(
                f"链接表 JSON 损坏（{self._path}），拒绝静默重置：{error}"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("contract") != REPLICATION_BOOK_LINKS_CONTRACT
            or not isinstance(value.get("links"), list)
        ):
            raise ReplicationLinkStoreError(
                f"链接表 contract 不符（{self._path}），期望 {REPLICATION_BOOK_LINKS_CONTRACT}"
            )
        links: dict[str, ReplicationBookLink] = {}
        peers: set[str] = set()
        for entry in value["links"]:
            link = ReplicationBookLink.from_json(entry)
            if link.replication_book_id in links or link.peer_book_id in peers:
                raise ReplicationLinkStoreError("链接表含重复的书身份")
            links[link.replication_book_id] = link
            peers.add(link.peer_book_id)
        self._links = links

    def _save(self) -> None:
        _atomic_write_json(
            self._path,
            {
                "contract": REPLICATION_BOOK_LINKS_CONTRACT,
                "links": [
                    link.to_json()
                    for link in sorted(
                        self._links.values(), key=lambda item: item.paired_at_utc_ms
                    )
                ],
            },
        )

    # -- 查询 --------------------------------------------------------------

    def resolve_by_peer(self, peer_book_id: str) -> ReplicationBookLink | None:
        for link in self._links.values():
            if link.peer_book_id == peer_book_id:
                return link
        return None

    def resolve_by_replication_id(self, replication_book_id: str) -> ReplicationBookLink | None:
        return self._links.get(replication_book_id)

    def resolve_by_local_ref(self, local_ref: str) -> ReplicationBookLink | None:
        for link in self._links.values():
            if link.local_ref == local_ref:
                return link
        return None

    def find_rendezvous_candidates(
        self, content_sha256: str, file_size: int
    ) -> list[ReplicationBookLink]:
        """按内容会合信号找候选，供上层（通常是 App 断言链路）判断重配对。

        刻意只返回候选、不自动改绑：同字节两份拷贝是合法状态。
        """
        if not _SHA256_RE.fullmatch(content_sha256):
            raise ReplicationLinkStoreError("contentSha256 形状非法")
        return [
            link
            for link in self._links.values()
            if link.paired_file_size == file_size
            and content_sha256 in (link.paired_sha256, link.last_synced_sha256)
        ]

    # -- 变更 --------------------------------------------------------------

    def pair(
        self,
        *,
        peer_book_id: str,
        content_sha256: str,
        file_size: int,
        display_name: str,
        local_ref: str | None = None,
    ) -> ReplicationBookLink:
        """幂等配对：已有链接原样返回（顺手刷新公告材料），否则铸新身份。

        replicationBookId 一次铸造终身不变——重复 pair 绝不能换 id。
        """
        if not _PEER_BOOK_ID_RE.fullmatch(peer_book_id):
            raise ReplicationLinkStoreError("peerBookId 形状非法（期望 localbook-…）")
        if not _SHA256_RE.fullmatch(content_sha256):
            raise ReplicationLinkStoreError("contentSha256 形状非法")
        if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
            raise ReplicationLinkStoreError("fileSize 非法")
        if not _valid_display_name(display_name):
            raise ReplicationLinkStoreError("displayName 非法")
        if local_ref is not None and not _valid_local_ref(local_ref):
            raise ReplicationLinkStoreError("localRef 必须是无 .. 无冒号的 vault 相对路径")
        existing = self.resolve_by_peer(peer_book_id)
        now = self._clock()
        if existing is not None:
            refreshed = replace(
                existing,
                display_name=display_name,
                local_ref=local_ref if local_ref is not None else existing.local_ref,
                updated_at_utc_ms=now,
            )
            self._links[refreshed.replication_book_id] = refreshed
            self._save()
            return refreshed
        if len(self._links) >= MAX_LINKS:
            raise ReplicationLinkStoreError(f"链接表已达上限 {MAX_LINKS} 条")
        minted = self._mint()
        if not _REPLICATION_BOOK_ID_RE.fullmatch(minted) or minted in self._links:
            raise ReplicationLinkStoreError("铸造的 replicationBookId 非法或撞车")
        link = ReplicationBookLink(
            replication_book_id=minted,
            peer_book_id=peer_book_id,
            paired_sha256=content_sha256,
            paired_file_size=file_size,
            last_synced_sha256=content_sha256,
            display_name=display_name,
            local_ref=local_ref,
            paired_at_utc_ms=now,
            updated_at_utc_ms=now,
        )
        self._links[minted] = link
        self._save()
        return link

    def register_minted(
        self,
        *,
        peer_book_id: str,
        replication_book_id: str,
        display_name: str,
    ) -> ReplicationBookLink:
        """公告式配对：**App 铸的** replicationBookId 在服务端登记。

        无内容会合材料（sha/size 为 None）——App runtime 拿不到全文 sha，
        而 peerBookId+铸好的 id 直接握手也不需要内容会合；内容基线由之后的
        record_sync 补。幂等：同 peer 同 id 原样返回；同 peer **不同 id**
        出声拒绝（App 重装重铸的场景，须人工/重配对流程裁决，绝不静默换身份）。
        """
        if not _PEER_BOOK_ID_RE.fullmatch(peer_book_id):
            raise ReplicationLinkStoreError("peerBookId 形状非法（期望 localbook-…）")
        if not _REPLICATION_BOOK_ID_RE.fullmatch(replication_book_id):
            raise ReplicationLinkStoreError("replicationBookId 形状非法")
        if not _valid_display_name(display_name):
            raise ReplicationLinkStoreError("displayName 非法")
        existing = self.resolve_by_peer(peer_book_id)
        holder = self._links.get(replication_book_id)
        now = self._clock()
        if existing is not None:
            if existing.replication_book_id != replication_book_id:
                raise ReplicationLinkStoreError(
                    "该 peerBookId 已绑定另一个 replicationBookId，拒绝静默换身份"
                )
            refreshed = replace(
                existing, display_name=display_name, updated_at_utc_ms=now,
            )
            self._links[replication_book_id] = refreshed
            self._save()
            return refreshed
        if holder is not None:
            raise ReplicationLinkStoreError(
                "该 replicationBookId 已绑定另一本书，拒绝静默抢占"
            )
        if len(self._links) >= MAX_LINKS:
            raise ReplicationLinkStoreError(f"链接表已达上限 {MAX_LINKS} 条")
        link = ReplicationBookLink(
            replication_book_id=replication_book_id,
            peer_book_id=peer_book_id,
            paired_sha256=None,
            paired_file_size=None,
            last_synced_sha256=None,
            display_name=display_name,
            local_ref=None,
            paired_at_utc_ms=now,
            updated_at_utc_ms=now,
        )
        self._links[replication_book_id] = link
        self._save()
        return link

    def rebind_peer(
        self, replication_book_id: str, new_peer_book_id: str
    ) -> ReplicationBookLink:
        """身份断裂后的显式改绑（App 侧断言）：换 peer id，身份不变。"""
        if not _PEER_BOOK_ID_RE.fullmatch(new_peer_book_id):
            raise ReplicationLinkStoreError("peerBookId 形状非法（期望 localbook-…）")
        link = self._links.get(replication_book_id)
        if link is None:
            raise ReplicationLinkStoreError("要改绑的链接不存在")
        holder = self.resolve_by_peer(new_peer_book_id)
        if holder is not None and holder.replication_book_id != replication_book_id:
            raise ReplicationLinkStoreError(
                "新 peerBookId 已绑在另一条链接上，拒绝静默抢占"
            )
        rebound = replace(
            link,
            peer_book_id=new_peer_book_id,
            updated_at_utc_ms=self._clock(),
        )
        self._links[replication_book_id] = rebound
        self._save()
        return rebound

    def record_sync(
        self, replication_book_id: str, content_sha256: str
    ) -> ReplicationBookLink:
        """一次成功收敛后推进合并基线。基线不是有效性条件，分叉不断链。"""
        if not _SHA256_RE.fullmatch(content_sha256):
            raise ReplicationLinkStoreError("contentSha256 形状非法")
        link = self._links.get(replication_book_id)
        if link is None:
            raise ReplicationLinkStoreError("要记账的链接不存在")
        updated = replace(
            link,
            last_synced_sha256=content_sha256,
            updated_at_utc_ms=self._clock(),
        )
        self._links[replication_book_id] = updated
        self._save()
        return updated

    def set_local_ref(
        self, replication_book_id: str, local_ref: str | None
    ) -> ReplicationBookLink:
        if local_ref is not None and not _valid_local_ref(local_ref):
            raise ReplicationLinkStoreError("localRef 必须是无 .. 无冒号的 vault 相对路径")
        link = self._links.get(replication_book_id)
        if link is None:
            raise ReplicationLinkStoreError("要更新的链接不存在")
        updated = replace(
            link,
            local_ref=local_ref,
            updated_at_utc_ms=self._clock(),
        )
        self._links[replication_book_id] = updated
        self._save()
        return updated

    def links(self) -> list[ReplicationBookLink]:
        return sorted(self._links.values(), key=lambda item: item.paired_at_utc_ms)
