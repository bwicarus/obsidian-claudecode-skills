from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "kg"))

import concept_node_service as concept_service_module  # noqa: E402
from concept_node_service import (  # noqa: E402
    ConceptNodeError,
    ConceptNodeService,
    migrate_page_brief_document,
    promote_page_brief,
)


def _service(root: Path) -> ConceptNodeService:
    return ConceptNodeService(
        graph_path=root / "state" / "emergent-graph.json",
        journal_path=root / "state" / "kg-node-mutations.jsonl",
        aliases_path=root / "state" / "concept-aliases.json",
        confirmations_path=root / "state" / "confirmations.json",
        kg_dir=root / "knowledge_graph",
        concept_root=root / "vault" / "资源" / "概念",
        clock=lambda: 1_700_000_000,
    )


def _brief() -> dict:
    return {
        "brief": "Defines a linear map.",
        "tags": ["linear map"],
        "concepts": [{
            "name": "linear map",
            "evidence": "A linear map preserves vector addition.",
        }],
        "page_type": "knowledge",
        "subtype": "text",
    }


class ConceptNodeServiceKgETest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = _service(self.root)
        self.old_rel = "资源/books/old-name.pdf"
        self.new_rel = "资源/books/new-name.pdf"
        self.page_text = "A linear map preserves vector addition."

    def tearDown(self):
        self.temp.cleanup()

    def test_rename_migrates_only_path_projection_and_future_replay_is_stable(self):
        created = promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        before = self.service.load_graph()["nodes"]["linear map"]
        before_evidence = before["provenance"][0]
        immutable = {
            "nodeId": before["id"],
            "signal": before["signal"],
            "evidenceId": before_evidence["id"],
            "sourceId": before_evidence["sourceId"],
            "quoteSha256": before_evidence["quoteSha256"],
        }

        migrated = migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-old-to-new",
            service=self.service,
        )

        self.assertEqual(migrated["payload"], {
            "contract": "page-brief-document-rename/1",
            "oldDocumentRef": "book:" + self.old_rel,
            "newDocumentRef": "book:" + self.new_rel,
            "changedNodes": 1,
            "migratedEvidence": 1,
            "migratedBookRefs": 1,
        })
        after = self.service.load_graph()["nodes"]["linear map"]
        evidence = after["provenance"][0]
        self.assertEqual(after["id"], immutable["nodeId"])
        self.assertEqual(after["signal"], immutable["signal"])
        self.assertEqual(evidence["id"], immutable["evidenceId"])
        self.assertEqual(evidence["sourceId"], immutable["sourceId"])
        self.assertEqual(evidence["quoteSha256"], immutable["quoteSha256"])
        self.assertEqual(evidence["documentRef"], "book:" + self.new_rel)
        self.assertEqual(after["books"], [self.new_rel])

        renamed_replay = promote_page_brief(
            file_rel=self.new_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        self.assertNotEqual(renamed_replay["mutationId"], created["mutationId"])
        self.assertEqual(
            [item["reason"] for item in renamed_replay["deduplicated"]],
            ["evidence-replay"],
        )
        final = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(final["signal"], 1)
        self.assertEqual(len(final["provenance"]), 1)

    def test_migration_receipt_replays_without_rewriting_graph(self):
        promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        first = migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-replay",
            service=self.service,
        )
        graph_after_first = self.service.graph_path.read_bytes()

        replay = migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-replay",
            service=self.service,
        )

        self.assertIs(replay["replay"], True)
        self.assertEqual(replay["txId"], first["txId"])
        self.assertEqual(self.service.graph_path.read_bytes(), graph_after_first)

    def test_evicted_page_brief_projection_preserves_old_and_adds_new_book(self):
        promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(concept_service_module, "_MAX_PROVENANCE", 1):
            self.service.upsert_candidates(
                [{
                    "surface": "linear map",
                    "sourceKind": "note",
                    "sourceId": "note:linear-map",
                    "documentRef": "vault-note:linear-map.md",
                }],
                mutation_id="evict-page-brief-provenance",
                operation_contract="kg-op/test-provenance-evict/1",
                operation_payload={"nodeKey": "linear map"},
            )
        migrated = migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-after-prune",
            service=self.service,
        )

        self.assertEqual(migrated["payload"]["migratedEvidence"], 0)
        self.assertEqual(migrated["payload"]["migratedBookRefs"], 0)
        self.assertEqual(
            self.service.load_graph()["nodes"]["linear map"]["books"],
            sorted([self.old_rel, self.new_rel]),
        )

    def test_other_source_document_refs_and_books_are_not_rewritten(self):
        self.service.upsert_candidates(
            [{
                "surface": "linear map",
                "sourceKind": "note",
                "sourceId": "note:old-name",
                "documentRef": "book:" + self.old_rel,
                "book": self.old_rel,
            }],
            mutation_id="note-before-rename",
            source="note",
        )
        migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-note-only",
            service=self.service,
        )

        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["books"], [self.old_rel])
        self.assertEqual(
            node["provenance"][0]["documentRef"],
            "book:" + self.old_rel,
        )

    def test_mixed_provenance_keeps_old_book_and_adds_new_projection(self):
        promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        self.service.upsert_candidates(
            [{
                "surface": "linear map",
                "sourceKind": "autonote",
                "sourceId": "autonote:ABC-linear-map.md",
                "documentRef": (
                    "vault:资源/概念/linear-map/ABC-linear-map.md"
                ),
                "book": self.old_rel,
            }],
            mutation_id="autonote-alongside-page-brief",
            source="propose-concept-notes",
        )

        migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-mixed-provenance",
            service=self.service,
        )

        node = self.service.load_graph()["nodes"]["linear map"]
        refs = {
            evidence["type"]: evidence["documentRef"]
            for evidence in node["provenance"]
        }
        self.assertEqual(refs["page-brief"], "book:" + self.new_rel)
        self.assertEqual(
            refs["autonote"],
            "vault:资源/概念/linear-map/ABC-linear-map.md",
        )
        self.assertEqual(node["books"], sorted([self.old_rel, self.new_rel]))
        self.assertEqual(node["signal"], 2)

    def test_chained_rename_preserves_identity_and_signal(self):
        third_rel = "资源/books/final-name.pdf"
        promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        before = self.service.load_graph()["nodes"]["linear map"]

        migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-chain-a-b",
            service=self.service,
        )
        migrate_page_brief_document(
            old_file_rel=self.new_rel,
            new_file_rel=third_rel,
            mutation_id="rename-chain-b-c",
            service=self.service,
        )

        after = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["signal"], before["signal"])
        self.assertEqual(after["books"], [third_rel])
        self.assertEqual(
            after["provenance"][0]["documentRef"],
            "book:" + third_rel,
        )
        self.assertEqual(
            self.service.mutation_status("rename-chain-b-c")["status"],
            "applied",
        )
        self.assertEqual(
            self.service.mutation_status("never-written")["status"],
            "absent",
        )

    def test_mutation_status_resolves_graph_replace_before_commit_append(self):
        promote_page_brief(
            file_rel=self.old_rel,
            page=12,
            page_text=self.page_text,
            brief=_brief(),
            service=self.service,
        )
        real_append = concept_service_module._append_jsonl

        def fail_migration_commit(path, row):
            if (
                row.get("phase") == "commit"
                and row.get("mutationId") == "rename-crash-window"
            ):
                raise OSError("simulated commit append failure")
            return real_append(path, row)

        with patch.object(
            concept_service_module,
            "_append_jsonl",
            side_effect=fail_migration_commit,
        ):
            with self.assertRaises(OSError):
                migrate_page_brief_document(
                    old_file_rel=self.old_rel,
                    new_file_rel=self.new_rel,
                    mutation_id="rename-crash-window",
                    service=self.service,
                )

        status = self.service.mutation_status("rename-crash-window")
        self.assertEqual(status["status"], "applied")
        self.assertEqual(
            [row["phase"] for row in status["recovered"]],
            ["commit"],
        )
        replay = migrate_page_brief_document(
            old_file_rel=self.old_rel,
            new_file_rel=self.new_rel,
            mutation_id="rename-crash-window",
            service=self.service,
        )
        self.assertIs(replay["replay"], True)
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(node["books"], [self.new_rel])

    def test_invalid_or_same_paths_fail_closed(self):
        for old_rel, new_rel in (
            ("../escape.pdf", self.new_rel),
            (self.old_rel, "/absolute.pdf"),
            (self.old_rel, self.old_rel),
            ("bad\\path.pdf", self.new_rel),
        ):
            with self.assertRaises(ConceptNodeError) as caught:
                migrate_page_brief_document(
                    old_file_rel=old_rel,
                    new_file_rel=new_rel,
                    mutation_id="invalid-path",
                    service=self.service,
                )
            self.assertEqual(caught.exception.code, "BW_KG_NODE_DOCUMENT")
        self.assertFalse(self.service.graph_path.exists())


if __name__ == "__main__":
    unittest.main()
