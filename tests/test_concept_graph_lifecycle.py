#!/usr/bin/env python3
"""概念网生命周期集成测试(R4 审查第 8 条:两晚连续运行——边不丢/墓碑不复活/重复不增/AI 文本不反哺)。
全部隔离(临时 store,不碰真 state/),零 AI。随 unittest discover 进 daily smoke gate。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "kg"))
sys.path.insert(0, str(ROOT / "scripts"))
import promote_concepts as PC  # noqa: E402
import propose_concept_notes as PCN  # noqa: E402
import build_unified_graph as BUG  # noqa: E402
import attention_profile as AP  # noqa: E402
from concept_node_service import stable_node_id  # noqa: E402


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

    def test_write_snapshot_is_strict_and_frozen(self):
        g = {"edge_claims": {}, "edge_audits": {}}
        PC.upsert_claim(
            g,
            "A",
            "B",
            "related",
            "related",
            "q",
            "note:x.md",
            "quote",
            "m",
        )
        PC.CONF_FILE.write_text("{broken", "utf-8")
        with self.assertRaises(PC.ConceptNodeError) as caught:
            PC._load_conf_edges(strict=True)
        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_CONFIRMATIONS_CORRUPT",
        )
        PC.CONF_FILE.write_text(
            json.dumps({"edges": {"A|B": True}}),
            "utf-8",
        )
        self.assertEqual(
            PC.derive_edges(
                g,
                confirmation_edges={"A|B": False},
            ),
            [],
            "事务必须消费 receipt 中冻结的确认快照，不能中途重读文件",
        )

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


class UnifiedIdentityLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.saved = (BUG.KG_DIR, BUG.EMERGENT, BUG.OUT, BUG.CONF)
        BUG.KG_DIR = self.tmp / "knowledge_graph"
        BUG.EMERGENT = self.tmp / "emergent.json"
        BUG.OUT = self.tmp / "unified.json"
        BUG.CONF = self.tmp / "confirmations.json"
        BUG.KG_DIR.mkdir()

    def tearDown(self):
        BUG.KG_DIR, BUG.EMERGENT, BUG.OUT, BUG.CONF = self.saved
        self.temp.cleanup()

    def test_persisted_id_survives_and_tombstone_never_reappears(self):
        (BUG.KG_DIR / "book.json").write_text(json.dumps({
            "book": "Book",
            "nodes": [{
                "id": "auth-1",
                "level": 2,
                "name": "Authored",
                "pages": [1],
            }],
            "edges": [],
        }), "utf-8")
        active_id = stable_node_id("active")
        BUG.EMERGENT.write_text(json.dumps({
            "nodes": {
                "active": {
                    "id": active_id,
                    "surface": "Active",
                    "subject": "Subject",
                    "origin": "emergent",
                    "provenance": [],
                },
                "rolled-back": {
                    "id": stable_node_id("rolled-back"),
                    "surface": "Rolled back",
                    "deleted": True,
                    "tombstone": {"rollbackOf": "tx-1"},
                },
                "authored": {
                    "id": stable_node_id("authored"),
                    "surface": "Authored",
                    "in_authored_kg": True,
                    "authored_ref": "Book#auth-1",
                    "provenance": [{"type": "page-brief", "page": 7}],
                },
            },
            "edges": [{
                "from": "active",
                "to": "authored",
                "kind": "related",
                "status": "audited",
                "quote": "evidence",
            }],
            "meta": {},
        }), "utf-8")
        BUG.CONF.write_text(json.dumps({"nodes": {}, "edges": {}}), "utf-8")

        result = BUG.build(write=False)
        ids = {node["id"] for node in result["nodes"]}
        self.assertIn(active_id, ids)
        self.assertNotIn(stable_node_id("rolled-back"), ids)
        self.assertNotIn(stable_node_id("authored"), ids)
        authored = next(node for node in result["nodes"] if node["id"] == "Book::auth-1")
        self.assertEqual(authored["pages"], [1, 7])
        self.assertEqual(authored["emergent_key"], "authored")
        self.assertTrue(any(
            edge["from"] == active_id and edge["to"] == "Book::auth-1"
            for edge in result["edges"]
        ))

    def test_nightly_promote_merges_without_erasing_page_nodes_or_tombstones(self):
        graph = {
            "nodes": {
                "page concept": {
                    "id": stable_node_id("page concept"),
                    "surface": "Page Concept",
                    "origin": "emergent",
                    # This fixture exercises merge preservation, not evidence
                    # migration.  A provenance-bearing graph now requires its
                    # durable mutation journal and is covered by KG-f tests.
                    "signal": 0,
                    "provenance": [],
                },
                "rolled back": {
                    "id": stable_node_id("rolled back"),
                    "surface": "Rolled Back",
                    "origin": "emergent",
                    "deleted": True,
                    "tombstone": {"rollbackOf": "tx-old"},
                },
            },
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        saved = (
            PC.OUT,
            PC.ALIASES_FILE,
            PC.CONF_FILE,
            PC.KG_DIR,
            PC.VAULT,
        )
        PC.OUT = self.tmp / "nightly-emergent.json"
        PC.ALIASES_FILE = self.tmp / "aliases.json"
        PC.CONF_FILE = self.tmp / "confirmations.json"
        PC.KG_DIR = self.tmp / "nightly-kg"
        PC.VAULT = self.tmp / "vault"
        PC.OUT.write_text(json.dumps(graph), "utf-8")
        PC.ALIASES_FILE.write_text("{}", "utf-8")
        PC.CONF_FILE.write_text('{"nodes":{},"edges":{}}', "utf-8")
        PC.KG_DIR.mkdir()
        PC.VAULT.mkdir()
        try:
            with patch.object(PC, "collect_seeds", return_value={
                "nightly concept": {
                    "surface": "Nightly Concept",
                    "sources": {"note"},
                    "signal": 1,
                    "provenance": [{"type": "note", "ref": "000-night.md"}],
                },
            }), patch.object(PC, "_authored_kg_terms", return_value={}):
                result = PC.build(write=True)
                graph_after_first = PC.OUT.read_bytes()
                journal_path = PC.OUT.parent / "kg-node-mutations.jsonl"
                journal_after_first = journal_path.read_bytes()
                repeated = PC.build(write=True)
                self.assertEqual(PC.OUT.read_bytes(), graph_after_first)
                self.assertEqual(
                    journal_path.read_bytes(),
                    journal_after_first,
                    "相同 nightly build 必须 exact replay，不能增长 journal",
                )
                self.assertEqual(
                    repeated["nodes"],
                    result["nodes"],
                )
        finally:
            (
                PC.OUT,
                PC.ALIASES_FILE,
                PC.CONF_FILE,
                PC.KG_DIR,
                PC.VAULT,
            ) = saved

        self.assertIn("page concept", result["nodes"])
        self.assertIs(result["nodes"]["rolled back"]["deleted"], True)
        self.assertEqual(
            result["nodes"]["page concept"]["id"],
            stable_node_id("page concept"),
        )
        self.assertIn("nightly concept", result["nodes"])


if __name__ == "__main__":
    unittest.main()
