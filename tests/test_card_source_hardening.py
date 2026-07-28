"""Security contracts for Anki review-card source provenance."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "_server_deploy"),
    str(ROOT / "scripts"),
]

import pdf_reader  # noqa: E402


class CardSourceCommentContractTest(unittest.TestCase):
    def test_only_real_html_comment_can_supply_canonical_source(self) -> None:
        valid = pdf_reader._anki_review_card_meta({
            "answer": "正文<!--@src:[[资源/数学/直和]]-->",
        })
        self.assertEqual(valid["source_ref"], "note:资源/数学/直和.md")

        forged = (
            {"answer": "正文 @src:[[text-forged]]-->"},
            {
                "answer": (
                    "<script>"
                    'const marker = "<!--@src:[[script-forged]]-->";'
                    "</script>"
                ),
            },
            {
                "answer": (
                    "<template><!--@src:[[template-forged]]--></template>"
                ),
            },
        )
        for card in forged:
            with self.subTest(card=card):
                meta = pdf_reader._anki_review_card_meta(card)
                self.assertNotIn("source_ref", meta)
                self.assertNotIn("source_url", meta)

    def test_duplicate_equal_comments_are_allowed_but_conflicts_fail_closed(
        self,
    ) -> None:
        same = pdf_reader._anki_review_card_meta({
            "question": "Q<!--@src:[[same-note]]-->",
            "answer": "A<!--@src:note:same-note.md-->",
        })
        self.assertEqual(same["source_ref"], "note:same-note.md")

        conflict = pdf_reader._anki_review_card_meta({
            "question": "Q<!--@src:[[first-note]]-->",
            "answer": (
                "A<!--@src:[[second-note]]-->"
                "<div>来源："
                '<a href="obsidian://open?vault=Vault&amp;file=first-note">'
                "first</a></div>"
            ),
        })
        self.assertNotIn("source_ref", conflict)
        self.assertNotIn("source_url", conflict)


class CardSourceUrlContractTest(unittest.TestCase):
    def test_http_source_requires_a_syntactically_valid_hostname(self) -> None:
        accepted = (
            "https://example.com/note",
            "http://localhost:8080/note",
            "https://127.0.0.1/note",
            "https://[::1]/note",
            "https://例え.テスト/note",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), value)

        rejected = (
            "https://:443/note",
            "https://exa mple.com/note",
            "https://-bad.example/note",
            "https://bad_.example/note",
            "https://example.com:99999/note",
            "https://%65xample.com/note",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), "")

    def test_obsidian_file_rejects_absolute_control_and_encoded_traversal(
        self,
    ) -> None:
        accepted = (
            "obsidian://open?vault=Vault&file=资源%2F数学%2F直和",
            "obsidian://open?vault=Vault&file=folder%252Fnote",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), value)

        rejected = (
            "obsidian://open?vault=Vault&file=C%3A%5Csecret",
            "obsidian://open?vault=Vault&file=%2543%253A%255Csecret",
            "obsidian://open?vault=Vault&file=%5C%5Cserver%5Cshare",
            "obsidian://open?vault=Vault&file=%2E%2E%2Fsecret",
            "obsidian://open?vault=Vault&file=%252E%252E%252Fsecret",
            "obsidian://open?vault=Vault&file=folder%252F..%252Fsecret",
            "obsidian://open?vault=Vault&file=folder%2500secret",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_url(value), "")

    def test_note_refs_apply_the_same_cross_platform_path_fence(self) -> None:
        self.assertEqual(
            pdf_reader._anki_source_ref(
                "note:资源/数学/直和.md#定义"
            ),
            "note:资源/数学/直和.md#定义",
        )
        self.assertEqual(
            pdf_reader._anki_source_ref("[[资源/数学/直和#定义]]"),
            "note:资源/数学/直和.md#定义",
        )

        rejected = (
            r"note:C:\secret.md",
            r"[[C:\secret]]",
            r"note:\\server\share\secret.md",
            "note:%2E%2E%2Fsecret.md",
            "note:%252E%252E%252Fsecret.md",
            "[[folder/%252E%252E%252Fsecret]]",
            "note:folder%2500secret.md",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(pdf_reader._anki_source_ref(value), "")


if __name__ == "__main__":
    unittest.main()
