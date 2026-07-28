from __future__ import annotations

import ast
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "kg"))

import concept_node_service as node_service_module  # noqa: E402
from concept_node_service import (  # noqa: E402
    LOG_CONTRACT,
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


def _note_candidate(index: int, *, surface: str = "vector space") -> dict:
    ref = f"notes/{index}.md"
    return {
        "surface": surface,
        "sourceKind": "note",
        "sourceId": f"note:{ref}",
        "documentRef": f"vault-note:{ref}",
    }


def _brief() -> dict:
    return {
        "brief": "Defines a vector space.",
        "tags": ["vector space"],
        "concepts": [{
            "name": "vector space",
            "evidence": "A vector space is closed under vector addition.",
        }],
        "page_type": "knowledge",
        "subtype": "text",
    }


def _resign_history_rows(rows: list[dict], mutation_id: str) -> None:
    prepare = next(
        row for row in rows
        if row.get("phase") == "prepare"
        and row.get("mutationId") == mutation_id
    )
    commit = next(
        row for row in rows
        if row.get("phase") == "commit"
        and row.get("mutationId") == mutation_id
    )
    prepare["historyDigest"] = node_service_module._digest(
        prepare["history"]
    )
    prepare_body = {
        key: value
        for key, value in prepare.items()
        if key != "prepareDigest"
    }
    prepare["prepareDigest"] = node_service_module._digest(prepare_body)
    commit["historyDigest"] = prepare["historyDigest"]
    commit["prepareDigest"] = prepare["prepareDigest"]


class ConceptNodeServiceKgFTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = _service(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_evicted_hot_receipt_replays_original_result_without_write(self):
        with patch.object(node_service_module, "_MAX_MUTATIONS", 2):
            first = self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="cold-receipt-0",
            )
            self.service.upsert_candidates(
                [_note_candidate(1, surface="inner product")],
                mutation_id="cold-receipt-1",
            )
            self.service.upsert_candidates(
                [_note_candidate(2, surface="linear map")],
                mutation_id="cold-receipt-2",
            )
            self.assertNotIn(
                "cold-receipt-0",
                self.service.load_graph()["meta"]["node_mutations"],
            )
            graph_before = self.service.graph_path.read_bytes()
            journal_before = self.service.journal_path.read_bytes()

            replay = self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="cold-receipt-0",
            )

        self.assertIs(replay["replay"], True)
        self.assertIs(replay["coldReplay"], True)
        self.assertEqual(replay["txId"], first["txId"])
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_same_mutation_id_with_different_payload_fails_closed(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="reuse-id",
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(1)],
                mutation_id="reuse-id",
            )

        self.assertEqual(caught.exception.code, "BW_KG_NODE_MUTATION_REUSE")
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_generic_mutator_is_not_reexecuted_after_hot_eviction(self):
        calls = {"count": 0}

        def mutate(graph):
            calls["count"] += 1
            graph.setdefault("meta", {})["custom"] = calls["count"]
            return {"value": calls["count"]}

        with patch.object(node_service_module, "_MAX_MUTATIONS", 1):
            first = self.service.mutate_graph(
                mutation_id="generic-cold",
                source="test",
                mutator=mutate,
                operation_contract="kg-op/test-generic/1",
                operation_payload={"value": 1},
            )
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="evict-generic",
            )
            replay = self.service.mutate_graph(
                mutation_id="generic-cold",
                source="test",
                mutator=mutate,
                operation_contract="kg-op/test-generic/1",
                operation_payload={"value": 1},
            )

        self.assertEqual(calls["count"], 1)
        self.assertEqual(replay["txId"], first["txId"])
        self.assertIs(replay["coldReplay"], True)

    def test_provenance_eviction_does_not_forget_old_occurrence(self):
        with patch.object(node_service_module, "_MAX_PROVENANCE", 4):
            for index in range(7):
                self.service.upsert_candidates(
                    [_note_candidate(index)],
                    mutation_id=f"provenance-{index}",
                )
        node = self.service.load_graph()["nodes"]["vector space"]
        self.assertEqual(node["signal"], 7)
        self.assertEqual(len(node["provenance"]), 4)

        replay = self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="provenance-old-new-mutation",
        )

        self.assertEqual(
            [item["reason"] for item in replay["deduplicated"]],
            ["evidence-replay"],
        )
        self.assertEqual(
            self.service.load_graph()["nodes"]["vector space"]["signal"],
            7,
        )

    def test_cold_page_brief_occurrence_follows_chained_rename(self):
        old_rel = "books/a.pdf"
        middle_rel = "books/b.pdf"
        final_rel = "books/c.pdf"
        page_text = "A vector space is closed under vector addition."
        promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(node_service_module, "_MAX_PROVENANCE", 4):
            for index in range(6):
                self.service.upsert_candidates(
                    [_note_candidate(index)],
                    mutation_id=f"rename-evict-{index}",
                )
        self.assertFalse(any(
            evidence.get("type") == "page-brief"
            for evidence in self.service.load_graph()["nodes"][
                "vector space"
            ]["provenance"]
        ))

        migrate_page_brief_document(
            old_file_rel=old_rel,
            new_file_rel=middle_rel,
            mutation_id="rename-a-b",
            service=self.service,
        )
        migrate_page_brief_document(
            old_file_rel=middle_rel,
            new_file_rel=final_rel,
            mutation_id="rename-b-c",
            service=self.service,
        )
        at_final = promote_page_brief(
            file_rel=final_rel,
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        self.assertEqual(
            [item["reason"] for item in at_final["deduplicated"]],
            ["evidence-replay"],
        )
        before_old_reuse = self.service.load_graph()["nodes"][
            "vector space"
        ]["signal"]
        reused_path_brief = _brief()
        # 完全相同的旧请求仍应视为客户端重试；改变同一逐字证据之外的
        # PageBrief 语义结果，才能证明旧路径已在 rename 投影后重新可用。
        reused_path_brief["brief"] = "Defines the reused document copy."
        at_reused_old_path = promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=reused_path_brief,
            service=self.service,
        )
        self.assertFalse(at_reused_old_path["deduplicated"])
        node = self.service.load_graph()["nodes"]["vector space"]
        self.assertEqual(node["signal"], before_old_reuse + 1)
        self.assertIn(final_rel, node["books"])

    def test_legacy_v1_mutation_id_is_reserved_but_not_guessed(self):
        seed = _service(self.root / "seed")
        first = seed.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="legacy-v1",
        )
        graph = seed.load_graph()
        graph["meta"].pop("kg_history", None)
        receipt = graph["meta"]["node_mutations"]["legacy-v1"]
        receipt.pop("_kgRequestDigest", None)
        receipt.pop("_kgOperationContract", None)
        node = copy.deepcopy(graph["nodes"]["vector space"])
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        before_graph = {
            "nodes": {},
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        prepare = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": first["txId"],
            "mutationId": "legacy-v1",
            "source": "legacy",
            "graphBeforeDigest": node_service_module._digest(before_graph),
            "graphAfterDigest": node_service_module._digest(graph),
            "beforeNodes": {"vector space": None},
            "afterNodes": {"vector space": node},
        }
        commit = {
            "contract": LOG_CONTRACT,
            "phase": "commit",
            "txId": first["txId"],
            "mutationId": "legacy-v1",
            "source": "legacy",
            "graphDigest": node_service_module._digest(graph),
        }
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in (prepare, commit)
            )
            + "\n",
            "utf-8",
        )

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="legacy-v1",
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_LEGACY_MUTATION",
        )

    def test_incomplete_baseline_fails_closed(self):
        graph = {
            "nodes": {
                "vector space": {
                    "id": "em:test",
                    "surface": "vector space",
                    "signal": 2,
                    "provenance": [{
                        "type": "note",
                        "ref": "notes/0.md",
                    }],
                },
            },
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        self.service.graph_path.parent.mkdir(parents=True)
        self.service.graph_path.write_text(json.dumps(graph), "utf-8")

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(1)],
                mutation_id="incomplete-baseline",
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_INCOMPLETE",
        )
        self.assertFalse(self.service.journal_path.exists())

    def test_middle_corruption_fails_closed_but_torn_tail_is_repaired(self):
        self.service.journal_path.parent.mkdir(parents=True)
        self.service.journal_path.write_bytes(
            (
                '{"contract":"kg-node-mutation-log/1","phase":"noop"}\n'
                "{broken}\n"
            ).encode("utf-8")
        )
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="middle-corrupt",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")

        self.service.journal_path.unlink()
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="before-torn",
        )
        with self.service.journal_path.open("ab") as handle:
            handle.write(b'{"contract":')
        self.service.upsert_candidates(
            [_note_candidate(1, surface="inner product")],
            mutation_id="after-torn",
        )
        rows = self.service._journal_rows()
        self.assertTrue(any(
            row.get("mutationId") == "after-torn"
            and row.get("phase") == "commit"
            for row in rows
        ))

    def test_complete_tail_truncation_is_detected_by_graph_head(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="head-1",
        )
        self.service.upsert_candidates(
            [_note_candidate(1, surface="inner product")],
            mutation_id="head-2",
        )
        rows = self.service._journal_rows()
        trimmed = rows[:-2]
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in trimmed
            )
            + "\n",
            "utf-8",
        )
        graph_before = self.service.graph_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(2, surface="linear map")],
                mutation_id="head-3",
            )

        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)

    def test_history_body_and_current_graph_tampering_fail_closed(self):
        for variant in ("baseline", "history", "prepare", "graph"):
            service = _service(self.root / variant)
            service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id=f"tamper-{variant}",
            )
            if variant in {"baseline", "history", "prepare"}:
                rows = service._journal_rows()
                if variant == "baseline":
                    baseline = next(
                        row
                        for row in rows
                        if row.get("phase") == "history-baseline"
                    )
                    baseline["legacyMutationIds"] = ["forged"]
                else:
                    prepare = next(
                        row
                        for row in rows
                        if row.get("phase") == "prepare"
                        and row.get("mutationId") == f"tamper-{variant}"
                    )
                    if variant == "history":
                        prepare["history"]["occurrencesAdded"][0][
                            "evidenceId"
                        ] = "kgev:tampered"
                    else:
                        prepare["beforeNodes"]["vector space"] = {
                            "signal": 999,
                        }
                service.journal_path.write_text(
                    "\n".join(
                        node_service_module._canonical_json(row)
                        for row in rows
                    )
                    + "\n",
                    "utf-8",
                )
            else:
                graph = service.load_graph()
                graph["nodes"]["vector space"]["signal"] = 999
                service.graph_path.write_text(
                    json.dumps(graph, ensure_ascii=False),
                    "utf-8",
                )
            graph_before = service.graph_path.read_bytes()
            journal_before = service.journal_path.read_bytes()

            with self.assertRaises(ConceptNodeError) as caught:
                service.upsert_candidates(
                    [_note_candidate(1, surface="inner product")],
                    mutation_id=f"after-{variant}-tamper",
                )

            self.assertEqual(
                caught.exception.code,
                "BW_KG_NODE_JOURNAL_CORRUPT",
            )
            self.assertEqual(service.graph_path.read_bytes(), graph_before)
            self.assertEqual(service.journal_path.read_bytes(), journal_before)

    def test_torn_utf8_tail_is_repaired_but_middle_utf8_is_corrupt(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="utf8-base",
        )
        with self.service.journal_path.open("ab") as handle:
            handle.write("中".encode("utf-8")[:2])
        self.service.upsert_candidates(
            [_note_candidate(1, surface="inner product")],
            mutation_id="utf8-after-tail",
        )
        self.assertTrue(any(
            row.get("mutationId") == "utf8-after-tail"
            and row.get("phase") == "commit"
            for row in self.service._journal_rows()
        ))

        rows = self.service.journal_path.read_bytes().splitlines(keepends=True)
        rows.insert(1, b"\xe4\xb8\n")
        self.service.journal_path.write_bytes(b"".join(rows))
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(2, surface="linear map")],
                mutation_id="utf8-middle",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")

    def test_orphan_v1_commit_is_never_minted_into_baseline(self):
        graph = self.service.load_graph()
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            node_service_module._canonical_json({
                "contract": LOG_CONTRACT,
                "phase": "commit",
                "txId": "orphan-tx",
                "mutationId": "orphan-mutation",
                "graphDigest": node_service_module._digest(graph),
            })
            + "\n",
            "utf-8",
        )
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="after-orphan",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")

    def test_v1_snapshot_cannot_be_used_for_new_rollback(self):
        seed = _service(self.root / "v1-seed")
        created = seed.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="legacy-no-hot",
        )
        graph = seed.load_graph()
        graph["meta"].pop("kg_history", None)
        graph["meta"].pop("node_mutations", None)
        node = copy.deepcopy(graph["nodes"]["vector space"])
        before_graph = {
            "nodes": {},
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        prepare = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": created["txId"],
            "mutationId": "legacy-no-hot",
            "source": "legacy",
            "graphBeforeDigest": node_service_module._digest(before_graph),
            "graphAfterDigest": node_service_module._digest(graph),
            # v1 没有 prepareDigest/historyDigest；即使该字段看起来合法，
            # 它也不是可认证的反向快照，绝不能用于 baseline 后的新回滚。
            "beforeNodes": {
                "vector space": {
                    "id": "em:forged",
                    "surface": "FORGED PRIOR",
                    "signal": 777,
                    "provenance": [],
                },
            },
            "afterNodes": {"vector space": node},
        }
        commit = {
            "contract": LOG_CONTRACT,
            "phase": "commit",
            "txId": created["txId"],
            "mutationId": "legacy-no-hot",
            "source": "legacy",
            "graphDigest": node_service_module._digest(graph),
        }
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in (prepare, commit)
            )
            + "\n",
            "utf-8",
        )

        self.assertEqual(
            self.service.mutation_status("legacy-no-hot")["status"],
            "applied",
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.rollback(
                created["txId"],
                mutation_id="legacy-no-hot-rollback",
            )
        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_ROLLBACK_UNSUPPORTED",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)
        self.assertEqual(
            self.service.mutation_status("legacy-no-hot")["status"],
            "applied",
        )
        old_factory = self.service.tx_factory
        self.service.tx_factory = lambda: created["txId"]
        with self.assertRaises(ConceptNodeError) as reused:
            self.service.upsert_candidates(
                [_note_candidate(1, surface="inner product")],
                mutation_id="legacy-tx-reuse",
            )
        self.assertEqual(
            reused.exception.code,
            "BW_KG_NODE_TRANSACTION_REUSE",
        )
        self.service.tx_factory = old_factory
        self.service.upsert_candidates(
            [_note_candidate(1, surface="inner product")],
            mutation_id="after-legacy-rollback",
        )
        self.assertIn(
            "inner product",
            self.service.load_graph()["nodes"],
        )

    def test_page_brief_rename_collision_is_zero_write(self):
        page_text = "A vector space is closed under vector addition."
        promote_page_brief(
            file_rel="books/source.pdf",
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        promote_page_brief(
            file_rel="books/target.pdf",
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            migrate_page_brief_document(
                old_file_rel="books/source.pdf",
                new_file_rel="books/target.pdf",
                mutation_id="rename-collision",
                service=self.service,
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_PROJECTION_CONFLICT",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_stale_graph_precondition_does_not_run_mutator(self):
        initial = self.service.load_graph()
        expected = self.service.graph_digest(initial)
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="stale-intervening",
        )
        calls = {"count": 0}
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        def mutate(graph):
            calls["count"] += 1
            graph.setdefault("meta", {})["shouldNotExist"] = True

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutate_graph(
                mutation_id="stale-operation",
                source="test",
                mutator=mutate,
                operation_contract="kg-op/test-stale/1",
                operation_payload={"graphSha256": expected},
                expected_graph_digest=expected,
            )

        self.assertEqual(caught.exception.code, "BW_KG_NODE_STALE_GRAPH")
        self.assertEqual(calls["count"], 0)
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_generic_mutator_cannot_rewrite_signal_or_provenance(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="generic-node-base",
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        def corrupt_node(graph):
            graph["nodes"]["vector space"]["signal"] = 99
            graph["nodes"]["vector space"]["provenance"] = []

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutate_graph(
                mutation_id="generic-node-bypass",
                source="test",
                mutator=corrupt_node,
                operation_contract="kg-op/test-node-bypass/1",
                operation_payload={"attempt": "signal"},
            )

        self.assertEqual(caught.exception.code, "BW_KG_NODE_IDENTITY")
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_empty_candidate_batch_is_rejected_without_receipt(self):
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [],
                mutation_id="empty-batch",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_CANDIDATE")
        self.assertFalse(self.service.graph_path.exists())
        self.assertFalse(self.service.journal_path.exists())

    def test_rollback_receipt_replays_and_original_success_stays_revoked(self):
        created = self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="rollback-original",
        )
        rolled_back = self.service.rollback(
            created["txId"],
            mutation_id="rollback-operation",
        )
        replay = self.service.rollback(
            created["txId"],
            mutation_id="rollback-operation",
        )
        self.assertIs(replay["replay"], True)
        self.assertEqual(replay["txId"], rolled_back["txId"])

        with self.assertRaises(ConceptNodeError) as original:
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="rollback-original",
            )
        self.assertEqual(
            original.exception.code,
            "BW_KG_NODE_MUTATION_ROLLED_BACK",
        )
        with self.assertRaises(ConceptNodeError) as other_id:
            self.service.rollback(
                created["txId"],
                mutation_id="different-rollback-id",
            )
        self.assertEqual(
            other_id.exception.code,
            "BW_KG_NODE_ROLLBACK_REUSE",
        )
        self.assertEqual(
            self.service.mutation_status("rollback-original")["status"],
            "rolled_back",
        )

    def test_rollback_reverses_cold_page_brief_rename_projection(self):
        page_text = "A vector space is closed under vector addition."
        old_rel = "books/rollback-old.pdf"
        new_rel = "books/rollback-new.pdf"
        promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(node_service_module, "_MAX_PROVENANCE", 1):
            self.service.upsert_candidates(
                [_note_candidate(0)],
                mutation_id="rollback-rename-evict",
            )
        renamed = migrate_page_brief_document(
            old_file_rel=old_rel,
            new_file_rel=new_rel,
            mutation_id="rollback-cold-rename",
            service=self.service,
        )
        self.service.rollback(
            renamed["txId"],
            mutation_id="rollback-cold-rename-operation",
        )
        changed_brief = _brief()
        changed_brief["brief"] = "A semantically refreshed brief."
        old_result = promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=changed_brief,
            service=self.service,
        )
        self.assertEqual(
            [item["reason"] for item in old_result["deduplicated"]],
            ["evidence-replay"],
        )
        signal_before_new = self.service.load_graph()["nodes"][
            "vector space"
        ]["signal"]
        new_result = promote_page_brief(
            file_rel=new_rel,
            page=3,
            page_text=page_text,
            brief=changed_brief,
            service=self.service,
        )
        self.assertFalse(new_result["deduplicated"])
        self.assertEqual(
            self.service.load_graph()["nodes"]["vector space"]["signal"],
            signal_before_new + 1,
        )

    def test_recovery_after_graph_replace_returns_cold_receipt(self):
        real_append = node_service_module._append_jsonl

        def fail_commit(path, row):
            if (
                row.get("phase") == "commit"
                and row.get("mutationId") == "recover-cold"
            ):
                raise OSError("simulated commit append failure")
            return real_append(path, row)

        with patch.object(
            node_service_module,
            "_append_jsonl",
            side_effect=fail_commit,
        ):
            with self.assertRaises(OSError):
                self.service.upsert_candidates(
                    [_note_candidate(0)],
                    mutation_id="recover-cold",
                )

        replay = self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="recover-cold",
        )
        self.assertIs(replay["replay"], True)
        self.assertEqual(
            self.service.load_graph()["nodes"]["vector space"]["signal"],
            1,
        )

    def test_jsonl_uses_only_ascii_lf_as_record_boundary(self):
        for index, separator in enumerate(("\u0085", "\u2028", "\u2029")):
            with self.subTest(codepoint=f"U+{ord(separator):04X}"):
                service = _service(self.root / f"unicode-{index}")
                candidate = _note_candidate(
                    index,
                    surface=f"unicode concept {index}",
                )
                candidate["brief"] = f"left{separator}right"
                service.upsert_candidates(
                    [candidate],
                    mutation_id=f"unicode-{index}",
                )
                with service.journal_path.open("ab") as handle:
                    handle.write(b'{"contract":')
                service.upsert_candidates(
                    [_note_candidate(
                        index + 20,
                        surface=f"after unicode {index}",
                    )],
                    mutation_id=f"unicode-after-{index}",
                )
                self.assertEqual(
                    service.mutation_status(
                        f"unicode-{index}"
                    )["status"],
                    "applied",
                )
                self.assertTrue(any(
                    row.get("mutationId") == f"unicode-after-{index}"
                    and row.get("phase") == "commit"
                    for row in service._journal_rows()
                ))

    def test_corrupt_history_is_rejected_before_recovery_append(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="recover-baseline",
        )
        real_append = node_service_module._append_jsonl

        def fail_commit(path, row):
            if (
                row.get("phase") == "commit"
                and row.get("mutationId") == "recover-unfinished"
            ):
                raise OSError("leave prepare unfinished")
            return real_append(path, row)

        with patch.object(
            node_service_module,
            "_append_jsonl",
            side_effect=fail_commit,
        ):
            with self.assertRaises(OSError):
                self.service.upsert_candidates(
                    [_note_candidate(1, surface="inner product")],
                    mutation_id="recover-unfinished",
                )
        rows = self.service._journal_rows()
        baseline = next(
            row for row in rows
            if row.get("phase") == "history-baseline"
        )
        baseline["legacyTransactionIds"] = ["forged"]
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.recover()

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_JOURNAL_CORRUPT",
        )
        self.assertEqual(
            self.service.journal_path.read_bytes(),
            journal_before,
        )

    def test_corrupt_v1_prefix_is_rejected_before_recovery_append(self):
        seed = _service(self.root / "recover-v1-seed")
        created = seed.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="recover-v1-committed",
        )
        graph = seed.load_graph()
        graph["meta"].pop("kg_history", None)
        graph["meta"].pop("node_mutations", None)
        before_graph = {
            "nodes": {},
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        first_prepare = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": created["txId"],
            "mutationId": "recover-v1-committed",
            "graphBeforeDigest": node_service_module._digest(before_graph),
            "graphAfterDigest": node_service_module._digest(graph),
            "beforeNodes": {"vector space": None},
            "afterNodes": {
                "vector space": copy.deepcopy(
                    graph["nodes"]["vector space"]
                ),
            },
        }
        corrupt_commit = {
            "contract": LOG_CONTRACT,
            "phase": "commit",
            "txId": created["txId"],
            "mutationId": "recover-v1-committed",
            "graphDigest": "0" * 64,
        }
        unfinished = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": "recover-v1-unfinished-tx",
            "mutationId": "recover-v1-unfinished",
            "graphBeforeDigest": node_service_module._digest(graph),
            "graphAfterDigest": "1" * 64,
            "beforeNodes": {},
            "afterNodes": {},
        }
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in (
                    first_prepare,
                    corrupt_commit,
                    unfinished,
                )
            ) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError):
            self.service.recover()

        self.assertEqual(
            self.service.journal_path.read_bytes(),
            journal_before,
        )

    def test_post_baseline_prepares_must_be_strictly_serial(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="serial-a",
        )
        self.service.upsert_candidates(
            [_note_candidate(1, surface="inner product")],
            mutation_id="serial-b",
        )
        rows = self.service._journal_rows()
        baseline = [
            row for row in rows
            if row.get("phase") == "history-baseline"
        ]
        prepare_a = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "serial-a"
        )
        commit_a = next(
            row for row in rows
            if row.get("phase") == "commit"
            and row.get("mutationId") == "serial-a"
        )
        prepare_b = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "serial-b"
        )
        commit_b = next(
            row for row in rows
            if row.get("phase") == "commit"
            and row.get("mutationId") == "serial-b"
        )
        interleaved = baseline + [
            prepare_a,
            prepare_b,
            commit_a,
            commit_b,
        ]
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in interleaved
            ) + "\n",
            "utf-8",
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutation_status("serial-b")

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_JOURNAL_CORRUPT",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_transaction_id_reuse_is_zero_write(self):
        created = self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="tx-original",
        )
        self.service.tx_factory = lambda: created["txId"]
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(1, surface="inner product")],
                mutation_id="tx-collision",
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_TRANSACTION_REUSE",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_history_occurrence_delta_must_match_node_snapshots(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="occurrence-source",
        )
        rows = self.service._journal_rows()
        prepare = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "occurrence-source"
        )
        commit = next(
            row for row in rows
            if row.get("phase") == "commit"
            and row.get("mutationId") == "occurrence-source"
        )
        prepare["history"]["occurrencesAdded"] = []
        prepare["historyDigest"] = node_service_module._digest(
            prepare["history"]
        )
        prepare_body = {
            key: value
            for key, value in prepare.items()
            if key != "prepareDigest"
        }
        prepare["prepareDigest"] = node_service_module._digest(prepare_body)
        commit["historyDigest"] = prepare["historyDigest"]
        commit["prepareDigest"] = prepare["prepareDigest"]
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(1, surface="inner product")],
                mutation_id="after-occurrence-tamper",
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_JOURNAL_CORRUPT",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_rollback_occurrence_removal_cannot_be_relabelled_or_omitted(self):
        self.service.upsert_candidates(
            [_note_candidate(0)],
            mutation_id="removal-base",
        )
        added = self.service.upsert_candidates(
            [_note_candidate(1)],
            mutation_id="removal-added",
        )
        self.service.rollback(
            added["txId"],
            mutation_id="removal-rollback",
        )
        rows = self.service._journal_rows()
        rollback_prepare = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "removal-rollback"
        )
        self.assertTrue(
            rollback_prepare["history"]["occurrencesRemoved"]
        )
        rollback_prepare["history"]["occurrencesRemoved"] = []
        _resign_history_rows(rows, "removal-rollback")
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutation_status("removal-rollback")

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_JOURNAL_CORRUPT",
        )
        self.assertEqual(
            self.service.journal_path.read_bytes(),
            journal_before,
        )

    def test_page_brief_projection_moves_must_cover_all_cold_sources(self):
        page_text = "A vector space is closed under vector addition."
        promote_page_brief(
            file_rel="books/projection-old.pdf",
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(node_service_module, "_MAX_PROVENANCE", 1):
            self.service.upsert_candidates(
                [_note_candidate(45)],
                mutation_id="projection-evict",
            )
        migrate_page_brief_document(
            old_file_rel="books/projection-old.pdf",
            new_file_rel="books/projection-new.pdf",
            mutation_id="projection-rename",
            service=self.service,
        )
        rows = self.service._journal_rows()
        rename_prepare = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "projection-rename"
        )
        self.assertTrue(rename_prepare["history"]["projections"][0]["moves"])
        rename_prepare["history"]["projections"][0]["moves"] = []
        _resign_history_rows(rows, "projection-rename")
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutation_status("projection-rename")

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_JOURNAL_CORRUPT",
        )
        self.assertEqual(
            self.service.journal_path.read_bytes(),
            journal_before,
        )

    def test_cold_page_brief_rename_collision_is_zero_write(self):
        page_text = "A vector space is closed under vector addition."
        promote_page_brief(
            file_rel="books/cold-source.pdf",
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        promote_page_brief(
            file_rel="books/cold-target.pdf",
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(node_service_module, "_MAX_PROVENANCE", 1):
            self.service.upsert_candidates(
                [_note_candidate(30)],
                mutation_id="cold-collision-evict",
            )
        graph_before = self.service.graph_path.read_bytes()
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            migrate_page_brief_document(
                old_file_rel="books/cold-source.pdf",
                new_file_rel="books/cold-target.pdf",
                mutation_id="cold-rename-collision",
                service=self.service,
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_PROJECTION_CONFLICT",
        )
        self.assertEqual(self.service.graph_path.read_bytes(), graph_before)
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_history_only_rename_can_be_rolled_back(self):
        page_text = "A vector space is closed under vector addition."
        old_rel = "books/history-only-old.pdf"
        new_rel = "books/history-only-new.pdf"
        promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        promote_page_brief(
            file_rel=new_rel,
            page=4,
            page_text=page_text,
            brief=_brief(),
            service=self.service,
        )
        with patch.object(node_service_module, "_MAX_PROVENANCE", 1):
            self.service.upsert_candidates(
                [_note_candidate(31)],
                mutation_id="history-only-evict",
            )
        renamed = migrate_page_brief_document(
            old_file_rel=old_rel,
            new_file_rel=new_rel,
            mutation_id="history-only-rename",
            service=self.service,
        )
        self.assertEqual(renamed["changedNodes"], [])
        self.service.rollback(
            renamed["txId"],
            mutation_id="history-only-rollback",
        )
        changed = _brief()
        changed["brief"] = "Changed only outside the evidence identity."
        old_result = promote_page_brief(
            file_rel=old_rel,
            page=3,
            page_text=page_text,
            brief=changed,
            service=self.service,
        )
        new_result = promote_page_brief(
            file_rel=new_rel,
            page=4,
            page_text=page_text,
            brief=changed,
            service=self.service,
        )
        self.assertEqual(
            [item["reason"] for item in old_result["deduplicated"]],
            ["evidence-replay"],
        )
        self.assertEqual(
            [item["reason"] for item in new_result["deduplicated"]],
            ["evidence-replay"],
        )

    def test_rollback_target_cannot_be_rebound_to_other_transaction(self):
        created_alpha = self.service.upsert_candidates(
            [_note_candidate(40, surface="alpha")],
            mutation_id="rollback-bind-alpha",
        )
        created_beta = self.service.upsert_candidates(
            [_note_candidate(41, surface="beta")],
            mutation_id="rollback-bind-beta",
        )
        self.service.rollback(
            created_alpha["txId"],
            mutation_id="rollback-bind-action",
        )
        rows = self.service._journal_rows()
        prepare = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "rollback-bind-action"
        )
        commit = next(
            row for row in rows
            if row.get("phase") == "commit"
            and row.get("mutationId") == "rollback-bind-action"
        )
        forged_target = created_beta["txId"]
        _, request_digest = self.service._operation_identity(
            "kg-op/rollback/1",
            {"rollbackOfTxId": forged_target},
        )
        prepare["commitExtra"]["rollbackOf"] = forged_target
        history = prepare["history"]
        history["commitExtra"]["rollbackOf"] = forged_target
        history["receipt"]["requestDigest"] = request_digest
        history["receipt"]["result"]["rollbackOf"] = forged_target
        history["resultDigest"] = node_service_module._digest(
            history["receipt"]["result"]
        )
        commit["rollbackOf"] = forged_target
        commit["requestDigest"] = request_digest
        commit["resultDigest"] = history["resultDigest"]
        _resign_history_rows(rows, "rollback-bind-action")
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutation_status("rollback-bind-action")

        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_signal_must_equal_durable_occurrence_count(self):
        self.service.upsert_candidates(
            [_note_candidate(42, surface="signal proof")],
            mutation_id="signal-proof",
        )
        rows = self.service._journal_rows()
        prepare = next(
            row for row in rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "signal-proof"
        )
        commit = next(
            row for row in rows
            if row.get("phase") == "commit"
            and row.get("mutationId") == "signal-proof"
        )
        graph = self.service.load_graph()
        prepare["afterNodes"]["signal proof"]["signal"] = 99
        graph["nodes"]["signal proof"]["signal"] = 99
        forged_graph_digest = node_service_module._digest(graph)
        prepare["graphAfterDigest"] = forged_graph_digest
        commit["graphDigest"] = forged_graph_digest
        _resign_history_rows(rows, "signal-proof")
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in rows
            ) + "\n",
            "utf-8",
        )

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.mutation_status("signal-proof")

        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")

    def test_malformed_first_v1_prepare_is_rejected_before_recovery_write(self):
        graph = self.service.load_graph()
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        unfinished = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": "malformed-first-v1-tx",
            "mutationId": "malformed-first-v1",
            "graphBeforeDigest": "",
            "graphAfterDigest": node_service_module._digest(graph),
            "beforeNodes": {},
            "afterNodes": {},
        }
        self.service.journal_path.write_text(
            node_service_module._canonical_json(unfinished) + "\n",
            "utf-8",
        )
        journal_before = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.recover()

        self.assertEqual(caught.exception.code, "BW_KG_NODE_JOURNAL_CORRUPT")
        self.assertEqual(self.service.journal_path.read_bytes(), journal_before)

    def test_invalid_legacy_receipt_is_quarantined_from_cold_replay(self):
        seed = _service(self.root / "baseline-write-seed")
        created = seed.upsert_candidates(
            [_note_candidate(43, surface="legacy receipt")],
            mutation_id="legacy-receipt",
        )
        seed_rows = seed._journal_rows()
        seed_prepare = next(
            row for row in seed_rows
            if row.get("phase") == "prepare"
            and row.get("mutationId") == "legacy-receipt"
        )
        graph = seed.load_graph()
        graph["meta"].pop("kg_history", None)
        graph["meta"]["node_mutations"]["legacy-receipt"][
            "mutationId"
        ] = "forged-mutation"
        graph_digest = node_service_module._digest(graph)
        prepare = {
            "contract": LOG_CONTRACT,
            "phase": "prepare",
            "txId": created["txId"],
            "mutationId": "legacy-receipt",
            "source": "legacy",
            "graphBeforeDigest": seed_prepare["graphBeforeDigest"],
            "graphAfterDigest": graph_digest,
            "beforeNodes": copy.deepcopy(seed_prepare["beforeNodes"]),
            "afterNodes": copy.deepcopy(seed_prepare["afterNodes"]),
        }
        commit = {
            "contract": LOG_CONTRACT,
            "phase": "commit",
            "txId": created["txId"],
            "mutationId": "legacy-receipt",
            "source": "legacy",
            "graphDigest": graph_digest,
        }
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in (prepare, commit)
            ) + "\n",
            "utf-8",
        )
        status = self.service.mutation_status("legacy-receipt")
        self.assertEqual(status["status"], "applied")
        self.assertIsNone(status["receipt"])
        rows = self.service._journal_rows()
        baseline = next(
            row for row in rows
            if row.get("phase") == "history-baseline"
        )
        self.assertNotIn("legacy-receipt", baseline["receipts"])
        journal_after_baseline = self.service.journal_path.read_bytes()

        with self.assertRaises(ConceptNodeError) as caught:
            self.service.upsert_candidates(
                [_note_candidate(43, surface="legacy receipt")],
                mutation_id="legacy-receipt",
            )

        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_LEGACY_MUTATION",
        )
        self.assertEqual(
            self.service.journal_path.read_bytes(),
            journal_after_baseline,
        )

    def test_legacy_rollback_receipt_must_match_v1_target_or_is_quarantined(self):
        seed = _service(self.root / "legacy-rollback-seed")
        created_alpha = seed.upsert_candidates(
            [_note_candidate(44, surface="legacy alpha")],
            mutation_id="legacy-create-alpha",
        )
        graph_alpha = seed.load_graph()
        graph_alpha["meta"].pop("kg_history", None)
        created_beta = seed.upsert_candidates(
            [_note_candidate(45, surface="legacy beta")],
            mutation_id="legacy-create-beta",
        )
        graph_beta = seed.load_graph()
        graph_beta["meta"].pop("kg_history", None)
        rolled_back = seed.rollback(
            created_alpha["txId"],
            mutation_id="legacy-rollback",
        )
        final_graph = seed.load_graph()
        final_graph["meta"].pop("kg_history", None)
        final_graph["meta"]["node_mutations"]["legacy-rollback"][
            "rollbackOf"
        ] = "forged-target"
        seed_rows = seed._journal_rows()
        empty_graph = {
            "nodes": {},
            "edges": [],
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        mutations = (
            (
                "legacy-create-alpha",
                created_alpha,
                empty_graph,
                graph_alpha,
                "",
            ),
            (
                "legacy-create-beta",
                created_beta,
                graph_alpha,
                graph_beta,
                "",
            ),
            (
                "legacy-rollback",
                rolled_back,
                graph_beta,
                final_graph,
                created_alpha["txId"],
            ),
        )
        v1_rows = []
        for mutation_id, result, before_graph, after_graph, rollback_of in mutations:
            source_prepare = next(
                row for row in seed_rows
                if row.get("phase") == "prepare"
                and row.get("mutationId") == mutation_id
            )
            prepare = {
                "contract": LOG_CONTRACT,
                "phase": "prepare",
                "txId": result["txId"],
                "mutationId": mutation_id,
                "source": source_prepare.get("source", "legacy"),
                "graphBeforeDigest": node_service_module._digest(before_graph),
                "graphAfterDigest": node_service_module._digest(after_graph),
                "beforeNodes": copy.deepcopy(source_prepare["beforeNodes"]),
                "afterNodes": copy.deepcopy(source_prepare["afterNodes"]),
            }
            commit = {
                "contract": LOG_CONTRACT,
                "phase": "commit",
                "txId": result["txId"],
                "mutationId": mutation_id,
                "source": source_prepare.get("source", "legacy"),
                "graphDigest": node_service_module._digest(after_graph),
            }
            if rollback_of:
                commit["rollbackOf"] = rollback_of
            v1_rows.extend((prepare, commit))
        self.service.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.graph_path.write_text(
            json.dumps(final_graph, ensure_ascii=False),
            "utf-8",
        )
        self.service.journal_path.write_text(
            "\n".join(
                node_service_module._canonical_json(row)
                for row in v1_rows
            ) + "\n",
            "utf-8",
        )

        status = self.service.mutation_status("legacy-rollback")

        self.assertEqual(status["status"], "applied")
        self.assertIsNone(status["receipt"])
        baseline = next(
            row for row in self.service._journal_rows()
            if row.get("phase") == "history-baseline"
        )
        self.assertEqual(
            baseline["legacyRollbackTargets"],
            {rolled_back["txId"]: created_alpha["txId"]},
        )
        self.assertNotIn("legacy-rollback", baseline["receipts"])
        with self.assertRaises(ConceptNodeError) as caught:
            self.service.rollback(
                created_alpha["txId"],
                mutation_id="legacy-rollback",
            )
        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_HISTORY_LEGACY_MUTATION",
        )

    def test_all_production_callers_declare_operation_contracts(self):
        for relative in (
            "scripts/kg/concept_node_service.py",
            "scripts/kg/promote_concepts.py",
            "scripts/kg/propose_concept_notes.py",
            "scripts/kg/audit_edges.py",
        ):
            tree = ast.parse((ROOT / relative).read_text("utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not (
                    isinstance(function, ast.Attribute)
                    and function.attr in {
                        "upsert_candidates",
                        "mutate_graph",
                    }
                ):
                    continue
                keywords = {
                    keyword.arg: keyword.value
                    for keyword in node.keywords
                    if keyword.arg
                }
                self.assertIn("operation_contract", keywords, relative)
                self.assertIn("operation_payload", keywords, relative)
                self.assertIsInstance(
                    keywords["operation_contract"],
                    ast.Constant,
                    relative,
                )
                self.assertIsInstance(
                    keywords["operation_contract"].value,
                    str,
                    relative,
                )


if __name__ == "__main__":
    unittest.main()
