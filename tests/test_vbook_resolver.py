#!/usr/bin/env python3
"""vbook 领域服务单测(转换层 v2 第 1 步):双向解析/边界/越界/stale/fail-closed/拒绝合并。全隔离零 AI。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import book_groups as BG  # noqa: E402
import vbook as VB  # noqa: E402

try:
    import fitz
except Exception:
    fitz = None


def _mk_pdf(path, pages):
    d = fitz.open()
    for i in range(pages):
        p = d.new_page(width=200, height=280)
        p.insert_text((20, 40), "page %d" % (i + 1))
    d.save(str(path))
    d.close()


@unittest.skipIf(fitz is None, "需要 PyMuPDF")
class VbookResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "书").mkdir()
        _mk_pdf(self.tmp / "书" / "测试书part1.pdf", 3)
        _mk_pdf(self.tmp / "书" / "测试书part2.pdf", 5)
        _mk_pdf(self.tmp / "书" / "单本.pdf", 2)
        self._saved = (BG.VAULT, BG.CACHE, VB.STORE, dict(VB._cache))
        BG.VAULT = self.tmp
        BG.CACHE = self.tmp / "pgcache.json"
        VB.STORE = self.tmp / "vbooks.json"
        VB._cache.update({"data": None, "mtime": 0.0})

    def tearDown(self):
        BG.VAULT, BG.CACHE, VB.STORE, cache = self._saved
        VB._cache.update(cache)

    def test_roundtrip_and_boundaries(self):
        g = VB.refresh("书/测试书part1.pdf")
        self.assertIsNotNone(g)
        self.assertEqual(g["total"], 8)
        ref = VB.VIEW_PREFIX + g["group_id"]
        # 双向:全局→源
        self.assertEqual(VB.resolve_view(ref, 1), ("书/测试书part1.pdf", 1))
        self.assertEqual(VB.resolve_view(ref, 3), ("书/测试书part1.pdf", 3))   # part1 末页
        self.assertEqual(VB.resolve_view(ref, 4), ("书/测试书part2.pdf", 1))   # 跨卷边界
        self.assertEqual(VB.resolve_view(ref, 8), ("书/测试书part2.pdf", 5))
        # 源→全局(反译)
        self.assertEqual(VB.to_view("书/测试书part2.pdf", 1), (ref, 4, g["revision"]))
        self.assertEqual(VB.to_view("书/测试书part1.pdf", 3), (ref, 3, g["revision"]))
        # 批量
        self.assertEqual(VB.resolve_pages(ref, [3, 4]),
                         [("书/测试书part1.pdf", 3), ("书/测试书part2.pdf", 1)])

    def test_range_stale_unknown(self):
        g = VB.refresh("书/测试书part1.pdf")
        ref = VB.VIEW_PREFIX + g["group_id"]
        with self.assertRaises(VB.VbookRange):
            VB.resolve_view(ref, 0)
        with self.assertRaises(VB.VbookRange):
            VB.resolve_view(ref, 9)
        with self.assertRaises(VB.VbookStale):
            VB.resolve_view(ref, 1, revision="r_deadbeef00")
        with self.assertRaises(VB.VbookUnknown):
            VB.get("vbook:g_nonexist00")
        # revision 正确 → 通过
        self.assertEqual(VB.resolve_view(ref, 4, revision=g["revision"]), ("书/测试书part2.pdf", 1))

    def test_stable_group_id_and_revision_change(self):
        g1 = VB.refresh("书/测试书part1.pdf")
        # 加一卷 → group_id 不变,revision 变(身份稳定,结构指纹变)
        _mk_pdf(self.tmp / "书" / "测试书part3.pdf", 2)
        g2 = VB.refresh("书/测试书part1.pdf")
        self.assertEqual(g1["group_id"], g2["group_id"])
        self.assertNotEqual(g1["revision"], g2["revision"])
        self.assertEqual(g2["total"], 10)
        # 旧 revision 请求 → stale(绝不静默重解释)
        with self.assertRaises(VB.VbookStale):
            VB.resolve_view(VB.VIEW_PREFIX + g2["group_id"], 5, revision=g1["revision"])

    def test_rejections_and_failclosed(self):
        # 非分卷:不成组
        self.assertIsNone(VB.group_for_rel("书/单本.pdf"))
        self.assertIsNone(VB.to_view("书/单本.pdf", 1))
        # 卷号重复(Part2 大小写变体 → 同 num)→ 拒绝合并
        _mk_pdf(self.tmp / "书" / "测试书Part2.pdf", 1)
        self.assertIsNone(VB.refresh("书/测试书part1.pdf"))
        (self.tmp / "书" / "测试书Part2.pdf").unlink()
        # 成员 0 页(坏文件)→ 拒绝
        (self.tmp / "书" / "测试书part9.pdf").write_bytes(b"not a pdf")
        self.assertIsNone(VB.refresh("书/测试书part1.pdf"))
        (self.tmp / "书" / "测试书part9.pdf").unlink()
        # fail-closed:未适配代码收到 vbook: 必须炸
        with self.assertRaises(VB.VbookUnadapted):
            VB.assert_not_view_ref("vbook:g_x", where="test")
        VB.assert_not_view_ref("书/单本.pdf")   # 真实 rel 不炸


if __name__ == "__main__":
    unittest.main()
