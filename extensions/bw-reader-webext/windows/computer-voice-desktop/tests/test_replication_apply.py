from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import replication_apply  # noqa: E402
from replication_apply import (  # noqa: E402
    ReplicationApplyError,
    ReplicationCommandApplier,
    ReplicationDataStore,
    run_once,
    worker_loop,
)
from replication_command_ledger import ReplicationCommandLedger  # noqa: E402


BOOK = "repbook-" + "c" * 32


def envelope(mutation_suffix: str, url: str, method: str, body: dict):
    return {
        "contract": "replication-command/1",
        "deviceId": "native-app-v1-" + "a" * 32,
        "replicationBookId": BOOK,
        "actor": "user",
        "op": {
            "mutationId": "mut-v2-" + mutation_suffix * 32,
            "url": url,
            "method": method,
            "body": body,
        },
    }


def highlight_item(item_id: str = "h_aaaaaa", page: int = 3) -> dict:
    return {
        "file": "localbook:localbook-" + "b" * 64,
        "id": item_id,
        "page": page,
        "rects": [[1, 2, 3, 4]],
        "color": "#ffd54a",
        "text": "抄过来的高亮",
        "note": "",
        "kind": "note",
        "sentence": "",
        "body": "",
        "time": 100,
    }


class ApplierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = ReplicationCommandLedger(self.root / "ledger.sqlite3")
        self.store = ReplicationDataStore(self.root / "replication-data")
        self.state = self.root / "state.json"
        self.dead = self.root / "dead-letter.jsonl"
        self.applier = ReplicationCommandApplier(
            self.ledger, self.store, self.state, self.dead,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _dead_lines(self) -> list[dict]:
        if not self.dead.exists():
            return []
        return [json.loads(line) for line in
                self.dead.read_text("utf-8").splitlines() if line.strip()]

    def test_upsert_patch_tombstone_roundtrip(self) -> None:
        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "POST", highlight_item("h_aaaaaa"),
        ))
        self.ledger.append(envelope(
            "2", "/pdf/api/highlights", "POST", highlight_item("h_bbbbbb", 5),
        ))
        self.ledger.append(envelope(
            "3", "/pdf/api/highlights", "PATCH",
            {"file": "localbook:x", "id": "h_aaaaaa", "note": "改过的备注"},
        ))
        self.ledger.append(envelope(
            "4", "/pdf/api/highlights", "DELETE",
            {"file": "localbook:x", "id": "h_bbbbbb"},
        ))
        report = self.applier.apply_pending()
        self.assertEqual(report["applied"], 4)
        self.assertEqual(report["deadLetters"], [])
        data = self.store.load(BOOK, "pdf-highlights")
        self.assertEqual(data["items"]["h_aaaaaa"]["note"], "改过的备注")
        self.assertEqual(data["items"]["h_aaaaaa"]["page"], 3)
        self.assertNotIn("file", data["items"]["h_aaaaaa"])
        self.assertNotIn("h_bbbbbb", data["items"])
        self.assertIn("h_bbbbbb", data["tombstones"])
        self.assertEqual(data["order"], ["h_aaaaaa"])
        self.assertEqual(self.applier.applied_cursor(), 4)

    def test_reapply_after_cursor_loss_is_idempotent(self) -> None:
        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "POST", highlight_item("h_aaaaaa"),
        ))
        self.applier.apply_pending()
        before = self.store.load(BOOK, "pdf-highlights")
        self.state.unlink()  # 模拟游标在数据落盘后、推进前崩掉
        report = self.applier.apply_pending()
        self.assertEqual(report["applied"], 1)
        self.assertEqual(self.store.load(BOOK, "pdf-highlights"), before)
        self.assertEqual(
            self.store.load(BOOK, "pdf-highlights")["order"], ["h_aaaaaa"],
        )

    def test_poison_commands_dead_letter_loudly_without_wedging(self) -> None:
        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "PATCH",
            {"file": "x", "id": "h_ffffff", "note": "条目不存在"},
        ))
        self.ledger.append(envelope(
            "2", "/pdf/api/notes", "POST", {"id": "c_" + "1" * 16},
        ))  # 执行映射表外
        self.ledger.append(envelope(
            "3", "/pdf/api/highlights", "POST", highlight_item("h_aaaaaa"),
        ))
        self.ledger.append(envelope(
            "4", "/pdf/api/highlights", "PATCH",
            {"file": "x", "id": "h_aaaaaa", "rects": [[9, 9, 9, 9]]},
        ))  # PATCH 表外字段
        report = self.applier.apply_pending()
        self.assertEqual(report["applied"], 1)
        self.assertEqual(len(report["deadLetters"]), 3)
        dead = self._dead_lines()
        self.assertEqual([item["cursor"] for item in dead], [1, 2, 4])
        self.assertIn("表外", dead[1]["error"])
        self.assertEqual(self.applier.applied_cursor(), 4)
        data = self.store.load(BOOK, "pdf-highlights")
        self.assertEqual(list(data["items"]), ["h_aaaaaa"])
        # 表外字段的 PATCH 整条拒收，rects 保持原样
        self.assertEqual(data["items"]["h_aaaaaa"]["rects"], [[1, 2, 3, 4]])

    def test_pair_announcement_registers_minted_link(self) -> None:
        from replication_book_links import ReplicationBookLinkStore

        links = ReplicationBookLinkStore(self.root / "links.json")
        applier = ReplicationCommandApplier(
            self.ledger, self.store, self.state, self.dead, link_store=links,
        )
        peer = "localbook-" + "b" * 64
        self.ledger.append(envelope(
            "1", "/replication/pair", "POST",
            {"peerBookId": peer, "replicationBookId": BOOK,
             "displayName": "LADR.pdf"},
        ))
        self.ledger.append(envelope(
            "2", "/pdf/api/highlights", "POST", highlight_item(),
        ))
        report = applier.apply_pending()
        self.assertEqual(report["applied"], 2)
        self.assertEqual(report["deadLetters"], [])
        link = links.resolve_by_peer(peer)
        self.assertIsNotNone(link)
        self.assertEqual(link.replication_book_id, BOOK)
        # body 与信封身份不一致 → 死信出声
        self.ledger.append(envelope(
            "3", "/replication/pair", "POST",
            {"peerBookId": peer,
             "replicationBookId": "repbook-" + "f" * 32,
             "displayName": "x"},
        ))
        report = applier.apply_pending()
        self.assertEqual(len(report["deadLetters"]), 1)
        self.assertIn("不一致", report["deadLetters"][0]["error"])

    def test_pair_without_link_store_dead_letters(self) -> None:
        self.ledger.append(envelope(
            "1", "/replication/pair", "POST",
            {"peerBookId": "localbook-" + "b" * 64,
             "replicationBookId": BOOK, "displayName": "x"},
        ))
        report = self.applier.apply_pending()
        self.assertEqual(report["applied"], 0)
        self.assertEqual(len(report["deadLetters"]), 1)

    def test_corrupt_state_file_is_loud(self) -> None:
        self.state.write_text("{broken", "utf-8")
        with self.assertRaises(ReplicationApplyError):
            self.applier.apply_pending()

    def test_corrupt_data_file_is_loud_and_command_dead_letters(self) -> None:
        path = self.root / "replication-data" / BOOK / "pdf-highlights.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", "utf-8")
        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "POST", highlight_item(),
        ))
        report = self.applier.apply_pending()
        self.assertEqual(report["applied"], 0)
        self.assertEqual(len(report["deadLetters"]), 1)
        self.assertIn("损坏", report["deadLetters"][0]["error"])


class ResyncAndDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = ReplicationCommandLedger(self.root / "ledger.sqlite3")
        self.store = ReplicationDataStore(self.root / "replication-data")
        self.applier = ReplicationCommandApplier(
            self.ledger, self.store,
            self.root / "state.json", self.root / "dead.jsonl",
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def test_resync_replaces_domain_and_tombstones_the_difference(self) -> None:
        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "POST", highlight_item("h_aaaaaa"),
        ))
        self.ledger.append(envelope(
            "2", "/pdf/api/highlights", "POST", highlight_item("h_bbbbbb", 5),
        ))
        self.applier.apply_pending()
        # App 端权威：只有 h_cccccc 和 h_aaaaaa（h_bbbbbb 已在 App 删除但
        # 删除命令丢了）——重同步必须让本端收敛并给 h_bbbbbb 补墓碑。
        resync_items = [
            {k: v for k, v in highlight_item("h_cccccc", 9).items()},
            {k: v for k, v in highlight_item("h_aaaaaa").items()},
        ]
        self.ledger.append(envelope(
            "3", "/replication/resync", "POST",
            {"domain": "pdf-highlights", "items": resync_items},
        ))
        report = self.applier.apply_pending()
        self.assertEqual(report["deadLetters"], [])
        data = self.store.load(BOOK, "pdf-highlights")
        self.assertEqual(data["order"], ["h_cccccc", "h_aaaaaa"])
        self.assertIn("h_bbbbbb", data["tombstones"])
        self.assertNotIn("h_bbbbbb", data["items"])
        self.assertNotIn("file", data["items"]["h_cccccc"])
        # 幂等：同 resync 重放（新 mutationId）结果不变
        self.ledger.append(envelope(
            "4", "/replication/resync", "POST",
            {"domain": "pdf-highlights", "items": resync_items},
        ))
        self.applier.apply_pending()
        self.assertEqual(
            self.store.load(BOOK, "pdf-highlights")["order"],
            ["h_cccccc", "h_aaaaaa"],
        )

    def test_resync_rejects_unknown_domain_loudly(self) -> None:
        self.ledger.append(envelope(
            "1", "/replication/resync", "POST",
            {"domain": "ink", "items": []},
        ))
        report = self.applier.apply_pending()
        self.assertEqual(len(report["deadLetters"]), 1)

    def test_digest_matches_javascript_canonical_shape(self) -> None:
        import replication_apply as module

        # 与 JS JSON.stringify(canonicalJSONValue(...)) 逐位一致的钉子：
        # 键排序、紧凑分隔、非 ASCII 不转义、int 无小数点、float 最短往返。
        value = [{"b": 1, "a": [1.5, 3, 0.25], "文": "高亮"}]
        self.assertEqual(
            module.canonical_json_for_digest(value),
            '[{"a":[1.5,3,0.25],"b":1,"文":"高亮"}]',
        )

    def test_export_digests_covers_books_and_orders_items(self) -> None:
        import replication_apply as module

        self.ledger.append(envelope(
            "1", "/pdf/api/highlights", "POST", highlight_item("h_aaaaaa"),
        ))
        self.applier.apply_pending()
        output = self.root / "replication-digests.json"
        value = module.export_replication_digests(
            self.root / "replication-data", output,
        )
        self.assertEqual(value["contract"], "replication-digests/1")
        entry = value["books"][BOOK]["pdf-highlights"]
        self.assertEqual(entry["count"], 1)
        self.assertRegex(entry["digest"], r"^[a-f0-9]{64}$")
        # 摘要 = 物化数组的 canonical sha —— App 端按同一规则可复算
        import hashlib as _hashlib

        data = self.store.load(BOOK, "pdf-highlights")
        expected = _hashlib.sha256(module.canonical_json_for_digest(
            module.materialize_domain_items(data)
        ).encode("utf-8")).hexdigest()
        self.assertEqual(entry["digest"], expected)
        self.assertTrue(output.exists())


class RunOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spool = self.root / "runtime" / "replication-spool"
        self.spool.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _spool_line(self, value) -> str:
        return json.dumps({
            "contract": "replication-spool-line/1",
            "receivedAtUtcMs": 7_000,
            "envelope": value,
        })

    def test_run_once_ingests_applies_and_writes_status(self) -> None:
        (self.spool / "inbox-20260823.jsonl").write_text(
            self._spool_line(envelope(
                "1", "/pdf/api/highlights", "POST", highlight_item(),
            )) + "\n",
            "utf-8",
        )
        status = run_once(self.root)
        self.assertTrue(status["ok"])
        self.assertEqual(status["ingested"], 1)
        self.assertEqual(status["applied"], 1)
        self.assertEqual(status["appliedCursor"], 1)
        data = json.loads((
            self.root / "replication-data" / BOOK / "pdf-highlights.json"
        ).read_text("utf-8"))
        self.assertIn("h_aaaaaa", data["items"])
        self.assertTrue(
            (self.root / "replication-apply.status.json").exists(),
            "诊断出口必须已在",
        )
        again = run_once(self.root)
        self.assertTrue(again["ok"])
        self.assertEqual(again["replayed"], 1)
        self.assertEqual(again["applied"], 0)

    def test_run_once_reads_the_bridge_spool_not_a_guessed_path(self) -> None:
        # spool 在 C# 桥的 runtime 目录，不在 BWReader 下 —— 传错根整条管道
        # 会静默空转。这条测试钉住"显式传入的 spool 目录真的被读"。
        bridge_runtime = self.root / "bridge" / "runtime" / "replication-spool"
        bridge_runtime.mkdir(parents=True)
        (bridge_runtime / "inbox-20260823.jsonl").write_text(
            self._spool_line(envelope(
                "9", "/pdf/api/highlights", "POST", highlight_item("h_cccccc"),
            )) + "\n",
            "utf-8",
        )
        local_root = self.root / "BWReader"
        status = run_once(local_root, bridge_runtime)
        self.assertEqual(status["ingested"], 1)
        self.assertEqual(status["applied"], 1)
        self.assertEqual(status["spoolDirectory"], str(bridge_runtime))
        self.assertTrue((
            local_root / "replication-data" / BOOK / "pdf-highlights.json"
        ).exists())

    def test_run_once_survives_missing_spool(self) -> None:
        status = run_once(self.root / "does-not-exist-root")
        # 根目录可建：账本自建目录；spool 缺失只是零摄取
        self.assertTrue(status["ok"])

    def test_worker_loop_contains_failures_and_stops(self) -> None:
        calls = []

        def sleeper(seconds: float) -> None:
            calls.append(seconds)

        worker_loop(
            self.root,
            interval_seconds=5.0,
            sleeper=sleeper,
            should_stop=lambda: len(calls) >= 2,
        )
        self.assertEqual(calls, [5.0, 5.0])

    def test_worker_loop_swallows_exceptions_from_run_once(self) -> None:
        original = replication_apply.run_once
        replication_apply.run_once = lambda root: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        calls = []
        try:
            worker_loop(
                self.root,
                sleeper=lambda seconds: calls.append(seconds),
                should_stop=lambda: len(calls) >= 1,
            )
        finally:
            replication_apply.run_once = original
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
