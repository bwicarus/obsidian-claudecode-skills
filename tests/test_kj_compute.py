"""KJ 掌握度折叠与准备度：近因加权、先验、自评一次性、更正重算、前置 weak/unknown。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import compute  # noqa: E402
from kj.service import KJService  # noqa: E402


class ComputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjc"))
        self.svc = KJService(self.tmp / "kj.db", render=False, actor="test")
        self.a = self.svc.create_node(name="目标")["node_id"]
        self.b = self.svc.create_node(name="前置")["node_id"]

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def quiz(self, node: str, *results: str) -> dict:
        items = [{"item_id": f"q{i}", "question": "?", "node_ids": [node]} for i in range(len(results))]
        q = self.svc.register_quiz(items=items)
        return self.svc.submit_results(quiz_id=q["quiz_id"], results=[{"item_id": f"q{i}", "result": r} for i, r in enumerate(results)])

    def m(self, node: str) -> dict:
        return self.svc.ledger.mastery_row(node)

    def test_prior_and_recency(self):
        self.quiz(self.a, "correct")
        self.assertAlmostEqual(self.m(self.a)["value"], 0.75)
        self.assertEqual(self.m(self.a)["progress"], "in_progress")   # 一题不判 mastered
        self.quiz(self.a, "correct")
        self.assertAlmostEqual(self.m(self.a)["value"], 0.875)
        self.assertEqual(self.m(self.a)["progress"], "mastered")
        self.assertEqual(self.m(self.a)["level"], 5)
        self.quiz(self.a, "wrong")
        self.assertAlmostEqual(self.m(self.a)["value"], 0.4375)     # 新错误立刻显现
        self.assertEqual(self.m(self.a)["progress"], "in_progress")

    def test_unanswered_and_undetermined_do_not_move(self):
        self.quiz(self.a, "correct", "unanswered", "undetermined")
        self.assertAlmostEqual(self.m(self.a)["value"], 0.75)
        self.assertEqual(self.m(self.a)["evidence_count"], 1)

    def test_correction_replays_at_original_position(self):
        items = [{"item_id": "q0", "question": "?", "node_ids": [self.a]}, {"item_id": "q1", "question": "?", "node_ids": [self.a]}]
        q = self.svc.register_quiz(items=items)
        self.svc.submit_results(quiz_id=q["quiz_id"], results=[{"item_id": "q0", "result": "wrong"}, {"item_id": "q1", "result": "correct"}])
        self.assertAlmostEqual(self.m(self.a)["value"], 0.625)   # 0.25 → 0.625
        self.svc.submit_results(quiz_id=q["quiz_id"], results=[{"item_id": "q0", "result": "correct"}])
        self.assertAlmostEqual(self.m(self.a)["value"], 0.875)   # 等价于一开始就 correct, correct
        self.assertEqual(self.m(self.a)["evidence_count"], 2)

    def test_self_assess_sets_once_then_evidence_moves_on(self):
        r = self.svc.self_assess(self.a, value=0.9, reason="确定会了")
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(self.m(self.a)["value"], 0.9)
        self.quiz(self.a, "wrong")
        self.assertAlmostEqual(self.m(self.a)["value"], 0.45)
        self.assertEqual(self.svc.self_assess(self.a, value=1.5)["code"], "bad_value")

    def test_readiness_unknown_weak_ready(self):
        self.svc.add_relation(from_id=self.b, to_id=self.a, type="prereq", evidence="目标定义用到前置")
        r = self.m(self.a)
        self.assertEqual((r["availability"], r["readiness"]), ("open", "unknown_basics"))   # 没记录 ≠ 未掌握
        self.quiz(self.b, "wrong", "wrong")
        r = self.m(self.a)
        self.assertEqual((r["availability"], r["readiness"], r["state"]), ("locked", "needs_basics", "locked"))
        self.assertEqual(r["detail"]["prereqs"]["weak"], [self.b])
        self.svc.self_assess(self.b, value=0.8)
        r = self.m(self.a)
        self.assertEqual((r["availability"], r["readiness"]), ("open", "ready"))

    def test_relation_change_recomputes_downstream(self):
        c = self.svc.create_node(name="更下游")["node_id"]
        self.quiz(self.b, "wrong", "wrong")
        rel = self.svc.add_relation(from_id=self.b, to_id=self.a, type="prereq", evidence="e")
        self.svc.add_relation(from_id=self.a, to_id=c, type="prereq", evidence="e2")
        self.assertEqual(self.m(self.a)["readiness"], "needs_basics")
        self.assertEqual(self.m(c)["readiness"], "unknown_basics")
        self.svc.retract_relation(rel["relation_id"], reason="关系登错")
        self.assertEqual(self.m(self.a)["readiness"], "no_prereq_info")

    def test_cycle_path_helper(self):
        self.svc.add_relation(from_id=self.b, to_id=self.a, type="prereq", evidence="e")
        self.assertEqual(compute.prereq_cycle_path(self.svc.ledger, self.a, self.b), [self.b, self.a, self.b])
        self.assertIsNone(compute.prereq_cycle_path(self.svc.ledger, self.b, self.a))

    def test_quiz_conclusion_codes(self):
        self.svc.add_relation(from_id=self.b, to_id=self.a, type="prereq", evidence="e")
        q = self.svc.register_quiz(target_node=self.a, items=[
            {"item_id": "p1", "question": "?", "node_ids": [self.b]}, {"item_id": "p2", "question": "?", "node_ids": [self.b]},
            {"item_id": "t1", "question": "?", "node_ids": [self.a]}])
        res = self.svc.submit_results(quiz_id=q["quiz_id"], results=[{"item_id": "p1", "result": "wrong"}, {"item_id": "p2", "result": "wrong"},
                                                                     {"item_id": "t1", "result": "wrong"}])
        self.assertEqual(res["conclusion"], "prereq_weak")
        self.assertEqual(res["weak_prereqs"], [self.b])
        q2 = self.svc.register_quiz(target_node=self.a, items=[
            {"item_id": "p1", "question": "?", "node_ids": [self.b]}, {"item_id": "p2", "question": "?", "node_ids": [self.b]},
            {"item_id": "p3", "question": "?", "node_ids": [self.b]}, {"item_id": "t1", "question": "?", "node_ids": [self.a]}])
        res2 = self.svc.submit_results(quiz_id=q2["quiz_id"], results=[{"item_id": "p1", "result": "correct"}, {"item_id": "p2", "result": "correct"},
                                                                       {"item_id": "p3", "result": "correct"}, {"item_id": "t1", "result": "wrong"}])
        self.assertEqual(res2["conclusion"], "prereqs_ok_target_stuck")


if __name__ == "__main__":
    unittest.main()
