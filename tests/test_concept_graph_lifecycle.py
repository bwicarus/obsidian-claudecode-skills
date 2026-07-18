#!/usr/bin/env python3
"""概念网生命周期集成测试(R4 审查第 8 条:两晚连续运行——边不丢/墓碑不复活/重复不增/AI 文本不反哺)。
全部隔离(临时 store,不碰真 state/),零 AI。随 unittest discover 进 daily smoke gate。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "kg"))
sys.path.insert(0, str(ROOT / "scripts"))
import promote_concepts as PC  # noqa: E402
import propose_concept_notes as PCN  # noqa: E402
import attention_profile as AP  # noqa: E402


class TwoNightLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (PC.OUT, PC.CONF_FILE)
        PC.OUT = self.tmp / "graph.json"
        PC.CONF_FILE = self.tmp / "conf.json"

    def tearDown(self):
        PC.OUT, PC.CONF_FILE = self._saved

    def test_two_nights_no_mutual_destruction(self):
        g = {"nodes": {}, "edge_claims": {}, "edge_audits": {}, "meta": {}}
        # 夜1:存量扫描两条 + propose 一条(book 来源)
        PC.upsert_claim(g, "A", "B", "prereq", "prereq", "B 依赖 A", "note:x.md", "quote", "aliasscan+sentconfirm")
        PC.upsert_claim(g, "C", "D", "related", "demote", "C 与 D 相关", "note:y.md", "prose", "aliasscan+sentconfirm")
        PC.upsert_claim(g, "E", "B", "prereq", "prereq", "B 用到 E", "book:z.pdf#p3", "book", "forwardsearch+aiclassify")
        edges1 = PC.derive_edges(g)
        self.assertEqual({e["status"] for e in edges1 if e["kind"] == "prereq"}, {"shadow"},
                         "prereq 未审计必须 shadow(不进 availability)")
        # 审计:keep A→B、remove C→D(墓碑)
        g["edge_audits"][PC._edge_id("A", "B")] = {"verdict": "keep", "ts": 1}
        g["edge_audits"][PC._edge_id("C", "D")] = {"verdict": "remove", "ts": 1}
        # 夜2:重扫同 claims(模拟 daily 存量扫描重跑)
        PC.upsert_claim(g, "A", "B", "prereq", "prereq", "B 依赖 A", "note:x.md", "quote", "aliasscan+sentconfirm")
        PC.upsert_claim(g, "C", "D", "related", "demote", "C 与 D 相关", "note:y.md", "prose", "aliasscan+sentconfirm")
        edges2 = PC.derive_edges(g)
        m = {(e["from"], e["to"]): e for e in edges2}
        self.assertIn(("E", "B"), m, "propose 写的边不能被存量扫描冲掉")
        self.assertEqual(m[("A", "B")]["status"], "audited", "审计 keep 不能被重扫覆盖")
        self.assertNotIn(("C", "D"), m, "墓碑不能复活")
        self.assertIn(PC._edge_id("C", "D"), g["edge_claims"], "墓碑只是 overlay,证据 claim 仍可查")
        # 夜3:幂等
        edges3 = PC.derive_edges(g)
        self.assertEqual(len(edges2), len(edges3))
        obs = g["edge_claims"][PC._edge_id("A", "B")]["observations"]
        self.assertEqual(len(obs), 1, "重复 upsert 同一观察不增行")

    def test_override_precedence_and_stable_id(self):
        g = {"edge_claims": {}, "edge_audits": {}}
        PC.upsert_claim(g, "A", "B", "prereq", "prereq", "q", "note:x.md", "quote", "m")
        PC.CONF_FILE.write_text(json.dumps({"edges": {"A|B": False}}), "utf-8")
        self.assertEqual(PC.derive_edges(g), [], "用户否决=最高优先墓碑")
        PC.CONF_FILE.write_text(json.dumps({"edges": {"A|B|prereq": True}}), "utf-8")   # 旧三段键兼容
        e = PC.derive_edges(g)[0]
        self.assertEqual(e["status"], "user_confirmed")
        g["edge_audits"][PC._edge_id("A", "B")] = {"verdict": "remove", "ts": 1}
        e = PC.derive_edges(g)[0]
        self.assertEqual(e["status"], "user_confirmed", "用户 True 压过审计墓碑(可复活)")

    def test_auto_text_never_feeds_edges(self):
        md = ("---\ntype: concept-auto\n---\n# X\n\n## 定义\n"
              "**AI 生成(仅参考,非原文)**:AI文本提到子空间。\n\n"
              "## 概念链接(自动)\n- 相关:[[200-向量空间|向量空间]]\n\n## AI 解释(自动)\n直和相关。\n")
        joined = " ".join(t for t, _ in PC._note_scannable_text(md))
        for bad in ("子空间", "向量空间", "直和", "AI 生成"):
            self.assertNotIn(bad, joined, "AI 生成内容绝不进扫描源(自强化环)")

    def test_english_sentence_split(self):
        s = PC._split_sentences("A vector space is a set V. It satisfies axioms like 1.20 and F^n cases. Next sentence here.")
        self.assertGreaterEqual(len(s), 3)
        self.assertTrue(any("vector space" in x for x in s))


class IdentityResolve(unittest.TestCase):
    def test_note_filename_identity(self):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "资源" / "概念" / "书"
        d.mkdir(parents=True)
        (d / "200-食中毒.md").write_text("---\ntype: concept-auto\naliases: []\n---\n# 食中毒\n", "utf-8")
        saved_vault, saved_em = AP.VAULT_ROOT, PCN.EMERGENT
        try:
            AP.VAULT_ROOT = tmp
            PCN.EMERGENT = tmp / "none.json"
            ident, how = PCN._identity_resolve("食中毒", use_ai=False)
            self.assertEqual(how, "note_filename", "已有 200-食中毒.md 必须被文件名识别,不再判新概念")
            self.assertTrue(ident)
        finally:
            AP.VAULT_ROOT, PCN.EMERGENT = saved_vault, saved_em


if __name__ == "__main__":
    unittest.main()
