from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "kg"))

from concept_node_service import (  # noqa: E402
    ConceptNodeError,
    ConceptNodeService,
    page_brief_candidates,
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


def _brief(*, include_matrix: bool = False) -> dict:
    concepts = [{
        "name": "linear map",
        "evidence": "A linear map preserves vector addition.",
    }]
    tags = ["linear map"]
    if include_matrix:
        concepts.append({
            "name": "matrix representation",
            "evidence": "Its matrix representation depends on the basis.",
        })
        tags.append("matrix representation")
    return {
        "brief": (
            "Defines a linear map and its matrix representation."
            if include_matrix else
            "Defines a linear map."
        ),
        "tags": tags,
        "concepts": concepts,
        "page_type": "knowledge",
        "subtype": "text",
    }


class ConceptNodeServiceKgDTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = _service(self.root)
        self.file_rel = "资源/books/linear-algebra.pdf"

    def tearDown(self):
        self.temp.cleanup()

    def test_irrelevant_page_text_drift_keeps_mutation_and_signal_stable(self):
        first = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=(
                "Header A. A linear map preserves vector addition. Footer A."
            ),
            brief=_brief(),
            service=self.service,
        )
        replay = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=(
                "Changed header. A linear map preserves vector addition. "
                "Changed footer."
            ),
            brief=_brief(),
            service=self.service,
        )

        self.assertEqual(replay["mutationId"], first["mutationId"])
        self.assertIs(replay["replay"], True)
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(len(node["provenance"]), 1)

    def test_legacy_page_text_bound_source_id_is_semantically_deduplicated(self):
        legacy_source_id = "page-brief:legacy-included-page-text-sha"
        legacy_candidates = page_brief_candidates(
            file_rel=self.file_rel,
            page=12,
            page_text="A linear map preserves vector addition.",
            brief=_brief(),
            source_id=legacy_source_id,
        )
        self.service.upsert_candidates(
            legacy_candidates,
            mutation_id=legacy_source_id,
            source="page-brief",
        )

        migrated = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text="A LINEAR MAP preserves vector addition.",
            brief={
                **_brief(),
                "concepts": [{
                    "name": "linear map",
                    "evidence": "A LINEAR MAP preserves vector addition.",
                }],
            },
            service=self.service,
        )

        self.assertNotEqual(migrated["mutationId"], legacy_source_id)
        self.assertEqual(
            [item["reason"] for item in migrated["deduplicated"]],
            ["evidence-replay"],
        )
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(len(node["provenance"]), 1)
        self.assertEqual(node["provenance"][0]["sourceId"], legacy_source_id)

    def test_semantic_change_can_add_new_concept_without_recounting_old_one(self):
        first = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=(
                "A linear map preserves vector addition. "
                "Its matrix representation depends on the basis."
            ),
            brief=_brief(),
            service=self.service,
        )
        expanded = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=(
                "A linear map preserves vector addition. "
                "Its matrix representation depends on the basis."
            ),
            brief=_brief(include_matrix=True),
            service=self.service,
        )

        self.assertNotEqual(expanded["mutationId"], first["mutationId"])
        self.assertEqual(
            [item["key"] for item in expanded["deduplicated"]],
            ["linear map"],
        )
        self.assertEqual(
            [item["key"] for item in expanded["created"]],
            ["matrix representation"],
        )
        graph = self.service.load_graph()
        self.assertEqual(graph["nodes"]["linear map"]["signal"], 1)
        self.assertEqual(graph["nodes"]["matrix representation"]["signal"], 1)

    def test_current_source_text_is_revalidated_before_mutation_replay(self):
        first = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text="A linear map preserves vector addition.",
            brief=_brief(),
            service=self.service,
        )

        with self.assertRaises(ConceptNodeError) as caught:
            promote_page_brief(
                file_rel=self.file_rel,
                page=12,
                page_text="The quoted evidence no longer exists.",
                brief=_brief(),
                service=self.service,
            )

        self.assertEqual(caught.exception.code, "BW_KG_NODE_EVIDENCE")
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(first["mutationId"].startswith("page-brief:"), True)

    def test_changed_quote_on_same_page_remains_distinct_evidence(self):
        first_brief = _brief()
        changed_brief = {
            **_brief(),
            "brief": "Defines two properties of a linear map.",
            "concepts": [{
                "name": "linear map",
                "evidence": "Every linear map sends zero to zero.",
            }],
        }
        source_text = (
            "A linear map preserves vector addition. "
            "Every linear map sends zero to zero."
        )
        first = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=source_text,
            brief=first_brief,
            service=self.service,
        )
        changed = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=source_text,
            brief=changed_brief,
            service=self.service,
        )

        self.assertNotEqual(changed["mutationId"], first["mutationId"])
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 2)
        self.assertEqual(len(node["provenance"]), 2)

    def test_same_quote_on_other_page_or_document_remains_distinct(self):
        page_text = "A linear map preserves vector addition."
        promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        promote_page_brief(
            file_rel=self.file_rel,
            page=13,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        promote_page_brief(
            file_rel="资源/books/other-linear-algebra.pdf",
            page=12,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )

        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 3)
        self.assertEqual(len(node["provenance"]), 3)

    def test_page_brief_replay_rule_does_not_spill_into_other_sources(self):
        base = {
            "surface": "linear map",
            "sourceKind": "note",
            "documentRef": "vault:note-a",
        }
        self.service.upsert_candidates(
            [{**base, "sourceId": "note:a"}],
            mutation_id="note-mutation-a",
            source="note",
        )
        self.service.upsert_candidates(
            [{**base, "sourceId": "note:b"}],
            mutation_id="note-mutation-b",
            source="note",
        )

        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 2)
        self.assertEqual(len(node["provenance"]), 2)

    def test_brief_or_tags_only_change_does_not_recount_same_evidence(self):
        page_text = "A linear map preserves vector addition."
        first = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        restated = promote_page_brief(
            file_rel=self.file_rel,
            page=12,
            page_text=page_text,
            brief={
                **_brief(),
                "brief": "Restates the defining preservation property.",
                "tags": ["linear transformation"],
            },
            service=self.service,
        )

        self.assertNotEqual(restated["mutationId"], first["mutationId"])
        self.assertEqual(
            [item["reason"] for item in restated["deduplicated"]],
            ["evidence-replay"],
        )
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(len(node["provenance"]), 1)

    def test_concurrent_distinct_mutations_for_same_occurrence_count_once(self):
        page_text = "A linear map preserves vector addition."
        briefs = [
            _brief(),
            {
                **_brief(),
                "brief": "Restates the same linear-map evidence.",
            },
        ]
        barrier = threading.Barrier(2)
        results: list[dict] = []
        failures: list[BaseException] = []

        def promote(brief: dict) -> None:
            try:
                barrier.wait()
                results.append(promote_page_brief(
                    file_rel=self.file_rel,
                    page=12,
                    page_text=page_text,
                    brief=brief,
                    service=self.service,
                ))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [
            threading.Thread(target=promote, args=(brief,))
            for brief in briefs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sum(bool(result["created"]) for result in results),
            1,
        )
        self.assertEqual(
            sum(bool(result["deduplicated"]) for result in results),
            1,
        )
        node = self.service.load_graph()["nodes"]["linear map"]
        self.assertEqual(node["signal"], 1)
        self.assertEqual(len(node["provenance"]), 1)


if __name__ == "__main__":
    unittest.main()
