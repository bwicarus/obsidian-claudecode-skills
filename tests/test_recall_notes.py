"""`recall.notes` 直接命令的合同回归(Codex 2026-07-29 12:20 定合同,12:31/12:39 复核)。

**为什么必须是持久测试**:此前我只用一次性脚本"验证通过"就交接,结果连错四次
(KG 路径写成 page-cache 目录、in_progress 整类被排除、备份图混入、复合词退化),
每次都在自造 fixture 上显示全绿。临时脚本不进回归,等于没验证过。

⚠ 本机没有真实 KG/Anki/索引数据,这里的绿灯**不能替代**在 Pi 上跑真实查询。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_direct_wire as W  # noqa: E402

# 统一返回字段(合同):三源必须一致,不能各给各的。
ENVELOPE = {"query", "results", "count", "total", "truncated", "complete", "sourceStatus"}
ENTRY_CORE = {"note", "subject", "keywords", "summary", "src"}


def _fake_pdf(root: Path):
    return types.SimpleNamespace(
        CLAUDE_DIR=root,
        _reader_storage_identity_current=lambda: types.SimpleNamespace(user_id=7),
        _safe_vault_path=lambda r: "/tmp/x" if r else None,
        _page_text_clean=lambda *a, **k: "",
        _epub_section_paragraphs=lambda *a, **k: [],
        _upages_load=lambda r: [], _upages_save=lambda *a: None,
        _hl_load=lambda r: {"highlights": []}, _notes_load=lambda r: [],
        _reader_sidecar_path=lambda *a, **k: None, _ink_load=lambda r: {},
    )


class RecallNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "index").mkdir()
        (self.root / "knowledge_graph").mkdir()
        (self.root / "anki" / "records").mkdir(parents=True)
        self.handler = W.build_handlers(_fake_pdf(self.root))[0]["recall.notes"]

    def tearDown(self) -> None:
        self.dir.cleanup()

    def _kg(self, name: str, nodes: list) -> None:
        (self.root / "knowledge_graph" / name).write_text(
            json.dumps({"nodes": nodes}, ensure_ascii=False), encoding="utf-8")

    def _anki(self, name: str, cards: list, **top) -> None:
        (self.root / "anki" / "records" / name).write_text(
            json.dumps(dict({"cards": cards}, **top), ensure_ascii=False), encoding="utf-8")

    def _index(self, rel: str, text: str) -> None:
        p = self.root / "index" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def call(self, **params):
        return self.handler({}, params, None)

    # ── 入参合同 ────────────────────────────────────────────────────────
    def test_query_is_required_and_not_inherited(self) -> None:
        with self.assertRaises(ValueError):
            self.call()
        with self.assertRaises(ValueError):
            self.call(query="   ")

    def test_query_max_80_chars(self) -> None:
        self.call(query="x" * 80)
        with self.assertRaises(ValueError):
            self.call(query="x" * 81)

    def test_limit_capped_at_8(self) -> None:
        self._kg("g.json", [{"name": f"线性代数{i:02d}", "progress": "mastered"}
                            for i in range(20)])
        r = self.call(query="线性代数", limit=99)
        self.assertEqual(r["count"], 8)
        self.assertEqual(r["total"], 20, "total 是命中总数,不是返回数")
        self.assertTrue(r["truncated"])

    # ── 复合词:整串子串匹配不够 ────────────────────────────────────────
    def test_compound_query_matches_via_bigram(self) -> None:
        """编排器常把「向量空间的定义」传成「向量空间定义」,索引里是「向量空间」。

        整串子串匹配必然 0 命中 —— 必须靠实词 + CJK bigram 评分。
        """
        self._index("数学.md",
                    "# 数学索引\n### 线性代数\n"
                    "- [[000-向量空间]] `向量空间, 线性组合, 基` — 定义向量空间的八条公理\n")
        r = self.call(query="向量空间定义")
        self.assertEqual(r["count"], 1, "复合词评分退化会让这条返回 0")
        self.assertEqual(r["results"][0]["note"], "000-向量空间")

    def test_single_char_query_is_noise_and_returns_nothing(self) -> None:
        """单字实词不参与评分(噪声太大),这是有意行为,不是漏召回。"""
        self._index("数学.md", "- [[甲乙丙]] `甲` — 甲的说明\n")
        self.assertEqual(self.call(query="甲")["count"], 0)

    # ── 知识索引 ───────────────────────────────────────────────────────
    def test_knowledge_index_summary_rows_are_not_entries(self) -> None:
        """index-format.md 明写不要读 knowledge-index.md 的条目内容(它只有科目汇总)。"""
        self._index("knowledge-index.md", "# 知识索引\n- 数学索引 42 条\n")
        self._index("数学.md", "- [[微积分基础]] `微积分, 极限` — 极限的定义\n")
        r = self.call(query="数学索引")
        self.assertTrue(all("42 条" not in str(x["summary"]) for x in r["results"]),
                        f"汇总行被当成知识条目:{r['results']}")

    def test_branch_index_is_scanned_recursively(self) -> None:
        """分支超 30 条会拆到 index/{科目}/{分支}.md,不递归就整片漏掉。"""
        self._index("数学/微积分.md",
                    "### 极限\n- [[夹逼定理]] `极限, 夹逼定理, 收敛` — 用上下界确定极限值\n")
        r = self.call(query="夹逼定理")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["subject"], "数学", "子目录名即科目")

    # ── KG 学习证据(四条并集)──────────────────────────────────────────
    def test_progress_states_map_to_evidence(self) -> None:
        self._kg("g.json", [{"name": "线性映射", "progress": "mastered"}])
        self.assertEqual(self.call(query="线性映射")["results"][0]["evidence"], "mastered")
        self._kg("g.json", [{"name": "线性映射", "progress": "in_progress"}])
        self.assertEqual(self.call(query="线性映射")["results"][0]["evidence"], "started")

    def test_unseen_with_containing_notes_still_counts(self) -> None:
        """Pi 上真实存在 progress=unseen 但记过笔记的节点,不能漏。"""
        self._kg("g.json", [{"name": "特征值", "progress": "unseen",
                             "containing_notes": ["a.md", "b.md"]}])
        r = self.call(query="特征值")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["note_count"], 2)
        self.assertEqual(r["results"][0]["note"], "a.md", "note 用真实笔记名")

    def test_unseen_with_mastery_still_counts(self) -> None:
        self._kg("g.json", [{"name": "行列式", "progress": "unseen", "mastery": 0.4}])
        self.assertEqual(self.call(query="行列式")["count"], 1)

    def test_truly_unseen_is_excluded(self) -> None:
        self._kg("g.json", [{"name": "张量积", "progress": "unseen",
                             "containing_notes": [], "mastery": 0}])
        self.assertEqual(self.call(query="张量积")["count"], 0)

    def test_mastery_true_is_not_treated_as_one(self) -> None:
        self._kg("g.json", [{"name": "对偶空间", "progress": "unseen", "mastery": True}])
        self.assertEqual(self.call(query="对偶空间")["count"], 0, "bool 不是掌握度")

    def test_backup_graphs_are_skipped(self) -> None:
        node = [{"name": "正交基", "progress": "mastered"}]
        self._kg("book.json", node)
        for bad in ("book.bak.json", "book.pre.json", "book.scan.json",
                    "book.tmp.json", "book.old.json", "_draft.json"):
            self._kg(bad, node)
        self.assertEqual(self.call(query="正交基")["total"], 1, "备份被当成现状召回")

    # ── Anki ───────────────────────────────────────────────────────────
    def test_cloze_card_body_is_searched(self) -> None:
        """只搜 front/back 会让本地明明有的 cloze 卡返回空(实测漏 25 张)。"""
        self._anki("n.json", [{"text": "零向量记作 {{c1::0}}"}])
        r = self.call(query="零向量记作")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["type"], "cloze")

    def test_tags_are_searchable(self) -> None:
        """tags 里的 qa_improved 这类标记是用户常用检索入口。"""
        self._anki("n.json", [{"front": "无关正文", "tags": ["qa_improved", "线代"]}])
        r = self.call(query="qa_improved")
        self.assertEqual(r["count"], 1)
        self.assertIn("qa_improved", r["results"][0]["keywords"])

    def test_source_note_is_used_not_file_stem(self) -> None:
        """嵌套卡的归属要用 record 里的真实 source_note,不能拿文件名顶替。"""
        self._anki("nested-file.json", [{"front": "特殊矩阵的性质"}],
                   source_note="000-矩阵.md")
        self.assertEqual(self.call(query="特殊矩阵")["results"][0]["note"], "000-矩阵.md")

    def test_card_level_source_note_wins(self) -> None:
        self._anki("f.json", [{"front": "伴随矩阵定义", "source_note": "卡内.md"}],
                   source_note="文件级.md")
        self.assertEqual(self.call(query="伴随矩阵")["results"][0]["note"], "卡内.md")

    # ── 返回合同 ───────────────────────────────────────────────────────
    def test_envelope_fields_exact(self) -> None:
        self.assertEqual(set(self.call(query="任意查询")), ENVELOPE)

    def test_all_sources_share_the_same_core_fields(self) -> None:
        """三源结果必须同构,否则上游要为每种来源写一套解析。"""
        self._index("数学.md", "- [[共同词条]] `共同词, 索引` — 索引侧摘要\n")
        self._kg("g.json", [{"name": "共同词条", "progress": "mastered"}])
        self._anki("n.json", [{"front": "共同词条是什么"}])
        r = self.call(query="共同词条")
        self.assertEqual({x["src"] for x in r["results"]}, {"index", "kg", "anki"})
        for x in r["results"]:
            self.assertTrue(ENTRY_CORE <= set(x), f"{x['src']} 缺字段:{ENTRY_CORE - set(x)}")

    def test_empty_hit_is_success(self) -> None:
        r = self.call(query="根本不存在的词条")
        self.assertEqual((r["count"], r["total"]), (0, 0))
        self.assertFalse(r["truncated"])
        self.assertTrue(r["complete"])

    # ── 来源状态:结构化,且不得把"没查成"报成"没学过" ────────────────
    def test_source_status_is_structured_per_source(self) -> None:
        st = self.call(query="任意查询")["sourceStatus"]
        self.assertEqual(set(st), {"index", "kg", "anki"})
        self.assertTrue(all(isinstance(v, dict) and "state" in v for v in st.values()))

    def test_missing_source_is_absent_not_ok(self) -> None:
        """源目录不存在 → absent,且 complete 必须为假。

        报 complete=true 会让上游把"根本没查"读成"用户确实没学过"。
        """
        import shutil
        shutil.rmtree(self.root / "knowledge_graph")
        r = self.call(query="任意查询")
        self.assertEqual(r["sourceStatus"]["kg"]["state"], "absent")
        self.assertFalse(r["complete"])

    def test_broken_source_reports_error(self) -> None:
        import shutil
        shutil.rmtree(self.root / "index")
        (self.root / "index").write_text("这不是目录", encoding="utf-8")
        r = self.call(query="任意查询")
        self.assertEqual(r["sourceStatus"]["index"]["state"], "error")
        self.assertFalse(r["complete"])

    def test_corrupt_json_is_counted_not_silently_skipped(self) -> None:
        (self.root / "knowledge_graph" / "broken.json").write_text("{不是 JSON",
                                                                   encoding="utf-8")
        st = self.call(query="任意查询")["sourceStatus"]["kg"]
        self.assertEqual(st.get("unreadable"), 1, "坏 JSON 必须计数上报")

    # ── 排序与边界 ─────────────────────────────────────────────────────
    def test_cross_source_order_is_deterministic(self) -> None:
        self._kg("g.json", [{"name": "复合命中", "progress": "mastered"}])
        self._anki("n.json", [{"front": "复合命中的卡片"}])
        self._index("数学.md", "- [[复合命中]] `复合命中` — 索引条目\n")
        first = self.call(query="复合命中")["results"]
        for _ in range(3):
            self.assertEqual(self.call(query="复合命中")["results"], first)

    def test_no_raw_vault_scan(self) -> None:
        """合同要求不扫 raw vault:即便 vault 里有命中也不得返回。"""
        (self.root / "资源").mkdir()
        (self.root / "资源" / "笔记.md").write_text("独有词条出现在 vault", encoding="utf-8")
        self.assertEqual(self.call(query="独有词条")["count"], 0)


if __name__ == "__main__":
    unittest.main()
