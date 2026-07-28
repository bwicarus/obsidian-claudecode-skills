from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "kg"))
sys.path.insert(0, str(ROOT / "_client" / "core"))

from concept_node_service import (  # noqa: E402
    ConceptNodeError,
    ConceptNodeService,
    _evidence_match_key,
    _text_key,
)


def _service(root: Path) -> ConceptNodeService:
    return ConceptNodeService(
        graph_path=root / "state" / "emergent-graph.json",
        journal_path=root / "state" / "kg-node-mutations.jsonl",
        aliases_path=root / "state" / "concept-aliases.json",
        confirmations_path=root / "state" / "confirmations.json",
        kg_dir=root / "knowledge_graph",
        concept_root=root / "vault" / "资源" / "概念",
    )


def _candidate(*, surface: str, quote: str, source_text: str) -> dict:
    return {
        "surface": surface,
        "sourceKind": "page-brief",
        "sourceId": "brief:kg-b:p1:v1",
        "documentRef": "book:kg-b.pdf",
        "page": 1,
        "quote": quote,
        "sourceText": source_text,
    }


class ConceptNodeServiceKgBTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = _service(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unicode_nfkc_and_casefold_drift_remains_contiguous(self) -> None:
        cases = [
            (
                "Linear Map",
                "A LINEAR MAP preserves addition.",
                "A linear map preserves addition.",
            ),
            (
                "Straße",
                "STRASSE erklärt Maße.",
                "Die Straße erklärt Maße.",
            ),
            (
                "Kelvin",
                "ＫＥＬＶＩＮ scale",
                "The kelvin scale",
            ),
        ]
        for surface, quote, source_text in cases:
            with self.subTest(surface=surface, quote=quote):
                checked = self.service._validate_candidate(
                    _candidate(
                        surface=surface,
                        quote=quote,
                        source_text=source_text,
                    )
                )
                self.assertEqual(checked["quote"], quote)
                self.assertEqual(
                    checked["quoteSha256"],
                    hashlib.sha256(_text_key(quote).encode("utf-8")).hexdigest(),
                )

        self.assertEqual(_evidence_match_key("Ｓｔｒａßｅ"), "strasse")

    def test_mutation_or_non_contiguous_match_stays_fail_closed(self) -> None:
        cases = [
            (
                "Alpha",
                "Alpha gamma relation",
                "Alpha beta gamma relation",
            ),
            (
                "state-of-the-art",
                "state-of-the-art model",
                "state of the art model",
            ),
            (
                "linear map",
                "linear map preserves addition",
                "linear transformation preserves addition",
            ),
            (
                "vector addition",
                "vector addition",
                "vector scalar addition",
            ),
            (
                "addition vector",
                "addition vector",
                "vector addition",
            ),
        ]
        for surface, quote, source_text in cases:
            with self.subTest(surface=surface, quote=quote):
                with self.assertRaises(ConceptNodeError) as caught:
                    self.service._validate_candidate(
                        _candidate(
                            surface=surface,
                            quote=quote,
                            source_text=source_text,
                        )
                    )
                self.assertEqual(caught.exception.code, "BW_KG_NODE_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
