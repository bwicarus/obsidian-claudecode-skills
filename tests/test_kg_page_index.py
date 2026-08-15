"""「这一页在讲什么概念」的行为合同。

这一层最要紧的不是命中率，是**不许对错书**。快照带上别的书的知识点，助手会
把它当作这页的内容讲出来，而用户没有任何办法看出这是张冠李戴 —— 那比什么
都不带糟得多。所以歧义一律弃权。

其次是别把摘要当原文：图上的 summary 是建图时的概括，助手若拿它当引用，
用户翻到那页会发现书上没有这句话。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "_server_deploy" / "kg_page_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("kg_page_index_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


KPI = _load() if MODULE_PATH.exists() else None

LADR = {
    "book": "LADR",
    "pdf": "/home/bwicarus/obsidian/资源/books/000-LADR/000-LADR4eChinese.pdf",
    "nodes": [
        {"id": "l0.ch1", "level": 0, "name": "向量空间", "pages": [11, 33]},
        {"id": "l1.1A", "level": 1, "name": "R^n 和 C^n", "pages": [12, 19]},
        {"id": "l2.1", "level": 2, "type": "definition", "name": "复数",
         "summary": "形如 a+bi 的数，" + "详" * 200, "pages": [12]},
        {"id": "l2.2", "level": 2, "type": "theorem", "name": "复数运算性质",
         "summary": "交换律与结合律", "pages": [12, 13]},
    ],
    "edges": [],
}


@unittest.skipIf(KPI is None, "模块不在此工作树")
class PageIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "knowledge_graph"
        self.dir.mkdir(parents=True)
        self._original = KPI._kg_dir
        KPI._kg_dir = lambda: self.dir
        KPI._reset_caches_for_tests()

    def tearDown(self):
        KPI._kg_dir = self._original
        KPI._reset_caches_for_tests()
        self._tmp.cleanup()

    def _write(self, name, payload):
        (self.dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ── 匹配 ────────────────────────────────────────────────────────
    def test_matches_by_source_filename(self):
        self._write("LADR", LADR)
        book, _ = KPI.book_for_file("资源/books/000-LADR/000-LADR4eChinese.pdf")
        self.assertEqual(book, "LADR")

    def test_path_differences_do_not_break_the_match(self):
        # 建图记的是建图机器上的绝对路径，阅读器给的是 vault 相对路径。
        # 只有文件名是两边都成立的东西。
        self._write("LADR", LADR)
        for probe in (
            "000-LADR4eChinese.pdf",
            "别的/目录/000-LADR4eChinese.pdf",
            "别的\\目录\\000-LADR4eChinese.pdf",
            "资源/books/000-LADR/000-LADR4echinese.PDF",
        ):
            with self.subTest(probe=probe):
                self.assertEqual(KPI.book_for_file(probe)[0], "LADR")

    def test_ambiguity_declines_rather_than_guesses(self):
        # 两张图声称来自同一个文件。猜哪张都可能把另一本书的概念
        # 讲成这页的内容，而用户看不出来。
        self._write("LADR", LADR)
        self._write("LADR-copy", dict(LADR, book="LADR-copy"))
        book, reason = KPI.book_for_file("资源/books/000-LADR/000-LADR4eChinese.pdf")
        self.assertIsNone(book, "歧义时必须弃权")
        self.assertIn("无法确定", reason)

    def test_unknown_book_says_so(self):
        self._write("LADR", LADR)
        book, reason = KPI.book_for_file("资源/books/别的书.pdf")
        self.assertIsNone(book)
        self.assertIn("还没有建过", reason, "要能跟'匹配失败'区分开")

    def test_backup_files_are_not_candidates(self):
        # 备份跟正本的 pdf 字段相同，不排除掉就永远歧义 —— 于是每本书都弃权。
        self._write("LADR", LADR)
        self._write("LADR.bak", LADR)
        self._write("kg_audit", LADR)
        self.assertEqual(
            KPI.book_for_file("000-LADR4eChinese.pdf")[0], "LADR")

    # ── 取节点 ──────────────────────────────────────────────────────
    def test_page_gives_section_and_concepts(self):
        self._write("LADR", LADR)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        self.assertTrue(out["available"])
        self.assertEqual(out["section"]["name"], "R^n 和 C^n")
        self.assertEqual([c["name"] for c in out["concepts"]],
                         ["复数", "复数运算性质"])

    def test_section_covers_its_whole_range(self):
        self._write("LADR", LADR)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 18)
        self.assertTrue(out["available"], "第 18 页在 [12,19] 区间内")
        self.assertEqual(out["section"]["name"], "R^n 和 C^n")
        self.assertEqual(out["concepts"], [], "这页没有 L2 节点")

    def test_page_outside_everything_is_explicit(self):
        self._write("LADR", LADR)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 900)
        self.assertFalse(out["available"])
        self.assertIn("没有对应节点", out["reason"],
                      "'这页没节点'和'这段代码没跑'必须能分开")

    def test_summary_is_marked_as_not_the_original_text(self):
        self._write("LADR", LADR)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        self.assertIn("不是本页原文", out["note"],
                      "不说清楚，助手会拿概括当引用，用户翻到那页会发现书上没这句")

    def test_long_summary_is_cut_and_says_so(self):
        self._write("LADR", LADR)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        first = out["concepts"][0]
        self.assertLessEqual(len(first["summary"]), KPI.SUMMARY_LIMIT)
        self.assertTrue(first["summary_truncated"])

    def test_many_concepts_are_capped_and_the_remainder_reported(self):
        crowded = dict(LADR, nodes=LADR["nodes"] + [
            {"id": f"l2.x{i}", "level": 2, "name": f"概念{i}", "pages": [12]}
            for i in range(10)
        ])
        self._write("LADR", crowded)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        self.assertEqual(len(out["concepts"]), KPI.MAX_CONCEPTS_PER_PAGE)
        self.assertEqual(out["concepts_truncated"], 12 - KPI.MAX_CONCEPTS_PER_PAGE)

    def test_no_graph_never_reads_as_no_concepts(self):
        out = KPI.knowledge_for_page("资源/books/没建过图.pdf", 5)
        self.assertFalse(out["available"])
        self.assertIn("还没有建过", out["reason"])
        self.assertEqual(out["concepts"], [])

    def test_corrupt_graph_does_not_raise(self):
        (self.dir / "LADR.json").write_text("{ broken", encoding="utf-8")
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        self.assertFalse(out["available"], "坏文件不该让翻页出错")

    def test_mastery_is_not_carried_into_the_snapshot(self):
        # 快照是"这页讲什么"，不是"你学得怎样"。掌握度另有权威来源，
        # 混进来会让助手拿一份没人保证新鲜的学习状态下断言。
        loaded = dict(LADR, nodes=[
            dict(node, mastery=0.9, state="mastered", containing_notes=["a.md"])
            for node in LADR["nodes"]
        ])
        self._write("LADR", loaded)
        out = KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)
        blob = json.dumps(out, ensure_ascii=False)
        for leaked in ("mastery", "mastered", "containing_notes"):
            self.assertNotIn(leaked, blob)

    def test_rebuilt_graph_is_picked_up(self):
        # 缓存按 mtime 失效。不失效的话，重新建图之后助手会一直讲旧结构。
        self._write("LADR", LADR)
        self.assertEqual(
            KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)["section"]["name"],
            "R^n 和 C^n")
        renamed = json.loads(json.dumps(LADR))
        renamed["nodes"][1]["name"] = "改名后的小节"
        path = self.dir / "LADR.json"
        path.write_text(json.dumps(renamed, ensure_ascii=False), encoding="utf-8")
        import os
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))
        self.assertEqual(
            KPI.knowledge_for_page("000-LADR4eChinese.pdf", 12)["section"]["name"],
            "改名后的小节")


if __name__ == "__main__":
    unittest.main()
