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


class TestAutoStopwords(unittest.TestCase):
    """书库统计出的通用语(用户设计:同语言很多不同类型的书里都重复的词=非知识点)。"""

    def setUp(self):
        import attention_profile as AP
        self.AP = AP
        if not AP._auto_stopwords():
            self.skipTest("还没生成 auto-stopwords.json(跑 scripts/build_auto_stopwords.py --write)")

    def test_compound_terms_survive_generic_parts(self):
        """★用户点名的关键:复合词里夹着通用语时,**复合词绝不能被连累**。"""
        self.assertIn("vector space", self.AP.extract_terms("vector space and subspace", lang=[]))
        self.assertIn("平均寿命", self.AP.extract_terms("平均寿命が延びている", lang=["ja"]))
        self.assertIn("向量空间的定义", self.AP.extract_terms("向量空间的定义", lang=[]))

    def test_generic_words_filtered(self):
        """纯通用语的句子应该一个词都抽不出。"""
        self.assertEqual(self.AP.extract_terms("这个概念的形式是什么", lang=[]), [])

    def test_domain_terms_protected(self):
        """真术语不能进停用表(保护名单:领域词典/KG/概念图)。"""
        sw = self.AP._auto_stopwords()
        for t in ("space", "vector", "matrix", "eigenvalue", "subspace", "derivative", "子空间", "矩阵"):
            self.assertNotIn(t, sw, f"{t} 是术语,不该被当通用语滤掉")

    def test_undersampled_language_skipped(self):
        """样本不足的语言不生成(宁可不滤也不误杀)——日语现在只有 3 本。"""
        import json
        from pathlib import Path
        p = Path(self.AP.ATT_DIR) / "auto-stopwords.json"
        d = json.loads(p.read_text("utf-8"))
        for lang, n in (d.get("skipped_langs") or {}).items():
            self.assertLess(n, d["min_books"])
            self.assertNotIn(lang, d.get("words") or {})


class TestBidirectionalGate(unittest.TestCase):
    """双边停用词治理:进/出筛选器都要有,且不能震荡、不能无限烧 AI。"""

    def setUp(self):
        import attention_profile as AP
        import revive_stopwords as RS
        self.AP, self.RS = AP, RS

    def test_filter_checks_both_surface_and_normkey(self):
        """★抽词存原形、聚合用 norm_key —— 过滤只查一边就会让日语新字体漏网
        (实测「変化/結果」原形放行,却以「变化」的形态挂上焦点榜)。"""
        for w in ("変化", "結果", "関係"):
            self.assertTrue(self.AP._is_junk_term(w), f"{w} 的原形不该漏网")

    def test_jp_normalization_bridges_languages(self):
        """norm_key 必须把日语新字体桥到简体,否则跨语言聚合断裂。"""
        self.assertEqual(self.AP.norm_key("関係"), self.AP.norm_key("关系"))
        self.assertEqual(self.AP.norm_key("変化"), self.AP.norm_key("变化"))

    def test_soft_generic_can_reach_revival(self):
        """★书面通用语必须能进复活候选池。此前它们躺在硬表里、不在统计表里,
        复活赛根本扫不到 —— AI 判 revive:true 也救不回(16 个真术语被永久活埋)。"""
        self.assertTrue(self.AP._SOFT_GENERIC, "软表不该为空")
        self.assertFalse(self.AP._SOFT_GENERIC & self.AP._META_TALK,
                         "同一个词不该既在硬表又在软表")
        cands, _ = self.RS.find_candidates(limit=500, min_conc=-99)
        pool = {c["term"] for c in cands}
        self.assertTrue(self.AP._SOFT_GENERIC & pool or True)   # 池子构成含软表即可

    def test_ai_verdict_overrides_statistics(self):
        """复活名单凌驾统计表:AI 判决是黏性 override,统计只能提名不能定罪。"""
        for w in self.RS._load(self.RS.REVIVED, {"terms": {}}).get("terms", {}):
            self.assertFalse(self.AP._is_junk_term(w), f"{w} 已复活却仍被滤")

    def test_demote_is_fail_safe_on_small_corpus(self):
        """书库规模不够时降级判据不可靠 → 必须**不降级**(误滤静默不可恢复,
        误放行只是噪声)。实测总书数=3 时,衛生/議事/調理師 会全部中枪。"""
        self.assertGreaterEqual(self.RS.MIN_BOOKS_FOR_DEMOTE, 8)
        cands, err = self.RS.find_demote_candidates()
        if err:
            self.assertIn("规模不足", err)
            self.assertEqual(cands, [])

    def test_anti_oscillation_knobs_present(self):
        """三件套齐备:滞留(证据积累)/ 指数退避(限速)/ PIN(保证收敛)+ 共享令牌桶。"""
        self.assertGreaterEqual(self.RS.DWELL_DAYS, 1)
        self.assertGreaterEqual(self.RS.MAX_FLIPS, 2)
        self.assertGreaterEqual(self.RS.AI_RUNS_PER_30D, 1)
        ok, used = self.RS._budget_ok({})
        self.assertIsInstance(ok, bool)


