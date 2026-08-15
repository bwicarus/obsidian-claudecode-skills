"""本地 KG 副本读取入口的行为合同。

这一层要防的是**一类特定的假话**：AI 拿着一份旧的、或者根本不存在的副本，
用跟拿着最新权威数据一样的语气回答关于用户书架的问题。

  · 没有副本 ≠ 这本书没有知识点
  · 一份旧图仍然可用，但读出来时必须带着"它有多旧"
  · 从没同步过要能跟"同步过但一本书都没有"区分开
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "lib" / "kg_mirror.py"


def _load():
    spec = importlib.util.spec_from_file_location("kg_mirror_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


KGM = _load() if MODULE_PATH.exists() else None
GRAPH = {"book": "LADR", "nodes": [{"id": "n1", "name": "直和"}], "edges": []}


@unittest.skipIf(KGM is None, "模块不在此工作树")
class MirrorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "kg-mirror"
        self.dir.mkdir(parents=True)
        self._original = KGM.mirror_dir
        KGM.mirror_dir = lambda: self.dir

    def tearDown(self):
        KGM.mirror_dir = self._original
        self._tmp.cleanup()

    def _write(self, book="LADR", graph=None, **entry):
        (self.dir / f"{book}.json").write_text(
            json.dumps(graph or GRAPH, ensure_ascii=False), encoding="utf-8")
        if entry:
            path = self.dir / KGM.MANIFEST_NAME
            current = json.loads(path.read_text("utf-8")) if path.is_file() else {"books": {}}
            current["books"][book] = entry
            path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    def test_missing_mirror_is_not_an_empty_graph(self):
        with self.assertRaises(KGM.MirrorMissing) as caught:
            KGM.load("LADR")
        message = str(caught.exception)
        self.assertIn("不表示这本书没有知识点", message,
                      "错误本身要挡住那句假话，不能只靠调用方自觉")

    def test_load_returns_graph_with_freshness(self):
        self._write(revision="r1", syncedAtEpochSeconds=int(time.time()), status="ok")
        graph, fresh = KGM.load("LADR")
        self.assertEqual(graph["nodes"][0]["id"], "n1")
        self.assertEqual(fresh.revision, "r1")
        self.assertTrue(fresh.is_current)
        self.assertIn("刚同步过", fresh.describe())

    def test_old_copy_is_readable_but_marked(self):
        # 旧图仍然有用 —— 不该拒绝读。但读出来必须知道它旧。
        self._write(revision="r1", status="ok",
                    syncedAtEpochSeconds=int(time.time()) - 5 * 24 * 3600)
        graph, fresh = KGM.load("LADR")
        self.assertEqual(graph["nodes"][0]["id"], "n1", "旧不等于不给")
        self.assertFalse(fresh.is_current)
        self.assertIn("天前", fresh.describe())
        self.assertIn("落后", fresh.describe())

    def test_failed_sync_surfaces_the_reason(self):
        self._write(revision="r1", status="stale", lastError="RuntimeError: 网络中断",
                    syncedAtEpochSeconds=int(time.time()) - 3600)
        _, fresh = KGM.load("LADR")
        self.assertFalse(fresh.is_current)
        self.assertIn("失败", fresh.describe())
        self.assertIn("网络中断", fresh.describe())

    def test_graph_without_manifest_entry_reads_as_unknown_age(self):
        # 清单丢了但文件还在：图可读，但绝不能因此显得新鲜。
        self._write()
        _, fresh = KGM.load("LADR")
        self.assertFalse(fresh.is_current,
                         "不知道多旧时必须当作旧的 —— 未知比明确过时更危险")

    def test_book_visible_even_when_manifest_lost(self):
        self._write()
        self.assertEqual(KGM.available_books(), ["LADR"],
                         "以磁盘为准：清单丢了不该让整本书消失")

    def test_manifest_is_not_listed_as_a_book(self):
        self._write(revision="r1", syncedAtEpochSeconds=int(time.time()), status="ok")
        self.assertNotIn("_mirror", KGM.available_books())

    def test_never_synced_is_distinguishable_from_synced_but_empty(self):
        never = KGM.status()
        self.assertTrue(never["neverSynced"],
                        "启动阶段要能说'还在同步'而不是'你没有图谱'")
        (self.dir / KGM.MANIFEST_NAME).write_text(
            json.dumps({"books": {}}), encoding="utf-8")
        synced_empty = KGM.status()
        self.assertFalse(synced_empty["neverSynced"])

    def test_status_lists_stale_books(self):
        self._write("LADR", revision="r1", status="ok",
                    syncedAtEpochSeconds=int(time.time()))
        self._write("OTHER", revision="r2", status="stale",
                    syncedAtEpochSeconds=int(time.time()) - 60)
        state = KGM.status()
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["stale"], ["OTHER"])

    def test_corrupt_manifest_does_not_hide_the_graph(self):
        self._write()
        (self.dir / KGM.MANIFEST_NAME).write_text("{ broken", encoding="utf-8")
        graph, fresh = KGM.load("LADR")
        self.assertEqual(graph["nodes"][0]["id"], "n1")
        self.assertFalse(fresh.is_current)


if __name__ == "__main__":
    unittest.main()
