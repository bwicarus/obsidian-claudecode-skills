"""KJ 账本：节点/定义/记录/关系登记规则 + 事件重放一致性。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import register as R  # noqa: E402
from kj.service import KJService  # noqa: E402


class KJCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjt"))
        self.svc = KJService(self.tmp / "kj.db", self.tmp / "vault", actor="test")

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node(self, name: str, **kw) -> str:
        r = self.svc.create_node(name=name, **kw)
        self.assertTrue(r["ok"], r)
        return r["node_id"]


class LedgerTests(KJCase):
    def test_duplicate_name_is_flagged_then_merge_moves_content(self):
        a = self.node("向量空间")
        dup = self.svc.create_node(name="向量空间")
        self.assertEqual(dup["possible_duplicate_of"]["id"], a)
        b = dup["node_id"]
        self.assertTrue(self.svc.add_record(b, text="在 B 上的记录", kind="reading")["ok"])
        m = self.svc.merge_node(b, a, reason="同一概念")
        self.assertTrue(m["ok"], m)
        self.assertEqual(self.svc.ledger.resolve(b)["id"], a)
        self.assertEqual(self.svc.node(a)["records"]["total"], 1)
        self.assertEqual(self.svc.search("向量空间")["local"][0]["id"], a)

    def test_definition_same_context_requires_decision(self):
        a = self.node("向量空间")
        src = {"kind": "pdf", "book": "LADR", "page": 12}
        first = self.svc.add_definition(a, text="定义一", source=src)
        self.assertTrue(first["ok"])
        again = self.svc.add_definition(a, text="定义二", source=src)
        self.assertEqual(again["code"], "definition_exists")
        self.assertEqual(again["existing"][0]["text"], "定义一")
        keep = self.svc.add_definition(a, text="定义二", source=src, decision="keep")
        self.assertEqual(keep["code"], "no_change")
        sup = self.svc.add_definition(a, text="定义二", source=src, decision="supersede", supersedes=again["existing"][0]["id"])
        self.assertTrue(sup["ok"], sup)
        other = self.svc.add_definition(a, text="另一本书", source={"kind": "pdf", "book": "Strang", "page": 1})
        self.assertTrue(other["ok"])
        texts = [d["text"] for d in self.svc.node(a)["definitions"]]
        self.assertEqual(texts, ["定义二", "另一本书"])
        with self.assertRaises(R.RegisterError):
            R.add_definition(self.svc.ledger, a, text="无出处", source=None)

    def test_records_append_without_dedupe_then_merge(self):
        a = self.node("向量空间")
        r1 = self.svc.add_record(a, text="读了 p.12", kind="reading", occurred_at="2026-09-01T10:00:00")
        r2 = self.svc.add_record(a, text="读了 p.12", kind="reading")
        self.assertNotEqual(r1["record_id"], r2["record_id"])
        self.assertEqual(self.svc.node(a)["records"]["total"], 2)
        bad = self.svc.merge_records(a, record_ids=[r1["record_id"]], text="x")
        self.assertEqual(bad["code"], "too_few")
        m = self.svc.merge_records(a, record_ids=[r1["record_id"], r2["record_id"]], text="两次读 p.12", occurrences=2)
        self.assertTrue(m["ok"], m)
        recs = self.svc.node(a)["records"]
        self.assertEqual(recs["total"], 1)
        self.assertEqual(recs["latest"][0]["occurrences"], 2)
        self.assertEqual(len(self.svc.ledger.records(a, include_merged=True)), 3)
        self.assertEqual(self.svc.merge_records(a, record_ids=[r1["record_id"], r2["record_id"]], text="再并")["code"], "record_not_found")

    def test_relation_rules(self):
        a, b, c = self.node("A"), self.node("B"), self.node("C")
        self.assertEqual(self.svc.add_relation(from_id=a, to_id=b, type="prereq")["code"], "missing_evidence")
        self.assertEqual(self.svc.add_relation(from_id=a, to_id=a, type="related")["code"], "self_relation")
        self.assertEqual(self.svc.add_relation(from_id=a, to_id=b, type="weird")["code"], "bad_relation_type")
        ab = self.svc.add_relation(from_id=a, to_id=b, type="prereq", evidence="B 的定义用到 A")
        self.assertTrue(ab["ok"], ab)
        self.assertTrue(self.svc.add_relation(from_id=b, to_id=c, type="prereq", evidence="C 用到 B")["ok"])
        cyc = self.svc.add_relation(from_id=c, to_id=a, type="prereq", evidence="想成环")
        self.assertEqual(cyc["code"], "prereq_cycle")
        self.assertEqual(cyc["path"][0], a)
        self.assertEqual(cyc["path"][-1], a)
        self.assertEqual(self.svc.add_relation(from_id=a, to_id=b, type="prereq", evidence="重复")["code"], "relation_exists")
        chg = self.svc.change_relation(ab["relation_id"], reverse=True, evidence="其实 A 依赖 B")
        self.assertTrue(chg["ok"], chg)
        self.assertEqual((chg["from"], chg["to"]), (b, a))
        self.assertEqual(self.svc.ledger.prereqs_of(a), [b])
        self.assertEqual(self.svc.retract_relation(ab["relation_id"])["code"], "already_retracted")
        self.assertTrue(self.svc.retract_relation(chg["relation_id"], reason="错了")["ok"])
        self.assertEqual(self.svc.ledger.prereqs_of(a), [])
        self.assertEqual(len(self.svc.ledger.relations(a, status="retracted")), 2)

    def test_rebuild_replays_to_identical_state(self):
        a, b = self.node("A"), self.node("B")
        self.svc.add_relation(from_id=b, to_id=a, type="prereq", evidence="e")
        q = self.svc.register_quiz(items=[{"item_id": "q1", "question": "?", "node_ids": [b]}, {"item_id": "q2", "question": "?", "node_ids": [a]}])
        self.svc.submit_results(quiz_id=q["quiz_id"], results=[{"item_id": "q1", "result": "wrong"}, {"item_id": "q2", "result": "correct"}])
        self.svc.self_assess(b, value=0.9)
        before = {n: self.svc.ledger.mastery_row(n) for n in (a, b)}
        rb = self.svc.rebuild()
        self.assertTrue(rb["ok"])
        for n in (a, b):
            after = self.svc.ledger.mastery_row(n)
            for k in ("value", "level", "progress", "availability", "readiness", "state", "evidence_count"):
                self.assertEqual(after[k], before[n][k], (n, k))

    def test_anki_snapshot_is_idempotent_per_card_per_day(self):
        a = self.node("A")
        card = self.svc.bind_card(node_ids=[a], anki_note_id=42, anki_card_ids=[7], front="f", back="b")
        self.assertEqual(card["card_key"], "anki:42")
        e1 = R.anki_snapshot(self.svc.ledger, card_id=7, mastery=0.6, node_ids=[a], card_key="anki:42", ts=1_700_000_000)
        e2 = R.anki_snapshot(self.svc.ledger, card_id=7, mastery=0.9, node_ids=[a], card_key="anki:42", ts=1_700_000_100)
        self.assertFalse(e1.duplicate)
        self.assertTrue(e2.duplicate)
        e3 = R.anki_snapshot(self.svc.ledger, card_id=7, mastery=0.9, node_ids=[a], card_key="anki:42", ts=1_700_100_000)
        self.assertFalse(e3.duplicate)
        self.assertEqual(self.svc.bind_card(node_ids=[], anki_note_id=1)["code"], "missing_node")


if __name__ == "__main__":
    unittest.main()
