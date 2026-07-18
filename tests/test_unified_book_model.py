#!/usr/bin/env python3
"""统一书模型(用户拍板 2026-07-19):**单本书 = 只有一个成员的合并书**。
守两件事:① 领域服务对两种书返回同构结果(单本=1成员/offset 0/恒等翻译);
② 业务层不再靠 `startswith("vbook:")` 分叉——形态判断只允许留在转换层边界。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "_server_deploy"))


class TestUnifiedBookModel(unittest.TestCase):
    def setUp(self):
        import vbook
        self.VB = vbook
        # 挑一本真实存在的单本 PDF(不依赖具体哪本;没有书就跳过)
        import book_groups as BG
        pdfs = sorted(p for p in BG.VAULT.rglob("*.pdf") if p.is_file())
        if not pdfs:
            self.skipTest("vault 里没有 PDF")
        self.single = str(pdfs[0].relative_to(BG.VAULT))

    def test_single_book_is_one_member_group(self):
        """单本书 → 一个成员、offset 0、pages=总页数。"""
        b = self.VB.book(self.single)
        self.assertFalse(b["merged"])
        self.assertEqual(len(b["members"]), 1)
        m = b["members"][0]
        self.assertEqual(m["rel"], self.single)
        self.assertEqual(m["offset"], 0)
        self.assertEqual(m["pages"], b["total"])

    def test_locate_globalize_identity_on_single_book(self):
        """单本书上 locate/globalize 必须是恒等——业务代码才敢无条件调用。"""
        for pg in (1, 3, 7):
            self.assertEqual(self.VB.locate(self.single, pg), (self.single, pg))
            self.assertEqual(self.VB.globalize(self.single, pg), (self.single, pg))

    def test_parts_never_empty(self):
        """parts 对任何引用都给得出成员列表(遍历整本的代码不必判空/判形态)。"""
        self.assertEqual(len(self.VB.parts(self.single)), 1)

    def test_no_form_branching_in_business_layer(self):
        """assistant.py 里不允许再出现 vbook 形态判断:它整个是业务层。
        (转换层边界在 pdf_reader 的 gate / 协议处,不在这里。)"""
        src = (ROOT / "_server_deploy" / "assistant.py").read_text("utf-8")
        hits = re.findall(r'startswith\(\s*["\']vbook:["\']\s*\)|is_view_ref\(', src)
        self.assertEqual(hits, [], f"assistant.py 又长出形态分支({len(hits)} 处)——"
                                   "书相关逻辑应只调 _vb_members/_vb_src/_vb_localize 等统一入口")

    def test_pdf_reader_branching_stays_at_the_boundary(self):
        """pdf_reader 的形态判断只允许留在转换层边界函数里。"""
        src = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")
        allowed = {"_vbook_gate", "_vb_parts", "pdf_api_book_meta", "pdf_view",
                   "pdf_api_notes", "pdf_api_userpages"}
        cur, offenders = "", []
        for line in src.splitlines():
            m = re.match(r"def (\w+)\(", line)
            if m:
                cur = m.group(1)
            if ("is_view_ref(" in line or 'startswith("vbook:")' in line) and cur not in allowed:
                offenders.append(cur)
        self.assertEqual(offenders, [], f"这些函数里冒出了形态分支:{offenders};"
                                        "业务逻辑请改用 _vb_parts/_vb_owner_of/VB.locate")


if __name__ == "__main__":
    unittest.main()
