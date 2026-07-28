"""卡片草稿确认入库后必须保留来源、全局编号和跨设备改进入口。"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "_server_deploy"),
    str(ROOT / "scripts"),
]

import pdf_reader  # noqa: E402
import attention_profile  # noqa: E402


class CardProvenanceContractTest(unittest.TestCase):
    def test_source_ref_normalizes_legacy_entity_metadata(self) -> None:
        self.assertEqual(
            pdf_reader._anki_source_ref("资源/books/math.pdf#p12"),
            "book:资源/books/math.pdf#p12",
        )
        self.assertEqual(
            pdf_reader._anki_source_ref("https://example.com/a"),
            "web:https://example.com/a",
        )
        self.assertEqual(
            pdf_reader._anki_source_ref("[[资源/books/000-直和]]"),
            "note:资源/books/000-直和.md",
        )
        self.assertEqual(
            pdf_reader._anki_source_ref(
                "[[资源/books/000-直和#定义|直和]]"
            ),
            "note:资源/books/000-直和.md#定义",
        )

    def test_review_queue_extracts_explicit_legacy_obsidian_source(self) -> None:
        meta = pdf_reader._anki_review_card_meta({
            "answer": (
                "答案<hr><div>来源："
                '<a href="obsidian://open?vault=Obsidian%20Vault'
                '&amp;file=%E8%B5%84%E6%BA%90%2F000-%E7%9B%B4%E5%92%8C">'
                "[[000-直和]]</a><br>原因：来自原笔记<br>"
                "Local ID：202605010001<br>"
                '<a href="https://bw.example/qa/?card=legacy-1">'
                "问 AI / 改进这张卡</a></div>"
            ),
        })
        self.assertEqual(
            meta["source_url"],
            "obsidian://open?vault=Obsidian%20Vault"
            "&file=%E8%B5%84%E6%BA%90%2F000-%E7%9B%B4%E5%92%8C",
        )
        self.assertEqual(
            meta["source_ref"],
            "note:资源/000-直和.md",
        )

    def test_review_queue_extracts_obsidian_footer_from_anki_field_shape(
        self,
    ) -> None:
        """AnkiConnect keeps the rendered footer in fields[*].value."""
        meta = pdf_reader._anki_review_card_meta({
            "cardId": 101,
            "note": 201,
            "question": "什么是直和？",
            "answer": "答案正文",
            "fields": {
                "Back": {
                    "value": (
                        "答案正文<hr><div "
                        'style="font-size:0.85em;color:#666;">'
                        "来源："
                        '<a href="obsidian://open?vault=Obsidian%20Vault'
                        "&amp;file=%E8%B5%84%E6%BA%90%2F000-"
                        '%E7%9B%B4%E5%92%8C">'
                        "[[000-直和]]</a><br>"
                        "原因：由原笔记的定义段落生成<br>"
                        "Local ID：202607270101<br>"
                        '<a href="https://bw.example/qa/?card=legacy-101">'
                        "问 AI / 改进这张卡</a></div>"
                    ),
                },
            },
        })
        self.assertEqual(
            meta["source_url"],
            "obsidian://open?vault=Obsidian%20Vault"
            "&file=%E8%B5%84%E6%BA%90%2F000-%E7%9B%B4%E5%92%8C",
        )
        self.assertEqual(meta["source_ref"], "note:资源/000-直和.md")

    def test_plain_or_script_embedded_links_cannot_forge_card_source(
        self,
    ) -> None:
        forged_cards = (
            {
                "answer": (
                    '<a href="https://example.com/ordinary">普通链接</a>'
                    '<a href="obsidian://open?vault=Vault&amp;file=forged">'
                    "没有来源标签的 Obsidian 链接</a>"
                ),
            },
            {
                "answer": (
                    "<script>来源："
                    '<a href="obsidian://open?vault=Vault&amp;file=script">'
                    "伪造来源</a></script>"
                    '<a href="https://example.com/after-script">普通链接</a>'
                ),
            },
            {
                "answer": (
                    "<div>原因："
                    '<a href="obsidian://open?vault=Vault&amp;file=reason">'
                    "原因链接不是来源</a></div>"
                ),
            },
        )
        for card in forged_cards:
            with self.subTest(card=card):
                meta = pdf_reader._anki_review_card_meta(card)
                self.assertNotIn("source_ref", meta)
                self.assertNotIn("source_url", meta)

    def test_source_url_allowlist_rejects_unsafe_or_ambiguous_links(self) -> None:
        accepted = (
            "https://example.com/note",
            "obsidian://open?vault=Vault&file=folder%2Fnote",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), value)

        rejected = (
            "javascript:alert(1)",
            "data:text/html,boom",
            "file:///tmp/note.md",
            "obsidian://search?vault=Vault&query=note",
            "obsidian://open?vault=Vault",
            "obsidian://open?vault=Vault&file=../secret",
            "https://user:secret@example.com/note",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), "")

    def test_canonical_marker_wins_over_mismatched_visible_source(self) -> None:
        meta = pdf_reader._anki_review_card_meta({
            "answer": (
                "A<!--@src:[[canonical-note]]-->"
                "<div>来源："
                '<a href="obsidian://open?vault=Vault&amp;file=other-note">'
                "other</a></div>"
            ),
        })
        self.assertEqual(meta["source_ref"], "note:canonical-note.md")
        self.assertIn("file=canonical-note", meta["source_url"])
        self.assertNotIn("other-note", meta["source_url"])

    def test_footer_contains_cross_device_action_and_machine_markers(self) -> None:
        old = os.environ.get("QA_PUBLIC_URL")
        os.environ["QA_PUBLIC_URL"] = "https://bw.example/qa"
        try:
            footer = pdf_reader._anki_provenance_footer(
                "card_a1b2c3",
                2,
                "book:资源/books/math.pdf#p12",
            )
        finally:
            if old is None:
                os.environ.pop("QA_PUBLIC_URL", None)
            else:
                os.environ["QA_PUBLIC_URL"] = old
        self.assertIn("问 AI / 改进这张卡", footer)
        self.assertIn("card=card_a1b2c3&amp;index=2", footer)
        self.assertIn(
            "<!--@src:book:资源/books/math.pdf#p12-->",
            footer,
        )
        self.assertIn("<!--@entity:card_a1b2c3:2-->", footer)

    def test_attention_graph_accepts_canonical_source_marker(self) -> None:
        ref = "book:资源/books/math.pdf#p12"
        self.assertEqual(attention_profile.obsidian_to_ref(ref), ref)

    def test_review_queue_extracts_new_entity_and_old_page_action(self) -> None:
        old = os.environ.get("QA_PUBLIC_URL")
        os.environ["QA_PUBLIC_URL"] = "https://bw.example/qa"
        try:
            meta = pdf_reader._anki_review_card_meta({
                "question": "Q",
                "answer": (
                    "A<!--@src:book:资源/books/math.pdf#p12-->"
                    "<!--@entity:card_a1b2c3:2-->"
                ),
            })
        finally:
            if old is None:
                os.environ.pop("QA_PUBLIC_URL", None)
            else:
                os.environ["QA_PUBLIC_URL"] = old
        self.assertEqual(meta["entity_id"], "card_a1b2c3")
        self.assertEqual(meta["entity_index"], 2)
        self.assertEqual(
            meta["source_ref"],
            "book:资源/books/math.pdf#p12",
        )
        self.assertEqual(
            meta["improve_url"],
            "https://bw.example/qa/?card=card_a1b2c3&index=2",
        )

    def test_review_queue_keeps_legacy_improve_link(self) -> None:
        old = os.environ.pop("QA_PUBLIC_URL", None)
        try:
            meta = pdf_reader._anki_review_card_meta({
                "answer": (
                    '<a href="https://bw.example/qa/?card=legacy-1">'
                    "问 AI / 改进这张卡</a>"
                ),
            })
        finally:
            if old is not None:
                os.environ["QA_PUBLIC_URL"] = old
        self.assertEqual(
            meta["improve_url"],
            "https://bw.example/qa/?card=legacy-1",
        )

    def test_frontend_sends_entity_identity_not_client_source(self) -> None:
        source = (
            ROOT
            / "_server_deploy"
            / "static"
            / "pdf"
            / "rc-flashcard.js"
        ).read_text("utf-8")
        self.assertIn("payload.entity_id = st.gid", source)
        self.assertIn("payload.card_index = i", source)
        self.assertNotIn("payload.source_ref", source)

    def test_entity_response_exposes_normalized_source_contract(self) -> None:
        source = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")
        self.assertIn('out["source_ref"] = _anki_source_ref(', source)

    def test_review_card_uses_the_shared_prepare_preview_commit_actions(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-review.js"
        ).read_text("utf-8")
        self.assertIn("更新到笔记", source)
        self.assertIn("根据此改进 Anki", source)
        self.assertIn("全部更新", source)
        self.assertIn("/api/assistant/card-improvement-draft", source)
        self.assertIn("/api/assistant/card-improvement-commit", source)
        self.assertNotIn("RC.review.improve()", source)


if __name__ == "__main__":
    unittest.main()
