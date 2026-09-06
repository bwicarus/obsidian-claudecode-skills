"""KJ 页级分析：一页提交 → 节点/定义/前置/别名/记录/边车写回/页标记；未分析与已分析的快照块；重放一致。全部离线。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import pages as PG  # noqa: E402
from kj import wikidata as WD  # noqa: E402
from kj.service import KJService  # noqa: E402

KEY = "a" * 16


class PageAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjp"))
        self.figdir = self.tmp / "pdf-figures"
        self.figdir.mkdir()
        os.environ["KJ_FIGURES_DIR"] = str(self.figdir)
        self.sidecar = self.figdir / f"{KEY}.json"
        self.sidecar.write_text(json.dumps({
            "pdf": "x.pdf",
            "formulas": [{"page": 5, "bbox": [0.1, 0.1, 0.5, 0.2], "conf": 0.9, "latex": None},
                         {"page": 5, "bbox": [0.1, 0.3, 0.5, 0.4], "conf": 0.9, "latex": None},
                         {"page": 6, "bbox": [0.1, 0.1, 0.5, 0.2], "conf": 0.9, "latex": None}],
            "figures_geom": [{"page": 5, "bbox": [0.2, 0.5, 0.8, 0.9], "fbox": [0.2, 0.5, 0.8, 0.9], "fsrc": "yolo", "caption": "图 5.1", "desc": ""}],
        }, ensure_ascii=False), "utf-8")
        self.svc = KJService(self.tmp / "kj.db", render=False, actor="test")
        self.L = self.svc.ledger

    def tearDown(self) -> None:
        self.svc.close()
        os.environ.pop("KJ_FIGURES_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self):
        return {
            "book": KEY, "page": 5, "book_title": "LADR", "summary": "引入特征值并给出存在定理", "kind": ["definition", "theorem"],
            "notation": [{"symbol": "L(V)", "meaning": "V 上全体算子", "concept": "线性映射"}],
            "concepts": [
                {"name": "线性映射", "role": "used", "aliases": ["linear map"]},
                {"name": "特征值", "qid": "Q3553768", "role": "defined",
                 "definition": {"text": "设 T∈L(V)，λ∈F 称为 T 的特征值，若存在 v≠0 使 Tv=λv", "uses": ["线性映射"]}},
                {"name": "特征值存在定理", "kind": "theorem", "role": "stated",
                 "definition": {"text": "有限维非零复向量空间上的每个算子都有特征值", "uses": ["特征值"]}},
            ],
            "formulas": [{"idx": 0, "latex": "Tv=\\lambda v"}, {"idx": 5, "latex": "x"}],
            "figures": [{"idx": 0, "desc": "特征向量在变换下方向不变的示意图"}],
            "exercises": [{"label": "5.A.1", "concepts": ["特征值"]}],
            "pitfalls": [{"text": "λ 可以为 0，特征值为 0 不等于没有特征值", "concept": "特征值"}],
        }

    def test_submit_creates_everything_and_marks_page(self):
        WD.upsert_entity(self.L, "Q3553768", {"zh": "特征值", "en": "eigenvalue", "ja": "固有値"}, {"en": "scale factor"}, {}, [], source="t")
        blk = self.svc.page_block(KEY, 5)
        self.assertEqual(blk["status"], "unanalyzed")
        self.assertEqual([f["idx"] for f in blk["boxes"]["formulas"]], [0, 1])       # 只列这一页的框，idx 页内从 0 起
        self.assertEqual(blk["boxes"]["figures"][0]["caption"], "图 5.1")
        self.assertIn("kj_page_submit", blk["instruction"])

        r = self.svc.page_submit(self._payload())
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["nodes_created"]), 3)
        by = {n["name"]: n for n in r["nodes_created"]}
        self.assertEqual(by["特征值存在定理"]["kind"], "theorem")
        E = by["特征值"]["node_id"]; L_ = by["线性映射"]["node_id"]; T = by["特征值存在定理"]["node_id"]
        self.assertEqual(self.L.node(E)["qid"], "Q3553768")
        self.assertIn("eigenvalue", {a["alias"] for a in self.L.aliases(E)})                       # 绑编号 → 三语回填
        self.assertEqual(len(r["definitions_added"]), 2)
        self.assertEqual({(x["from"], x["to"]) for x in r["prereqs_added"]}, {(L_, E), (E, T)})     # uses 里同页新建的概念按名解析
        self.assertEqual(r["notation_set"], 1)
        self.assertIn(("L(V)", f"page:{KEY}:5"), {(a["alias"], a["origin"]) for a in self.L.aliases(L_)})
        self.assertEqual((r["records_added"], r["records_duplicate"]), (1, 0))
        self.assertEqual((r["sidecar"]["formulas_written"], r["sidecar"]["figures_written"], r["sidecar"]["unmatched"]), (1, 1, ["formula:5"]))
        sc = json.loads(self.sidecar.read_text("utf-8"))
        self.assertEqual((sc["formulas"][0]["latex"], sc["formulas"][0]["latex_engine"], sc["formulas"][1]["latex"]), ("Tv=\\lambda v", "page-analysis", None))
        self.assertTrue(sc["figures_geom"][0]["desc"].startswith("特征向量"))
        self.assertTrue(self.sidecar.with_suffix(".json.bak").exists())

        st = self.svc.page_status(KEY, 5)
        self.assertEqual((st["analyzed"], st["node_count"], st["formulas"]["with_latex"], st["figures"]["with_desc"]), (True, 3, 1, 1))
        blk = self.svc.page_block(KEY, 5)
        self.assertEqual(blk["status"], "analyzed")
        roles = {(n["name"], n["role"]) for n in blk["nodes"]}
        self.assertIn(("特征值", "defined"), roles); self.assertIn(("特征值", "exercised"), roles); self.assertIn(("特征值存在定理", "stated"), roles)
        self.assertEqual(blk["formulas"], [{"idx": 0, "latex": "Tv=\\lambda v"}])
        self.assertEqual(blk["exercises"][0]["node_ids"], [E])
        self.assertEqual(PG.node_pages(self.L, E), [{"book": KEY, "page": 5, "role": "defined"}, {"book": KEY, "page": 5, "role": "exercised"}])

        # 重交：定义已存在不重复、坑按页去重、节点解析而不新建
        r2 = self.svc.page_submit(self._payload())
        self.assertEqual((len(r2["nodes_created"]), len(r2["nodes_resolved"]), len(r2["definition_exists"])), (0, 3, 2))
        self.assertEqual((r2["records_added"], r2["records_duplicate"]), (0, 1))
        self.assertEqual(self.L.db.execute("SELECT COUNT(*) FROM records WHERE node_id=? AND kind='observation'", (E,)).fetchone()[0], 1)

        self.L.rebuild()                                                                              # 投影可从事件重放
        self.assertTrue(self.svc.page_status(KEY, 5)["analyzed"])
        self.assertEqual(len(PG.node_pages(self.L, E)), 2)
        bp = self.svc.book_pages(KEY, total=6)
        self.assertEqual((bp["analyzed_pages"], bp["unanalyzed_pages"], bp["pages_with_boxes"]), ([5], [1, 2, 3, 4, 6], 2))

    def test_ambiguous_and_unresolved_are_reported_not_guessed(self):
        self.svc.create_node(name="极限（数学）", aliases=["极限"])
        self.svc.create_node(name="极限（范畴论）", aliases=["极限"])
        r = self.svc.page_submit({"book": KEY, "page": 7, "concepts": [
            {"name": "极限", "role": "used"},
            {"name": "导数", "role": "defined", "definition": {"text": "导数是极限", "uses": ["不存在"]}}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual([a["name"] for a in r["ambiguous"]], ["极限"])
        self.assertEqual(r["unresolved_uses"], ["不存在"])
        self.assertEqual(sorted(m["name"] for m in r["also_mentioned"]), ["极限（数学）", "极限（范畴论）"])   # 防漏清单是字串匹配：多义两个都列，由 AI 判
        self.assertEqual(self.svc.page_block(KEY, 8)["boxes"]["sidecar"], True)
        os.environ["KJ_FIGURES_DIR"] = str(self.tmp / "nowhere")
        self.assertEqual(self.svc.page_block(KEY, 8)["boxes"]["sidecar"], False)
        self.assertEqual(self.svc.page_status("Z", 0).get("code"), "bad_page")

    def test_book_key_matches_pdf_reader_sha(self):
        import hashlib
        p = self.tmp / "book.pdf"
        p.write_bytes(b"%PDF")
        self.assertEqual(PG.book_key(str(p)), hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:16])
        self.assertEqual(PG.book_key(KEY), KEY)


if __name__ == "__main__":
    unittest.main()
