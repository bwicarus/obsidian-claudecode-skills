"""KJ Markdown 页（可重建视图）与渐进式查询。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import markdown as MD  # noqa: E402
from kj.service import KJService  # noqa: E402


class MarkdownQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjm"))
        self.vault = self.tmp / "KJ"
        self.svc = KJService(self.tmp / "kj.db", self.vault, actor="test")
        self.a = self.svc.create_node(name="向量空间", aliases=["vector space"], summary="八条公理")["node_id"]
        self.b = self.svc.create_node(name="向量加法")["node_id"]
        self.svc.add_relation(from_id=self.b, to_id=self.a, type="prereq", evidence="定义用到加法")
        self.svc.add_definition(self.a, text="满足八条公理的集合", source={"kind": "pdf", "book": "LADR", "page": 12})

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def page(self, node: str) -> Path:
        n = self.svc.ledger.node(node)
        return self.vault / "节点" / MD.filename_for(node, n["name"])

    def test_page_layout_and_links(self):
        text = self.page(self.a).read_text("utf-8")
        self.assertTrue(text.startswith("---\nkj_id: " + self.a))
        self.assertIn("# 向量空间", text)
        self.assertIn("别名：vector space", text)
        self.assertIn("- 满足八条公理的集合（pdf：LADR · p.12）", text)
        self.assertIn(f"[[向量加法·{self.b.split(':')[1]}|向量加法]] — 定义用到加法", text)
        self.assertIn("readiness: unknown_basics", text)
        self.assertEqual(MD.node_id_from_file(self.page(self.a)), self.a)
        self.assertIn("[[向量空间·", (self.vault / MD.INDEX_NAME).read_text("utf-8"))

    def test_rename_moves_file_and_merge_removes_page(self):
        old = self.page(self.a)
        self.assertTrue(old.exists())
        self.assertTrue(self.svc.update_node(self.a, name="线性空间")["ok"])
        self.assertFalse(old.exists())
        self.assertTrue(self.page(self.a).exists())
        self.assertIn("[[线性空间·", self.page(self.b).read_text("utf-8"))   # 邻居页里的链接也跟着重写
        page_b = self.page(self.b)
        self.svc.merge_node(self.b, self.a)
        self.assertFalse(page_b.exists())
        self.assertEqual(self.svc.rebuild_markdown()["pages"], 1)

    def test_search_local_first_then_exact_first(self):
        c = self.svc.create_node(name="向量")["node_id"]
        res = self.svc.search("向量")
        self.assertEqual(res["local"][0]["id"], c)                       # 精确命中排最前
        self.assertEqual({x["id"] for x in res["local"]}, {self.a, self.b, c})
        self.assertEqual([x["id"] for x in self.svc.search("vector space")["local"]], [self.a])   # 别名 FTS
        self.assertEqual([x["id"] for x in self.svc.search("八条公理")["local"]], [self.a])       # 定义正文
        self.assertIn("hint", self.svc.search("不存在的东西"))

    def test_browse_and_detail(self):
        root = self.svc.browse()
        self.assertEqual(root["total_nodes"], 2)
        self.assertEqual({n["id"] for n in root["groups"]["concept"]}, {self.a, self.b})
        self.svc.add_relation(from_id=self.b, to_id=self.a, type="part_of")
        self.assertEqual([n["id"] for n in self.svc.browse(self.a)["children"]], [self.b])
        d = self.svc.node(self.a)
        self.assertEqual(d["prereqs"][0]["node"]["id"], self.b)
        self.assertTrue(d["next_hint"].startswith("unknown_basics"))
        self.assertEqual(self.svc.node("kj:0000000000")["code"], "node_not_found")
        self.assertEqual(self.svc.node(self.b)["path"], [{"node": self.a, "name": "向量空间", "via": "part_of"}])


if __name__ == "__main__":
    unittest.main()
