"""`recall.notes` 直接命令的合同回归(Codex 2026-07-29 12:20 定合同,12:31 复核)。

**为什么必须是持久测试**:此前我只用一次性脚本"验证通过"就交接,结果连错三次
(KG 路径写成 page-cache 目录、in_progress 整类被排除、备份图被当现状),每次都在
自造 fixture 上显示全绿。临时脚本不进回归,等于没验证过。
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

    def _anki(self, name: str, cards: list) -> None:
        (self.root / "anki" / "records" / name).write_text(
            json.dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8")

    def call(self, **params):
        return self.handler({}, params, None)

    # ── 入参合同 ────────────────────────────────────────────────────────
    def test_query_is_required_and_not_inherited(self) -> None:
        with self.assertRaises(ValueError):
            self.call()
        with self.assertRaises(ValueError):
            self.call(query="   ")

    def test_query_max_80_chars(self) -> None:
        self.call(query="x" * 80)                      # 边界值可用
        with self.assertRaises(ValueError):
            self.call(query="x" * 81)

    def test_limit_capped_at_8(self) -> None:
        self._kg("g.json", [{"name": f"节点{i}", "progress": "mastered"}
                            for i in range(20)])
        r = self.call(query="节点", limit=99)
        self.assertEqual(r["count"], 8)
        self.assertEqual(r["total"], 20, "total 是命中总数,不是返回数")
        self.assertTrue(r["truncated"])

    # ── KG 学习证据(四条并集)──────────────────────────────────────────
    def test_progress_mastered_and_in_progress_are_learned(self) -> None:
        self._kg("g.json", [
            {"name": "甲", "progress": "mastered"},
            {"name": "乙", "progress": "in_progress"},
        ])
        got = {x["title"]: x["evidence"] for x in self.call(query="")["results"]} \
            if False else {x["title"]: x["evidence"] for x in self.call(query="甲")["results"]}
        self.assertEqual(got.get("甲"), "mastered")
        self.assertEqual(self.call(query="乙")["results"][0]["evidence"], "started")

    def test_unseen_with_containing_notes_still_counts(self) -> None:
        """Pi 上真实存在 progress=unseen 但记过笔记的节点,不能漏。"""
        self._kg("g.json", [{"name": "丙", "progress": "unseen",
                             "containing_notes": ["a.md", "b.md"]}])
        r = self.call(query="丙")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["note_count"], 2)

    def test_unseen_with_mastery_still_counts(self) -> None:
        self._kg("g.json", [{"name": "丁", "progress": "unseen", "mastery": 0.4}])
        self.assertEqual(self.call(query="丁")["count"], 1)

    def test_truly_unseen_is_excluded(self) -> None:
        self._kg("g.json", [{"name": "戊", "progress": "unseen",
                             "containing_notes": [], "mastery": 0}])
        self.assertEqual(self.call(query="戊")["count"], 0)

    def test_mastery_true_is_not_treated_as_one(self) -> None:
        self._kg("g.json", [{"name": "己", "progress": "unseen", "mastery": True}])
        self.assertEqual(self.call(query="己")["count"], 0, "bool 不是掌握度")

    # ── 备份图不得混入 ─────────────────────────────────────────────────
    def test_backup_graphs_are_skipped(self) -> None:
        node = [{"name": "庚", "progress": "mastered"}]
        self._kg("book.json", node)
        for bad in ("book.bak.json", "book.pre.json", "book.scan.json",
                    "book.tmp.json", "book.old.json", "_draft.json"):
            self._kg(bad, node)
        r = self.call(query="庚")
        self.assertEqual(r["total"], 1, f"备份被当成现状召回:{r['results']}")

    # ── Anki:cloze 正文在 text ────────────────────────────────────────
    def test_cloze_card_body_is_searched(self) -> None:
        """只搜 front/back 会让本地明明有的 cloze 卡返回空(实测漏 25 张)。"""
        self._anki("n.json", [{"text": "零向量记作 {{c1::0}}"}])
        r = self.call(query="零向量记作")
        self.assertEqual(r["count"], 1, "cloze 卡的 text 字段必须参与匹配")
        self.assertEqual(r["results"][0]["type"], "cloze")

    def test_basic_card_still_matched(self) -> None:
        self._anki("n.json", [{"front": "辛是什么", "back": "答案"}])
        self.assertEqual(self.call(query="辛")["results"][0]["type"], "basic")

    # ── 返回合同 ───────────────────────────────────────────────────────
    def test_empty_hit_is_success_not_error(self) -> None:
        r = self.call(query="根本不存在的词")
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["total"], 0)
        self.assertFalse(r["truncated"])
        self.assertTrue(r["complete"])
        self.assertEqual(r["sourceStatus"], "ok")

    def test_envelope_fields_exact(self) -> None:
        self.assertEqual(set(self.call(query="任意")),
                         {"query", "results", "count", "total",
                          "truncated", "complete", "sourceStatus"})

    def test_single_source_failure_reports_partial(self) -> None:
        (self.root / "index").rmdir()
        (self.root / "index").write_text("不是目录", encoding="utf-8")   # 让 glob 失败
        r = self.call(query="任意")
        self.assertFalse(r["complete"])
        self.assertEqual(r["sourceStatus"], "partial")

    def test_cross_source_order_is_deterministic(self) -> None:
        """同 query 必须每次返回同一批,不受文件枚举顺序影响。"""
        self._kg("g.json", [{"name": "壬掌握", "progress": "mastered"},
                            {"name": "壬在学", "progress": "in_progress"}])
        self._anki("n.json", [{"front": "壬卡片"}])
        (self.root / "index" / "i.md").write_text("- 壬索引条目\n", encoding="utf-8")
        first = self.call(query="壬")["results"]
        for _ in range(3):
            self.assertEqual(self.call(query="壬")["results"], first)
        self.assertEqual(first[0]["evidence"], "mastered", "证据最强的排最前")

    def test_no_raw_vault_scan(self) -> None:
        """合同要求不扫 raw vault:即便 vault 里有命中也不得返回。"""
        (self.root / "资源").mkdir()
        (self.root / "资源" / "笔记.md").write_text("癸出现在 vault", encoding="utf-8")
        self.assertEqual(self.call(query="癸")["count"], 0)


if __name__ == "__main__":
    unittest.main()
