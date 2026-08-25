from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from replication_notifications import (  # noqa: E402
    NotificationError,
    NotificationStore,
)


class NotificationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_state_machine_and_archive(self) -> None:
        item = self.store.create(kind="review-due", title="该复习了")
        self.assertEqual(item["state"], "pending")
        acked = self.store.acknowledge(item["id"])
        self.assertEqual(acked["state"], "acknowledged")
        resolved = self.store.resolve(
            item["id"], by="ai", note="用户完成了目标")
        self.assertEqual(resolved["resolvedBy"], "ai")
        self.assertEqual(self.store.open_items(), [], "resolve 即离开 open 表")
        lines = (self.root / "notifications-archive.jsonl") \
            .read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1, "入库归档一条")
        self.assertEqual(json.loads(lines[0])["resolutionNote"],
                         "用户完成了目标")

    def test_dedupe_key_is_idempotent(self) -> None:
        first = self.store.create(
            kind="review-due", title="该复习了", dedupe_key="review-due")
        second = self.store.create(
            kind="review-due", title="该复习了(重复)", dedupe_key="review-due")
        self.assertEqual(first["id"], second["id"],
                         "同 dedupe key 的 open 通知只有一条")

    def test_auto_resolve_item_mutated(self) -> None:
        item = self.store.create(
            kind="fix-card", title="修这张卡",
            auto_resolve={"type": "item-mutated", "itemId": "n123",
                          "op": "修改"})
        untouched = self.store.create(kind="other", title="别的事")
        count = self.store.auto_resolve_against([
            {"kind": "note", "itemId": "n999", "op": "修改"},
            {"kind": "note", "itemId": "n123", "op": "修改"},
        ])
        self.assertEqual(count, 1)
        remaining = [i["id"] for i in self.store.open_items()]
        self.assertEqual(remaining, [untouched["id"]])
        archived = json.loads(
            (self.root / "notifications-archive.jsonl")
            .read_text("utf-8").splitlines()[0])
        self.assertEqual(archived["resolvedBy"], "auto")
        self.assertEqual(archived["id"], item["id"])

    def test_auto_resolve_card_reviewed(self) -> None:
        self.store.create(
            kind="review-card", title="复习这张卡",
            auto_resolve={"type": "card-reviewed", "cardId": "anki_card_9"})
        miss = self.store.auto_resolve_against([
            {"kind": "note", "itemId": "anki_card_9", "op": "修改"},
        ])
        self.assertEqual(miss, 0, "普通改动不算复习")
        hit = self.store.auto_resolve_against([
            {"kind": "review", "itemId": "anki_card_9", "op": "复习"},
        ])
        self.assertEqual(hit, 1)

    def test_expire_and_export(self) -> None:
        self.store.create(kind="a", title="快过期",
                          expires_at_ms=int(time.time() * 1000) - 1)
        keep = self.store.create(kind="b", title="留着")
        self.assertEqual(self.store.expire_due(), 1)
        export = self.root / "runtime" / "notifications-open.json"
        self.store.export_open(export)
        value = json.loads(export.read_text("utf-8"))
        self.assertEqual([i["id"] for i in value["items"]], [keep["id"]])
        self.assertNotIn("dedupeKey", value["items"][0],
                         "投影只带展示字段")

    def test_bad_auto_type_rejected(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="x", title="y",
                              auto_resolve={"type": "nonsense"})


if __name__ == "__main__":
    unittest.main()
