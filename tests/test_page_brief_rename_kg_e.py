from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
sys.path.insert(0, str(ROOT / "scripts" / "kg"))

import pdf_reader  # noqa: E402
import concept_node_service  # noqa: E402
from concept_node_service import (  # noqa: E402
    ConceptNodeService,
    promote_page_brief,
)


class PageBriefRenameKgETest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.vault = self.root / "vault"
        self.old_rel = "books/old-name.pdf"
        self.new_rel = "books/new-name.pdf"
        self.third_rel = "books/final-name.pdf"
        self.old_pdf = self.vault / self.old_rel
        self.old_pdf.parent.mkdir(parents=True)
        self.old_pdf.write_bytes(b"%PDF-1.4\nKG-e rename fixture\n%%EOF\n")
        self.saved = (
            pdf_reader.CLAUDE_DIR,
            pdf_reader.OBSIDIAN_ROOT,
            pdf_reader._BRIEF_DIR,
            pdf_reader._BOOK_BRIEF_PATH,
        )
        pdf_reader.CLAUDE_DIR = self.project
        pdf_reader.OBSIDIAN_ROOT = self.vault
        pdf_reader._BRIEF_DIR = self.project / "state" / "pdf-page-brief"
        pdf_reader._BOOK_BRIEF_PATH = (
            self.project / "state" / "pdf-book-brief.json"
        )
        pdf_reader._brief_inflight.clear()
        self.app = Flask(__name__)

    def tearDown(self):
        (
            pdf_reader.CLAUDE_DIR,
            pdf_reader.OBSIDIAN_ROOT,
            pdf_reader._BRIEF_DIR,
            pdf_reader._BOOK_BRIEF_PATH,
        ) = self.saved
        pdf_reader._brief_inflight.clear()
        self.temp.cleanup()

    def _brief(self, pdf_path: Path | None = None) -> dict:
        identity_path = pdf_path or self.old_pdf
        stat_path = identity_path if identity_path.exists() else self.old_pdf
        return {
            "pdf": str(identity_path),
            "ver": 2,
            "book_mtime": int(stat_path.stat().st_mtime),
            "briefs": {
                "1": {
                    "brief": "synced",
                    "page_type": "knowledge",
                    "kg_status": "synced",
                },
                "2": {
                    "brief": "pending",
                    "page_type": "knowledge",
                    "kg_status": "pending",
                },
                "3": {
                    "brief": "",
                    "page_type": "skip",
                    "kg_status": "not_applicable",
                },
            },
            "_none_pages": [9],
        }

    def _write_state(self, *, enabled=False) -> Path:
        brief_path = pdf_reader._brief_path_abs(self.old_pdf)
        brief_path.write_text(
            json.dumps(self._brief(), ensure_ascii=False, indent=1),
            "utf-8",
        )
        pdf_reader._BOOK_BRIEF_PATH.parent.mkdir(parents=True, exist_ok=True)
        pdf_reader._BOOK_BRIEF_PATH.write_text(
            json.dumps(
                {self.old_rel: enabled, "books/other.pdf": True},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        return brief_path

    def _post(self, old_rel=None, new_name="new-name.pdf"):
        with self.app.test_request_context(
            "/pdf/api/rename-pdf",
            method="POST",
            json={
                "file": old_rel or self.old_rel,
                "new_name": new_name,
            },
        ):
            return self.app.make_response(pdf_reader.pdf_api_rename_pdf())

    @staticmethod
    def _kg_result():
        return {
            "contract": "concept-node-service/1",
            "mutationId": "patched",
            "txId": "kg-tx",
            "payload": {"migratedEvidence": 1},
        }

    def _concept_service(self) -> ConceptNodeService:
        return ConceptNodeService(
            graph_path=self.project / "state" / "emergent-graph.json",
            journal_path=self.project / "state" / "kg-node-mutations.jsonl",
            aliases_path=self.project / "state" / "concept-aliases.json",
            confirmations_path=self.project / "state" / "confirmations.json",
            kg_dir=self.project / "knowledge_graph",
            concept_root=self.vault / "资源" / "概念",
            clock=lambda: 1_700_000_000,
        )

    def test_success_preserves_all_brief_states_and_explicit_false(self):
        old_brief = self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel
        kg_calls = []

        def fake_kg(old_rel, new_rel, mutation_id):
            kg_calls.append((old_rel, new_rel, mutation_id))
            return self._kg_result()

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                side_effect=fake_kg,
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(self.old_pdf.exists())
        self.assertTrue(new_pdf.exists())
        self.assertFalse(old_brief.exists())
        new_brief = pdf_reader._brief_path_abs(new_pdf)
        migrated = json.loads(new_brief.read_text("utf-8"))
        expected = self._brief(new_pdf)
        self.assertEqual(migrated, expected)
        self.assertEqual(
            {
                page: value["kg_status"]
                for page, value in migrated["briefs"].items()
            },
            {
                "1": "synced",
                "2": "pending",
                "3": "not_applicable",
            },
        )
        self.assertEqual(migrated["_none_pages"], [9])
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertNotIn(self.old_rel, settings)
        self.assertIs(settings[self.new_rel], False)
        self.assertIs(settings["books/other.pdf"], True)
        self.assertEqual(len(kg_calls), 1)
        self.assertEqual(kg_calls[0][:2], (self.old_rel, self.new_rel))
        legacy.assert_called_once_with(
            self.old_rel,
            self.new_rel,
            self.old_pdf,
            new_pdf,
        )
        intents = list(pdf_reader._pdf_rename_dir().glob("*.json"))
        self.assertEqual(len(intents), 1)
        self.assertEqual(
            json.loads(intents[0].read_text("utf-8"))["phase"],
            "committed",
        )

    def test_route_uses_real_concept_service_migration_without_repromoting_ai(self):
        self._write_state(enabled=True)
        service = self._concept_service()
        page_text = "A linear map preserves vector addition."
        promote_page_brief(
            file_rel=self.old_rel,
            page=1,
            page_text=page_text,
            brief={
                "brief": "Defines a linear map.",
                "tags": ["linear map"],
                "concepts": [{
                    "name": "linear map",
                    "evidence": page_text,
                }],
                "page_type": "knowledge",
                "subtype": "text",
            },
            service=service,
        )

        with (
            patch.object(
                concept_node_service,
                "ConceptNodeService",
                return_value=service,
            ),
            # Runtime selection has its own isolated contract. This route test
            # injects the already-constructed service module explicitly so it
            # cannot accidentally depend on a machine-wide current symlink.
            patch(
                "kg_runtime.import_module",
                return_value=concept_node_service,
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200, response.get_json())
        node = service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(node["books"], [self.new_rel])
        self.assertEqual(
            node["provenance"][0]["documentRef"],
            "book:" + self.new_rel,
        )
        self.assertEqual(
            len(
                [
                    row
                    for row in service.journal_path.read_text("utf-8").splitlines()
                    if '"source":"page-brief-document-rename"' in row
                ]
            ),
            2,
        )

    def test_inflight_generation_returns_409_before_any_write(self):
        old_brief = self._write_state()
        pdf_reader._brief_inflight.add((pdf_reader._book_sha(self.old_pdf), 1))

        with (
            patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg,
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["code"],
            "BW_PDF_RENAME_BRIEF_INFLIGHT",
        )
        self.assertTrue(self.old_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertFalse((self.vault / self.new_rel).exists())
        self.assertEqual(list(pdf_reader._pdf_rename_dir().glob("*.json")), [])
        kg.assert_not_called()
        legacy.assert_not_called()

    def test_destination_sidecar_or_book_setting_conflict_is_zero_write(self):
        old_brief = self._write_state()
        new_pdf = self.vault / self.new_rel
        destination = pdf_reader._brief_path_abs(new_pdf)
        destination.write_text(
            json.dumps(self._brief(new_pdf), ensure_ascii=False),
            "utf-8",
        )

        with patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg:
            response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["code"],
            "BW_PDF_RENAME_BRIEF_DESTINATION",
        )
        self.assertTrue(self.old_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(list(pdf_reader._pdf_rename_dir().glob("*.json")), [])
        kg.assert_not_called()

    def test_destination_book_setting_conflict_is_zero_write(self):
        old_brief = self._write_state(enabled=False)
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        settings[self.new_rel] = True
        pdf_reader._BOOK_BRIEF_PATH.write_text(
            json.dumps(settings, ensure_ascii=False),
            "utf-8",
        )

        with patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg:
            response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["code"],
            "BW_PDF_RENAME_BOOK_SETTING_DESTINATION",
        )
        self.assertTrue(self.old_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertFalse((self.vault / self.new_rel).exists())
        self.assertEqual(list(pdf_reader._pdf_rename_dir().glob("*.json")), [])
        kg.assert_not_called()

    def test_corrupt_source_sidecar_fails_closed_before_pdf_rename(self):
        old_brief = pdf_reader._brief_path_abs(self.old_pdf)
        old_brief.write_text("{broken", "utf-8")

        with patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg:
            response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["code"],
            "BW_PDF_RENAME_CORRUPT",
        )
        self.assertTrue(self.old_pdf.exists())
        self.assertFalse((self.vault / self.new_rel).exists())
        self.assertEqual(old_brief.read_text("utf-8"), "{broken")
        kg.assert_not_called()

    def test_corrupt_book_setting_fails_closed_before_pdf_rename(self):
        old_brief = self._write_state()
        pdf_reader._BOOK_BRIEF_PATH.write_text("{broken", "utf-8")

        with patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg:
            response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["code"],
            "BW_PDF_RENAME_CORRUPT",
        )
        self.assertTrue(self.old_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertFalse((self.vault / self.new_rel).exists())
        self.assertEqual(
            pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"),
            "{broken",
        )
        kg.assert_not_called()

    def test_sidecar_write_failure_rolls_pdf_back_before_kg(self):
        old_brief = self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel
        real_write = pdf_reader._pdf_rename_write_bytes

        def fail_new_brief(path, payload):
            if path == pdf_reader._brief_path_abs(new_pdf):
                raise OSError("sidecar disk fault")
            return real_write(path, payload)

        with (
            patch.object(
                pdf_reader,
                "_pdf_rename_write_bytes",
                side_effect=fail_new_brief,
            ),
            patch.object(pdf_reader, "_migrate_page_brief_kg_document") as kg,
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "BW_PDF_RENAME_BRIEF")
        self.assertTrue(self.old_pdf.exists())
        self.assertFalse(new_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertFalse(pdf_reader._brief_path_abs(new_pdf).exists())
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertIs(settings[self.old_rel], False)
        intent = json.loads(
            next(pdf_reader._pdf_rename_dir().glob("*.json")).read_text("utf-8")
        )
        self.assertEqual(intent["phase"], "aborted")
        kg.assert_not_called()
        legacy.assert_not_called()

    def test_kg_failure_before_graph_apply_rolls_everything_back(self):
        old_brief = self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                side_effect=RuntimeError("kg unavailable"),
            ),
            patch.object(
                pdf_reader,
                "_page_brief_kg_migration_state",
                return_value="absent",
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "BW_PDF_RENAME_KG")
        self.assertTrue(self.old_pdf.exists())
        self.assertFalse(new_pdf.exists())
        self.assertTrue(old_brief.exists())
        self.assertFalse(pdf_reader._brief_path_abs(new_pdf).exists())
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertIs(settings[self.old_rel], False)
        self.assertNotIn(self.new_rel, settings)
        intent = json.loads(
            next(pdf_reader._pdf_rename_dir().glob("*.json")).read_text("utf-8")
        )
        self.assertEqual(intent["phase"], "aborted")
        self.assertIs(intent["rolledBack"], True)
        legacy.assert_not_called()

    def test_rollback_restores_missing_source_brief_byte_for_byte(self):
        old_brief = self._write_state(enabled=False)
        original_brief = old_brief.read_bytes()
        new_pdf = self.vault / self.new_rel

        def fail_after_source_disappears(*_args):
            old_brief.unlink()
            raise RuntimeError("source sidecar disappeared after staging")

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                side_effect=fail_after_source_disappears,
            ),
            patch.object(
                pdf_reader,
                "_page_brief_kg_migration_state",
                return_value="absent",
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "BW_PDF_RENAME_KG")
        self.assertTrue(self.old_pdf.exists())
        self.assertFalse(new_pdf.exists())
        self.assertEqual(old_brief.read_bytes(), original_brief)
        self.assertFalse(pdf_reader._brief_path_abs(new_pdf).exists())
        intent = json.loads(
            next(pdf_reader._pdf_rename_dir().glob("*.json")).read_text("utf-8")
        )
        self.assertEqual(intent["phase"], "aborted")
        self.assertIs(intent["rolledBack"], True)
        legacy.assert_not_called()

    def test_graph_replace_commit_append_window_completes_forward(self):
        old_brief = self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                side_effect=RuntimeError("commit append failed"),
            ),
            patch.object(
                pdf_reader,
                "_page_brief_kg_migration_state",
                return_value="applied",
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(self.old_pdf.exists())
        self.assertTrue(new_pdf.exists())
        self.assertFalse(old_brief.exists())
        self.assertTrue(pdf_reader._brief_path_abs(new_pdf).exists())
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertIs(settings[self.new_rel], False)
        intent = json.loads(
            next(pdf_reader._pdf_rename_dir().glob("*.json")).read_text("utf-8")
        )
        self.assertEqual(intent["phase"], "committed")
        self.assertIs(intent["kgResult"]["recovered"], True)

    def test_retry_recovers_failure_after_kg_before_config(self):
        old_brief = self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel
        original_apply = pdf_reader._pdf_rename_apply_book_setting
        apply_calls = {"n": 0}

        def fail_once(*args, **kwargs):
            apply_calls["n"] += 1
            if apply_calls["n"] == 1:
                raise OSError("config disk fault")
            return original_apply(*args, **kwargs)

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                return_value=self._kg_result(),
            ),
            patch.object(
                pdf_reader,
                "_pdf_rename_apply_book_setting",
                side_effect=fail_once,
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            first = self._post()
            self.assertEqual(first.status_code, 500)
            self.assertFalse(self.old_pdf.exists())
            self.assertTrue(new_pdf.exists())
            self.assertTrue(old_brief.exists())
            self.assertTrue(pdf_reader._brief_path_abs(new_pdf).exists())

            second = self._post()

        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertIs(second.get_json()["recovered"], True)
        self.assertFalse(old_brief.exists())
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertNotIn(self.old_rel, settings)
        self.assertIs(settings[self.new_rel], False)
        legacy.assert_called_once()

    def test_completed_retry_rejects_replaced_destination_pdf(self):
        self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                return_value=self._kg_result(),
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars"),
        ):
            first = self._post()
            self.assertEqual(first.status_code, 200, first.get_json())
            new_pdf.write_bytes(
                b"%PDF-1.4\nreplacement with a different identity\n%%EOF\n"
            )
            second = self._post()

        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.get_json()["code"],
            "BW_PDF_RENAME_PDF_CHANGED",
        )

    def test_completed_retry_allows_mutable_brief_and_setting(self):
        self._write_state(enabled=False)
        new_pdf = self.vault / self.new_rel

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                return_value=self._kg_result(),
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            first = self._post()
            self.assertEqual(first.status_code, 200, first.get_json())
            new_brief_path = pdf_reader._brief_path_abs(new_pdf)
            new_brief = json.loads(new_brief_path.read_text("utf-8"))
            new_brief["briefs"]["4"] = {
                "brief": "generated after rename",
                "page_type": "knowledge",
                "kg_status": "synced",
            }
            new_brief["briefs"]["2"]["kg_status"] = "synced"
            new_brief_path.write_text(
                json.dumps(new_brief, ensure_ascii=False, indent=1),
                "utf-8",
            )
            settings = json.loads(
                pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8")
            )
            settings[self.new_rel] = True
            pdf_reader._BOOK_BRIEF_PATH.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                "utf-8",
            )

            second = self._post()

        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertIs(second.get_json()["recovered"], True)
        self.assertEqual(legacy.call_count, 1)
        persisted = json.loads(
            pdf_reader._brief_path_abs(new_pdf).read_text("utf-8")
        )
        self.assertIn("4", persisted["briefs"])
        self.assertEqual(persisted["briefs"]["2"]["kg_status"], "synced")
        self.assertIs(
            json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))[
                self.new_rel
            ],
            True,
        )

    def test_committed_retry_never_reruns_legacy_sidecar_migration(self):
        self._write_state(enabled=False)

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                return_value=self._kg_result(),
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars") as legacy,
        ):
            first = self._post()
            self.assertEqual(first.status_code, 200, first.get_json())
            second = self._post()

        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(legacy.call_count, 1)
        intent = json.loads(
            next(pdf_reader._pdf_rename_dir().glob("*.json")).read_text("utf-8")
        )
        self.assertIs(intent["legacySidecarsMigrated"], True)

    def test_legacy_sidecar_target_conflict_preserves_both_files(self):
        source = self.project / "state" / "legacy-old.json"
        destination = self.project / "state" / "legacy-new.json"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"old-user-data")
        destination.write_bytes(b"new-user-data")

        with self.assertRaises(pdf_reader._PdfRenameError) as caught:
            pdf_reader._pdf_rename_move_sidecar(source, destination)

        self.assertEqual(
            caught.exception.code,
            "BW_PDF_RENAME_SIDECAR_CONFLICT",
        )
        self.assertEqual(source.read_bytes(), b"old-user-data")
        self.assertEqual(destination.read_bytes(), b"new-user-data")

    def test_chained_rename_moves_same_state_twice(self):
        self._write_state(enabled=False)

        with (
            patch.object(
                pdf_reader,
                "_migrate_page_brief_kg_document",
                return_value=self._kg_result(),
            ),
            patch.object(pdf_reader, "_migrate_book_sidecars"),
        ):
            first = self._post()
            second = self._post(
                old_rel=self.new_rel,
                new_name="final-name.pdf",
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        final_pdf = self.vault / self.third_rel
        self.assertTrue(final_pdf.exists())
        final_brief = json.loads(
            pdf_reader._brief_path_abs(final_pdf).read_text("utf-8")
        )
        self.assertEqual(final_brief["pdf"], str(final_pdf))
        self.assertEqual(final_brief["briefs"]["2"]["kg_status"], "pending")
        settings = json.loads(pdf_reader._BOOK_BRIEF_PATH.read_text("utf-8"))
        self.assertEqual(
            settings,
            {"books/other.pdf": True, self.third_rel: False},
        )
        phases = [
            json.loads(path.read_text("utf-8"))["phase"]
            for path in pdf_reader._pdf_rename_dir().glob("*.json")
        ]
        self.assertEqual(sorted(phases), ["committed", "committed"])


if __name__ == "__main__":
    unittest.main()
