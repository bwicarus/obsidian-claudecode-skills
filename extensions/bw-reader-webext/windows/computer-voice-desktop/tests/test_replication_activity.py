from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import replication_activity  # noqa: E402
from replication_apply import (  # noqa: E402
    ReplicationCommandApplier,
    ReplicationDataStore,
)
from replication_command_ledger import ReplicationCommandLedger  # noqa: E402

BOOK = "repbook-" + "c" * 32


def envelope(suffix: str, url: str, method: str, body: dict) -> dict:
    return {
        "contract": "replication-command/1",
        "deviceId": "native-app-v1-" + "a" * 32,
        "replicationBookId": BOOK,
        "actor": "user",
        "op": {
            "mutationId": "mut-v2-" + suffix * 32,
            "url": url,
            "method": method,
            "body": body,
        },
    }


class ActivityApplyAndQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = ReplicationCommandLedger(
            self.root / "replication-command-ledger.sqlite3"
        )
        self.store = ReplicationDataStore(self.root / "replication-data")
        self.applier = ReplicationCommandApplier(
            self.ledger, self.store, self.root / "state.json",
            self.root / "dead-letter.jsonl",
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _dwell_body(self, with_loc: bool = True) -> dict:
        body = {
            "kind": "dwell",
            "file": "localbook:localbook-" + "b" * 64,
            "client": "native",
            "entries": [{"page": 43, "secs": 120}, {"upage": "u_1234", "secs": 60}],
            "at": int(time.time()),
        }
        if with_loc:
            body["loc"] = {"lat": 35.0, "lon": 139.0, "acc": 12.0,
                           "name": "図書館", "at": int(time.time())}
        return body

    def test_activity_command_appends_jsonl_and_query_sums(self) -> None:
        self.ledger.append(envelope(
            "1", "/replication/activity", "POST", self._dwell_body()))
        self.ledger.append(envelope(
            "2", "/pdf/api/highlights", "POST",
            {"file": "localbook:x", "id": "h_aaaaaa", "page": 3,
             "rects": [[1, 2, 3, 4]], "color": "#fff", "text": "t",
             "note": "", "kind": "note", "sentence": "", "body": "",
             "time": 100},
        ))
        report = self.applier.apply_pending()
        self.assertEqual(report["deadLetters"], [])
        path = self.root / "replication-data" / BOOK / "activity-dwell.jsonl"
        self.assertTrue(path.is_file())
        rows = [json.loads(line) for line in
                path.read_text("utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"]["loc"]["name"], "図書館")
        # 派生查询:dwell 秒数汇总 + 地点归集 + 改删清单
        summary = replication_activity.summarize(self.root, 1.0)
        self.assertEqual(len(summary["books"]), 1)
        book = summary["books"][0]
        self.assertEqual(book["minutes"], 3.0)
        self.assertEqual(book["places"], {"図書館": 180})
        self.assertEqual(len(book["mutations"]), 1)
        self.assertEqual(book["mutations"][0]["kind"], "高亮")
        self.assertEqual(book["mutations"][0]["ops"], ["新建"])
        self.assertEqual(book["mutations"][0]["count"], 1)
        self.assertEqual(book["mutationsOmitted"], 0)

    def test_l0_folds_consecutive_edits_and_caps_output(self) -> None:
        # AI 读取范围设计:同条目连续修改折一行;超 limit 只报"还有 N 条"。
        for index in range(30):
            e = envelope("0", "/pdf/api/notes", "PATCH", None)
            e["op"]["mutationId"] = "mut-v2-" + format(index + 100, "032x")
            e["op"]["body"] = {"file": "localbook:x",
                "id": "n%011d" % (index // 3), "html": {}}
            self.ledger.append(e)
        self.applier.apply_pending()
        summary = replication_activity.summarize(self.root, 1.0, limit=5)
        book = summary["books"][0]
        self.assertEqual(len(book["mutations"]), 5, "L0 截断到 limit")
        self.assertEqual(book["mutationsOmitted"], 5, "10 组折叠后超出 5 组")
        self.assertTrue(all(m["count"] == 3 for m in book["mutations"]),
                        "每组连续 3 次 PATCH 折成一行 ×3")
        detail = replication_activity.summarize(self.root, 1.0, detail=True)
        self.assertEqual(len(detail["books"][0]["mutations"]), 30,
                         "L1 明细不折不截")

    def test_activity_command_is_idempotent_and_shape_gated(self) -> None:
        first = envelope("1", "/replication/activity", "POST", self._dwell_body(False))
        self.ledger.append(first)
        self.ledger.append(first)   # 同 mutationId 幂等
        self.applier.apply_pending()
        path = self.root / "replication-data" / BOOK / "activity-dwell.jsonl"
        rows = path.read_text("utf-8").splitlines()
        self.assertEqual(len(rows), 1, "账本幂等 → 只落一条")
        bad = envelope("2", "/replication/activity", "POST",
                       {"kind": "dwell", "entries": [], "extra": True})
        self.ledger.append(bad)
        report = self.applier.apply_pending()
        self.assertEqual(len(report["deadLetters"]), 1)
        self.assertIn("字段不符", report["deadLetters"][0]["error"])

    def test_digests_export_ignores_activity_jsonl(self) -> None:
        from replication_apply import export_replication_digests
        self.ledger.append(envelope(
            "1", "/replication/activity", "POST", self._dwell_body()))
        self.applier.apply_pending()
        out = self.root / "digests.json"
        export_replication_digests(self.root / "replication-data", out)
        value = json.loads(out.read_text("utf-8"))
        self.assertEqual(value["books"].get(BOOK, {}), {},
                         "activity jsonl 不是数据域,摘要绝不收录")


if __name__ == "__main__":
    unittest.main()
