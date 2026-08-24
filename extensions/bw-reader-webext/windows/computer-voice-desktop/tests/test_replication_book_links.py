from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import replication_book_links  # noqa: E402
from replication_book_links import (  # noqa: E402
    REPLICATION_BOOK_LINKS_CONTRACT,
    ReplicationBookLinkStore,
    ReplicationLinkStoreError,
)


PEER_A = "localbook-" + "a" * 64
PEER_B = "localbook-" + "b" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64


class ReplicationBookLinkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "replication-book-links.json"
        self.now = 1_000
        self.minted = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self) -> ReplicationBookLinkStore:
        def clock() -> int:
            self.now += 1
            return self.now

        def mint() -> str:
            self.minted += 1
            return "repbook-" + format(self.minted, "032x")

        return ReplicationBookLinkStore(self.path, clock_utc_ms=clock, mint_id=mint)

    def _pair_a(self, store: ReplicationBookLinkStore):
        return store.pair(
            peer_book_id=PEER_A,
            content_sha256=SHA_1,
            file_size=123,
            display_name="LADR.pdf",
            local_ref="LADR.pdf",
        )

    def test_pair_mints_identity_and_persists(self) -> None:
        link = self._pair_a(self._store())
        self.assertRegex(link.replication_book_id, r"^repbook-[a-f0-9]{32}$")
        self.assertEqual(link.last_synced_sha256, SHA_1)
        raw = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(raw["contract"], REPLICATION_BOOK_LINKS_CONTRACT)
        reloaded = ReplicationBookLinkStore(self.path)
        found = reloaded.resolve_by_peer(PEER_A)
        self.assertIsNotNone(found)
        self.assertEqual(found.replication_book_id, link.replication_book_id)
        self.assertEqual(found.local_ref, "LADR.pdf")

    def test_pair_is_idempotent_and_never_changes_identity(self) -> None:
        store = self._store()
        first = self._pair_a(store)
        second = store.pair(
            peer_book_id=PEER_A,
            content_sha256=SHA_2,  # 摘要已漂移：不影响身份
            file_size=456,
            display_name="LADR-renamed.pdf",
        )
        self.assertEqual(second.replication_book_id, first.replication_book_id)
        self.assertEqual(second.paired_sha256, SHA_1)  # 配对材料是存档，不刷新
        self.assertEqual(second.display_name, "LADR-renamed.pdf")
        self.assertEqual(second.local_ref, "LADR.pdf")  # 未提供则保留
        self.assertEqual(len(store.links()), 1)

    def test_pair_rejects_malformed_inputs(self) -> None:
        store = self._store()
        cases = [
            dict(peer_book_id="book_" + "a" * 32, content_sha256=SHA_1, file_size=1, display_name="x"),
            dict(peer_book_id=PEER_A, content_sha256="zz", file_size=1, display_name="x"),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=-1, display_name="x"),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=True, display_name="x"),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=1, display_name=""),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=1, display_name="x", local_ref="a:b.pdf"),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=1, display_name="x", local_ref="../up.pdf"),
            dict(peer_book_id=PEER_A, content_sha256=SHA_1, file_size=1, display_name="x", local_ref="/abs.pdf"),
        ]
        for case in cases:
            with self.assertRaises(ReplicationLinkStoreError, msg=str(case)):
                store.pair(**case)
        self.assertEqual(store.links(), [])

    def test_rebind_keeps_identity_and_refuses_silent_takeover(self) -> None:
        store = self._store()
        link = self._pair_a(store)
        rebound = store.rebind_peer(link.replication_book_id, PEER_B)
        self.assertEqual(rebound.replication_book_id, link.replication_book_id)
        self.assertEqual(rebound.peer_book_id, PEER_B)
        self.assertIsNone(store.resolve_by_peer(PEER_A))
        other = store.pair(
            peer_book_id=PEER_A,
            content_sha256=SHA_2,
            file_size=9,
            display_name="other.pdf",
        )
        with self.assertRaises(ReplicationLinkStoreError):
            store.rebind_peer(other.replication_book_id, PEER_B)
        with self.assertRaises(ReplicationLinkStoreError):
            store.rebind_peer("repbook-" + "f" * 32, PEER_B)
        # 改绑到自己已持有的 peer = 无操作幂等
        same = store.rebind_peer(rebound.replication_book_id, PEER_B)
        self.assertEqual(same.peer_book_id, PEER_B)

    def test_record_sync_moves_baseline_without_gating(self) -> None:
        store = self._store()
        link = self._pair_a(store)
        updated = store.record_sync(link.replication_book_id, SHA_2)
        self.assertEqual(updated.last_synced_sha256, SHA_2)
        self.assertEqual(updated.paired_sha256, SHA_1)
        # 摘要分叉后一切解析照常 —— 不存在"绑定失效"这种状态
        self.assertIsNotNone(store.resolve_by_peer(PEER_A))
        self.assertIsNotNone(store.resolve_by_local_ref("LADR.pdf"))
        with self.assertRaises(ReplicationLinkStoreError):
            store.record_sync("repbook-" + "f" * 32, SHA_2)

    def test_find_rendezvous_candidates(self) -> None:
        store = self._store()
        link = self._pair_a(store)
        store.record_sync(link.replication_book_id, SHA_2)
        self.assertEqual(len(store.find_rendezvous_candidates(SHA_1, 123)), 1)
        self.assertEqual(len(store.find_rendezvous_candidates(SHA_2, 123)), 1)
        self.assertEqual(store.find_rendezvous_candidates(SHA_1, 999), [])
        self.assertEqual(store.find_rendezvous_candidates("3" * 64, 123), [])
        with self.assertRaises(ReplicationLinkStoreError):
            store.find_rendezvous_candidates("bad", 123)

    def test_missing_file_starts_empty_but_corrupt_file_is_loud(self) -> None:
        self.assertEqual(self._store().links(), [])
        self.path.write_text("{not json", "utf-8")
        with self.assertRaises(ReplicationLinkStoreError):
            ReplicationBookLinkStore(self.path)
        self.path.write_text(json.dumps({"contract": "wrong/1", "links": []}), "utf-8")
        with self.assertRaises(ReplicationLinkStoreError):
            ReplicationBookLinkStore(self.path)

    def test_duplicate_entries_in_file_are_rejected(self) -> None:
        store = self._store()
        link = self._pair_a(store)
        raw = json.loads(self.path.read_text("utf-8"))
        raw["links"].append(dict(raw["links"][0], replicationBookId="repbook-" + "e" * 32))
        self.path.write_text(json.dumps(raw), "utf-8")
        with self.assertRaises(ReplicationLinkStoreError):
            ReplicationBookLinkStore(self.path)
        del link

    def test_set_local_ref_updates_and_clears(self) -> None:
        store = self._store()
        link = self._pair_a(store)
        updated = store.set_local_ref(link.replication_book_id, "dir/new.pdf")
        self.assertEqual(updated.local_ref, "dir/new.pdf")
        cleared = store.set_local_ref(link.replication_book_id, None)
        self.assertIsNone(cleared.local_ref)
        with self.assertRaises(ReplicationLinkStoreError):
            store.set_local_ref(link.replication_book_id, "bad:ref")

    def test_link_cap_is_loud(self) -> None:
        store = self._store()
        original = replication_book_links.MAX_LINKS
        replication_book_links.MAX_LINKS = 1
        try:
            self._pair_a(store)
            with self.assertRaises(ReplicationLinkStoreError):
                store.pair(
                    peer_book_id=PEER_B,
                    content_sha256=SHA_2,
                    file_size=1,
                    display_name="b.pdf",
                )
        finally:
            replication_book_links.MAX_LINKS = original

    def test_register_minted_pairs_without_content_material(self) -> None:
        store = self._store()
        minted = "repbook-" + "d" * 32
        link = store.register_minted(
            peer_book_id=PEER_A,
            replication_book_id=minted,
            display_name="LADR.pdf",
        )
        self.assertEqual(link.replication_book_id, minted)
        self.assertIsNone(link.paired_sha256)
        self.assertIsNone(link.paired_file_size)
        # 幂等：同 peer 同 id 原样返回
        again = store.register_minted(
            peer_book_id=PEER_A,
            replication_book_id=minted,
            display_name="LADR-renamed.pdf",
        )
        self.assertEqual(again.replication_book_id, minted)
        self.assertEqual(len(store.links()), 1)
        # 无内容材料的链接不参与会合候选
        self.assertEqual(store.find_rendezvous_candidates(SHA_1, 0), [])
        # 基线由之后的 record_sync 补
        synced = store.record_sync(minted, SHA_2)
        self.assertEqual(synced.last_synced_sha256, SHA_2)
        # 重开文件仍认识 None 材料
        reloaded = ReplicationBookLinkStore(self.path)
        self.assertIsNotNone(reloaded.resolve_by_peer(PEER_A))

    def test_register_minted_refuses_silent_identity_swap(self) -> None:
        store = self._store()
        minted = "repbook-" + "d" * 32
        store.register_minted(
            peer_book_id=PEER_A, replication_book_id=minted, display_name="a",
        )
        with self.assertRaises(ReplicationLinkStoreError):
            store.register_minted(  # App 重装重铸：同 peer 不同 id
                peer_book_id=PEER_A,
                replication_book_id="repbook-" + "e" * 32,
                display_name="a",
            )
        with self.assertRaises(ReplicationLinkStoreError):
            store.register_minted(  # 同 id 抢别的书
                peer_book_id=PEER_B,
                replication_book_id=minted,
                display_name="b",
            )

    def test_invalid_minted_id_is_rejected(self) -> None:
        store = ReplicationBookLinkStore(self.path, mint_id=lambda: "not-a-repbook-id")
        with self.assertRaises(ReplicationLinkStoreError):
            store.pair(
                peer_book_id=PEER_A,
                content_sha256=SHA_1,
                file_size=1,
                display_name="x",
            )


if __name__ == "__main__":
    unittest.main()
