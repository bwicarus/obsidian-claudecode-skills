from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
import sys
import types
import unittest
from unittest import mock

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl_stub
import pdf_reader  # noqa: E402


class AnkiDraftRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test")
        app.register_blueprint(pdf_reader.bp)
        self.client = app.test_client()
        self.payload = {
            "draftId": "draft-" + "a" * 32,
            "file": "books/book.epub",
            "target": {"kind": "epub", "section": 3},
            "sourceText": "unique source paragraph",
            "cards": [{"type": "basic", "front": "Q", "back": "A"}],
        }

    def test_valid_source_registers_private_draft_but_never_writes_anki(self) -> None:
        with (
            mock.patch.object(pdf_reader, "_safe_vault_path", return_value=Path("book.epub")),
            mock.patch.object(pdf_reader, "_epub_section_paragraphs", return_value=["unique source paragraph"]),
            mock.patch.object(pdf_reader, "_entity_reg_cards", return_value="card_abcdef") as register,
        ):
            response = self.client.post("/pdf/api/anki-draft", json=self.payload)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "draft_registered")
        self.assertFalse(body["anki_written"])
        self.assertEqual(body["gid"], "card_abcdef")
        self.assertEqual(register.call_count, 1)
        self.assertRegex(register.call_args.kwargs["entity_id"], r"^card_[a-f0-9]{12}$")
        self.assertEqual(register.call_args.args[1]["source_ref"], "book:books/book.epub#section=3")

    def test_ambiguous_source_fails_before_entity_registration(self) -> None:
        with (
            mock.patch.object(pdf_reader, "_safe_vault_path", return_value=Path("book.epub")),
            mock.patch.object(pdf_reader, "_epub_section_paragraphs", return_value=["unique source paragraph unique source paragraph"]),
            mock.patch.object(pdf_reader, "_entity_reg_cards") as register,
        ):
            response = self.client.post("/pdf/api/anki-draft", json=self.payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "source_ambiguous")
        register.assert_not_called()

    def test_unresolved_book_and_extra_fields_fail_closed(self) -> None:
        bad = dict(self.payload, unexpected=True)
        with mock.patch.object(pdf_reader, "_entity_reg_cards") as register:
            response = self.client.post("/pdf/api/anki-draft", json=bad)
        self.assertEqual(response.status_code, 400)
        register.assert_not_called()

    def test_whitespace_folding_does_not_make_two_matches_unique(self) -> None:
        self.assertEqual(
            pdf_reader._exact_source_text_count("a\n b -- a  b", "a b"),
            2,
        )

    def test_same_draft_replay_preserves_existing_review_state(self) -> None:
        entity_id = "card_abcdef123456"
        existing = {
            "kind": "cards",
            "url": "",
            "ts": 1,
            "local": "",
            "data": [{
                "type": "basic",
                "front": "Q",
                "back": "A",
                "cloze": None,
            }],
            "states": {"0": {"_st": "known"}},
            "source_ref": "book:books/book.epub#section=3",
        }
        store = types.SimpleNamespace(lock=lambda *_args: nullcontext())
        with (
            mock.patch.object(pdf_reader, "_reader_storage_identity_current", return_value="test"),
            mock.patch.object(pdf_reader, "_reader_sidecar_store", return_value=store),
            mock.patch.object(pdf_reader, "_asset_load", return_value={entity_id: existing}),
            mock.patch.object(pdf_reader, "_asset_save") as save,
        ):
            replayed = pdf_reader._entity_reg_cards(
                [{"type": "basic", "front": "Q", "back": "A"}],
                {"source_ref": "book:books/book.epub#section=3"},
                entity_id=entity_id,
            )
        self.assertEqual(replayed, entity_id)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
