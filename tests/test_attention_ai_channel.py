#!/usr/bin/env python3
"""AI 回答入注意力画像(用户设计 2026-07-19)的三条规则:
① 指位词/元话语不是知识点(「这一页」「第五页」「听到」)——它们既污染榜单,
   又让「这轮用户没给出有效词」永远判不出来;
② AI 回答常态极低权重、**仅在用户轮零有效词时**补位提权,且道歉/报错/操作确认不算实质回答;
③ **qa_ai 绝不作为概念网来源**(铁律:AI 说过的话不能变成知识结构,否则自强化环)。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "kg"))


class TestAttentionAIChannel(unittest.TestCase):
    def setUp(self):
        import attention_profile as AP
        self.AP = AP

    def test_positional_words_are_not_terms(self):
        """代词式提问必须抽出**零**有效词——补位逻辑整个依赖这一点。"""
        for q in ("这一页写的是什么?", "那下页写的什么?", "第五页写的是什么?",
                  "翻到最后一页。", "你听到了吗?", "这上一章讲了什么"):
            self.assertEqual(self.AP.extract_terms(q, lang=[]), [], f"{q} 不该抽出词")

    def test_real_terms_survive(self):
        """真术语不能被误伤(过滤宁松勿紧)。"""
        self.assertIn("食中毒", self.AP.extract_terms("食中毒の予防について", lang=["ja"]))
        self.assertIn("公衆衛生学", self.AP.extract_terms("公衆衛生学の第3章", lang=["ja"]))

    def test_ai_substantive_gate(self):
        """道歉/读不出/纯操作确认 → 不是实质回答,整条不入。"""
        for bad in ("抱歉，我还是没能把第74页的文字内容读取出来（可能是纯图）",
                    "哦抱歉，我这边还是没法顺利读取第5页的文字内容",
                    "好的",
                    "已翻到第 80 页！"):
            self.assertFalse(self.AP._ai_substantive(bad), f"{bad[:20]} 应判为非实质")
        self.assertTrue(self.AP._ai_substantive(
            "这页主要是在讲“卫生统计”是什么，以及它包含哪些数据类型。先用平均寿命的例子引入。"))

    def test_boost_only_when_user_turn_empty(self):
        """权重档位:用户轮有词 → 0.3;用户轮零词 → QA_AI_BOOST。"""
        self.assertLess(self.AP.W["qa_ai"], 1.0)
        self.assertGreater(self.AP.QA_AI_BOOST, self.AP.W["qa_ai"])
        self.assertLessEqual(self.AP.QA_AI_BOOST, self.AP.W["qa"],
                             "补位权重不该超过用户自己提问(它终究是间接信号)")

    def test_qa_ai_never_feeds_concept_graph(self):
        """铁律:AI 说过的话不进概念网(既不算深渠道,也不参与渠道计数)。"""
        import propose_concept_notes as P
        self.assertIn("qa_ai", P.NON_CONCEPT_CHANNELS)
        self.assertNotIn("qa_ai", P.DEEP_CHANNELS)


if __name__ == "__main__":
    unittest.main()