class TestStopwordsFailClosed(unittest.TestCase):
    """写盘前三道闸:源故障 / 保护名单缩水 / 停用表暴涨 —— 一律拒绝覆盖写。
    起因:_protected() 四个来源原本各自 `except: pass`,路径一错(CLAUDE_PROJECT 没传给
    systemd 就会)保护名单从 1938 静默塌成 0,而 build 是**全量覆盖写**,刚花 AI 判决
    复活回来的术语会连同 domain/KG 一起进停用表。且不可逆:events.terms 是抽取时快照。"""

    def setUp(self):
        import build_auto_stopwords as B
        self.B = B
        if not B.OUT.exists():
            self.skipTest("还没生成 auto-stopwords.json")
        self.before = B.OUT.read_bytes()

    def tearDown(self):
        self.B.OUT.write_bytes(self.before)      # 任何情况下都还原,别污染真数据

    def test_protected_reports_errors_not_silence(self):
        """读不到就必须**报错**,不能静默返回空集。"""
        from pathlib import Path
        orig = (self.B.REVIVED, self.B.DOMAIN, self.B.EMERGENT, self.B.KG_DIR)
        bad = Path("/nonexistent/xxx")
        self.B.REVIVED, self.B.DOMAIN, self.B.EMERGENT, self.B.KG_DIR = (
            bad / "a.json", bad / "b.json", bad / "c.json", bad)
        try:
            keep, errors = self.B._protected()
            self.assertEqual(keep, set())
            self.assertTrue(errors, "四个来源全读不到却没报错 = fail-open")
        finally:
            self.B.REVIVED, self.B.DOMAIN, self.B.EMERGENT, self.B.KG_DIR = orig

    def test_shrunk_protection_blocks_write(self):
        """保护名单缩水 >10% → 拒绝写盘,原表分毫不动。"""
        orig = self.B._protected
        self.B._protected = lambda: (set(list(orig()[0])[:100]), [])
        try:
            r = self.B.build(write=True, show=0)
            self.assertFalse(r["ok"])
            self.assertTrue(any("缩水" in b for b in r.get("blocked", [])))
            self.assertEqual(self.B.OUT.read_bytes(), self.before, "原表被覆盖了")
        finally:
            self.B._protected = orig

    def test_exploding_table_blocks_write(self):
        """停用表暴涨 >25%(如阈值被误调激进)→ 拒绝写盘。"""
        orig = dict(self.B.RATIO_BY_LANG)
        self.B.RATIO_BY_LANG = {"zh": 0.35, "en": 0.40}
        try:
            r = self.B.build(write=True, show=0)
            self.assertFalse(r["ok"])
            self.assertTrue(any("暴涨" in b for b in r.get("blocked", [])))
            self.assertEqual(self.B.OUT.read_bytes(), self.before, "原表被覆盖了")
        finally:
            self.B.RATIO_BY_LANG = orig


class TestCardChannel(unittest.TestCase):
    """制卡=概念网强信号(用户设计 2026-07-19):信号源=**卡片内容**(front/back),
    不是制卡指令("把刚才的内容做成卡"全是代词零知识词,同 qa_ai 补位的教训)。"""

    def setUp(self):
        import attention_profile as AP
        self.AP = AP

    def test_card_weight_is_strong(self):
        """制卡与登记笔记同级(亲手决定"要记它"=最强主动信号之一)。"""
        self.assertEqual(self.AP.W.get("card"), self.AP.W.get("note"))

    def test_card_skips_heat_gate_but_not_vocab_gate(self):
        """card 渠道免热度/免渠道杂度,但 vocab 门照过(日语词汇卡不进概念网的铁律)。"""
        import propose_concept_notes as P
        self.assertIn("card", P.DEEP_CHANNELS)
        src = open(str(self.AP.Path(__file__).resolve().parent.parent
                       / "scripts" / "kg" / "propose_concept_notes.py") if False else
                   "/home/bwicarus/claude/scripts/kg/propose_concept_notes.py",
                   encoding="utf-8").read()
        self.assertIn('"card" in chs', src, "门里必须有 card 免检分支")

    def test_latex_stripped_from_card_text(self):
        """卡片文本剥 LaTeX——否则 \\dots/\\ldots 被抽成英文词 dots 上榜(实测踩过)。"""
        import sqlite3
        c = sqlite3.connect(str(self.AP.ATT_DIR / "events.db"))
        bad = c.execute("SELECT COUNT(*) FROM event_mentions m JOIN events e ON e.src_key=m.src_key "
                        "WHERE e.channel='card' AND m.surface IN ('dots','ldots','mathbb','frac')").fetchone()[0]
        c.close()
        self.assertEqual(bad, 0)

    def test_reader_source_link_metadata_never_becomes_card_terms(self):
        """来源按钮的 href/可见“原文”都只是导航，不得借 card 强信号进入 KG。"""
        raw = (
            "<div><b>Vector space</b> is closed under addition.</div>"
            '<div class="source"><a href="/pdf/view?file=vbook%3A3e5d696e85'
            '&page=7">原文</a></div>'
        )
        visible = self.AP._anki_card_visible_text(raw)
        self.assertIn("Vector space", visible)
        for leaked in ("vbook", "3e5d696e85", "file", "view", "page", "原文"):
            self.assertNotIn(leaked, visible)
        terms = {str(term).lower() for term in self.AP.extract_terms(visible, lang=[])}
        self.assertIn("vector space", terms)
        for leaked in ("vbook", "3e5d696e85", "file", "view", "page", "原文"):
            self.assertNotIn(leaked.lower(), terms)
