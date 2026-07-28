#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))

from card_candidate_service import CardCandidateIndex, CardCandidateService


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")


class CardCandidateServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.records = self.root / "records"
        self.graphs = self.root / "knowledge_graph"
        self.vault = self.root / "vault"
        self.records.mkdir()
        self.graphs.mkdir()
        self.vault.mkdir()
        _write(
            self.records / "page.json",
            {
                "source_note": "notes/page.md",
                "cards": [
                    {
                        "anki_note_id": 202,
                        "front": "页面知识点",
                        "back": "page answer",
                    }
                ],
            },
        )
        _write(
            self.records / "focus.json",
            {
                "source_note": "notes/focus.md",
                "cards": [
                    {
                        "anki_note_id": 303,
                        "front": "What does alpha mean?",
                        "back": "focus answer",
                    }
                ],
            },
        )
        _write(
            self.records / "material.json",
            {
                "source_note": "notes/material.md",
                "cards": [
                    {
                        "anki_note_id": 404,
                        "front": "关联材料",
                        "back": "material answer",
                    }
                ],
            },
        )
        _write(
            self.graphs / "Book.json",
            {
                "book": "Book",
                "pdf": str(self.vault / "books" / "book.pdf"),
                "nodes": [
                    {
                        "id": "book.chapter",
                        "name": "Chapter",
                        "pages": [5, 6],
                        "note_ref": "page.md",
                        "containing_notes": ["notes/page.md"],
                    }
                ],
                "edges": [],
            },
        )
        self.index = CardCandidateIndex(self.records, self.graphs, self.vault)
        self.service = CardCandidateService(self.index)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _resolve(note_ids):
        mapping = {202: [20, 21], 303: [30], 404: [40]}
        return {note_id: mapping.get(note_id, []) for note_id in note_ids}

    def test_four_channels_merge_dedupe_rank_and_due_fill(self):
        direct_ref = "book:books/book.pdf#p5"
        page_ref = "kg:Book#book.chapter"

        def source_cards(refs):
            values = {
                direct_ref: [10],
                page_ref: [20],  # duplicate of the record/KG route
            }
            return {ref: values.get(ref, []) for ref in refs}

        def graph(ref):
            if ref == "book:other.pdf#p7":
                return {
                    "layers": [
                        [{"ref": ref}],
                        [{"ref": "note:notes/material.md"}],
                    ]
                }
            return {"layers": [[{"ref": ref}]]}

        plan = self.service.build(
            {
                "file": "books/book.pdf",
                "page": 5,
                "selection": "alpha",
            },
            [90, 20, 91],
            resolve_note_cards=self._resolve,
            find_source_cards=source_cards,
            search_term_cards=lambda terms: {"alpha": [30]},
            focus_terms=lambda text: {"top": [{"term": "alpha"}]},
            relate_material=lambda term: {
                "materials": [{"ref": "book:other.pdf#p7"}]
            },
            material_graph=graph,
            limit=7,
        )

        self.assertEqual(
            plan["selected_card_ids"],
            [10, 20, 21, 30, 40, 90, 91],
        )
        self.assertEqual(plan["source_ref"], direct_ref)
        self.assertEqual(plan["related_total"], 5)
        self.assertEqual(
            {
                evidence["kind"]
                for evidence in plan["metadata"]["20"]["evidence"]
            },
            {"page_kg", "due"},
        )
        self.assertEqual(
            {
                evidence["kind"]
                for evidence in plan["metadata"]["40"]["evidence"]
            },
            {"material_graph"},
        )
        self.assertEqual(plan["metadata"]["20"]["due"], True)
        self.assertEqual(plan["metadata"]["21"]["related"], True)

    def test_fail_soft_keeps_due_order_and_exclusions(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("optional discovery unavailable")

        _write(self.records / "broken.json", "{not-json")
        plan = self.service.build(
            {"visible_text": "alpha beta"},
            [7, 8, 9],
            resolve_note_cards=broken,
            find_source_cards=broken,
            search_term_cards=broken,
            focus_terms=broken,
            relate_material=broken,
            material_graph=broken,
            exclude_card_ids=[8],
            limit=60,
        )
        self.assertEqual(plan["selected_card_ids"], [7, 9])
        self.assertEqual(plan["related_total"], 0)
        self.assertEqual(plan["metadata"]["7"]["reason_labels"], ["到期"])

    def test_real_record_and_kg_shapes_include_page_boundaries_and_cloze_cards(self):
        self.assertEqual(
            self.index.page_kg_refs("books/book.pdf", 5),
            ["kg:Book#book.chapter"],
        )
        self.assertEqual(
            self.index.page_kg_refs("books/book.pdf", 6),
            ["kg:Book#book.chapter"],
        )
        self.assertEqual(self.index.page_kg_refs("books/book.pdf", 7), [])
        self.assertEqual(
            self.index.note_ids_for_ref("kg:Book#book.chapter"),
            [202],
        )
        self.assertEqual(self._resolve([202])[202], [20, 21])

    def test_index_refreshes_after_record_file_changes(self):
        self.assertEqual(
            self.index.focus_matches(["newterm"]).get("newterm"),
            [],
        )
        _write(
            self.records / "focus.json",
            {
                "source_note": "notes/focus.md",
                "cards": [
                    {
                        "anki_note_id": 505,
                        "front": "newterm with a longer changed payload",
                        "back": "answer",
                    }
                ],
            },
        )
        self.assertEqual(
            self.index.focus_matches(["newterm"])["newterm"],
            [505],
        )

    def test_anki_material_ref_is_a_card_id(self):
        plan = self.service.build(
            {"visible_text": "alpha"},
            [],
            resolve_note_cards=self._resolve,
            focus_terms=lambda text: ["no-match-term"],
            relate_material=lambda term: {"materials": [{"ref": "anki:77"}]},
            material_graph=lambda ref: {"layers": [[{"ref": ref}]]},
            limit=3,
        )
        self.assertEqual(plan["selected_card_ids"], [77])
        self.assertEqual(
            plan["metadata"]["77"]["evidence"][0]["kind"],
            "material_graph",
        )


if __name__ == "__main__":
    unittest.main()
