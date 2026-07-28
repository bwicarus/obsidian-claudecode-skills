from __future__ import annotations

import errno
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "kg"))
sys.path.insert(0, str(ROOT / "_client" / "core"))

from concept_node_service import (  # noqa: E402
    ConceptNodeError,
    ConceptNodeService,
    _acquire_windows_file_lock,
    _exclusive_file_lock,
    page_brief_candidates,
    promote_page_brief,
    stable_node_id,
)
from gen_page_brief import _parse, _verified_concepts  # noqa: E402


def _service(tmp_path: Path, **kwargs) -> ConceptNodeService:
    paths = {
        "graph_path": tmp_path / "state" / "emergent-graph.json",
        "journal_path": tmp_path / "state" / "kg-node-mutations.jsonl",
        "aliases_path": tmp_path / "state" / "concept-aliases.json",
        "confirmations_path": tmp_path / "state" / "confirmations.json",
        "kg_dir": tmp_path / "knowledge_graph",
        "concept_root": tmp_path / "vault" / "资源" / "概念",
        "clock": lambda: 1_700_000_000,
    }
    paths.update(kwargs)
    return ConceptNodeService(**paths)


def _page_candidate(
    *,
    surface: str = "线性映射",
    source_id: str = "brief:book-a:p12:v2",
    quote: str = "线性映射保持向量加法。",
    source_text: str = "定义：线性映射保持向量加法。随后讨论矩阵表示。",
) -> dict:
    return {
        "surface": surface,
        "sourceKind": "page-brief",
        "sourceId": source_id,
        "documentRef": "book:资源/books/a.pdf",
        "book": "资源/books/a.pdf",
        "page": 12,
        "quote": quote,
        "sourceText": source_text,
        "brief": "定义线性映射并讨论矩阵表示。",
    }


class ConceptNodeServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_has_stable_id_provenance_and_mutation_replay(self):
        service = _service(self.tmp_path)
        first = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="page-brief:a:12:v2",
            source="page-brief",
        )

        self.assertEqual(first["created"], [{
            "key": "线性映射",
            "nodeId": stable_node_id("线性映射"),
            "surface": "线性映射",
        }])
        self.assertTrue(first["txId"].startswith("kgntx-"))
        graph = service.load_graph()
        node = graph["nodes"]["线性映射"]
        self.assertEqual(node["id"], stable_node_id("线性映射"))
        self.assertEqual(node["signal"], 1)
        self.assertEqual(node["provenance"][0]["quote"], "线性映射保持向量加法。")
        self.assertEqual(
            graph["meta"]["node_mutations"]["page-brief:a:12:v2"]["txId"],
            first["txId"],
        )

        replay = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="page-brief:a:12:v2",
            source="page-brief",
        )
        self.assertIs(replay["replay"], True)
        self.assertEqual(replay["txId"], first["txId"])
        self.assertEqual(service.load_graph()["nodes"]["线性映射"]["signal"], 1)

        evidence_replay = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="page-brief:a:12:v2:retry",
            source="page-brief",
        )
        self.assertEqual(
            evidence_replay["deduplicated"][0]["reason"],
            "evidence-replay",
        )
        self.assertTrue(evidence_replay["txId"])
        self.assertEqual(service.load_graph()["nodes"]["线性映射"]["signal"], 1)

    def test_page_sources_require_exact_quote_and_tags_are_not_sources(self):
        service = _service(self.tmp_path)
        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
            [{
                "surface": "矩阵",
                "sourceKind": "tag",
                "sourceId": "tag:1",
                "documentRef": "book:a",
            }],
            mutation_id="bad-tag",
        )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_SOURCE")

        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
                [_page_candidate(quote="原文中不存在的改写")],
                mutation_id="bad-quote",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_EVIDENCE")
        self.assertFalse(service.graph_path.exists())

    def test_alias_and_authored_kg_deduplicate_before_creation(self):
        service = _service(self.tmp_path)
        service.aliases_path.parent.mkdir(parents=True)
        service.aliases_path.write_text(
            json.dumps({"linear map": ["Linear Mapping"]}),
            "utf-8",
        )
        service.kg_dir.mkdir(parents=True)
        (service.kg_dir / "book.json").write_text(
            json.dumps({
                "book": "Linear Algebra",
                "nodes": [{"id": "l2-eigen", "level": 2, "name": "Eigenvalue"}],
            }),
            "utf-8",
        )

        alias_result = service.upsert_candidates(
            [{
                "surface": "LINEAR MAPPING",
                "sourceKind": "note",
                "sourceId": "note:1",
                "documentRef": "vault:note-1",
            }],
            mutation_id="alias-1",
        )
        self.assertEqual(alias_result["created"][0]["key"], "linear map")
        self.assertEqual(
            alias_result["created"][0]["nodeId"],
            stable_node_id("linear map"),
        )

        authored_result = service.upsert_candidates(
            [{
                "surface": "eigenvalue",
                "sourceKind": "note",
                "sourceId": "note:2",
                "documentRef": "vault:note-2",
            }],
            mutation_id="authored-1",
        )
        self.assertEqual(authored_result["created"], [])
        self.assertEqual(
            authored_result["anchored"][0]["authoredRef"],
            "Linear Algebra#l2-eigen",
        )
        self.assertEqual(
            service.load_graph()["nodes"]["eigenvalue"]["authored_ref"],
            "Linear Algebra#l2-eigen",
        )
        self.assertEqual(
            service.load_graph()["nodes"]["eigenvalue"]["provenance"][0]["sourceId"],
            "note:2",
        )

    def test_rollback_creates_tombstone_and_auto_upsert_cannot_revive(self):
        service = _service(self.tmp_path)
        created = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="create-1",
        )
        rollback = service.rollback(created["txId"], mutation_id="rollback-1")
        self.assertEqual(rollback["rollbackOf"], created["txId"])
        node = service.load_graph()["nodes"]["线性映射"]
        self.assertEqual(node["id"], stable_node_id("线性映射"))
        self.assertIs(node["deleted"], True)
        self.assertEqual(node["tombstone"]["rollbackOf"], created["txId"])

        blocked = service.upsert_candidates(
            [_page_candidate(source_id="brief:book-a:p13:v2")],
            mutation_id="create-2",
        )
        self.assertEqual(blocked["rejected"][0]["reason"], "tombstoned")
        self.assertEqual(blocked["created"], [])

        replay = service.rollback(created["txId"], mutation_id="rollback-1")
        self.assertIs(replay["replay"], True)

    def test_rollback_refuses_to_clobber_later_evidence(self):
        service = _service(self.tmp_path)
        first = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="create-1",
        )
        service.upsert_candidates(
            [_page_candidate(
                source_id="brief:book-a:p13:v2",
                quote="线性映射的矩阵表示",
                source_text="下一页讨论线性映射的矩阵表示。",
            )],
            mutation_id="create-2",
        )
        with self.assertRaises(ConceptNodeError) as caught:
            service.rollback(first["txId"], mutation_id="rollback-conflict")
        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_ROLLBACK_CONFLICT",
        )

    def test_same_mutation_is_serialized_across_threads(self):
        service = _service(self.tmp_path)
        results: list[dict] = []
        failures: list[Exception] = []

        def run():
            try:
                results.append(service.upsert_candidates(
                    [_page_candidate()],
                    mutation_id="concurrent-one",
                ))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(
            sum(
                bool(result.get("created")) and not result.get("replay")
                for result in results
            ),
            1,
        )
        self.assertEqual(
            sum(result.get("replay") is True for result in results),
            7,
        )
        self.assertEqual(service.load_graph()["nodes"]["线性映射"]["signal"], 1)

    def test_distinct_mutations_are_serialized_across_subprocesses(self):
        service = _service(self.tmp_path)
        worker = r"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts" / "kg"))
from concept_node_service import ConceptNodeService

paths = json.loads(sys.argv[2])
index = int(sys.argv[3])
ready_dir = Path(sys.argv[4])
start_path = Path(sys.argv[5])
ready_dir.mkdir(parents=True, exist_ok=True)
(ready_dir / str(index)).write_text("ready", encoding="utf-8")
deadline = __import__("time").monotonic() + 10
while not start_path.exists():
    if __import__("time").monotonic() >= deadline:
        raise RuntimeError("start barrier timeout")
    __import__("time").sleep(0.005)
service = ConceptNodeService(**{key: Path(value) for key, value in paths.items()})
service.upsert_candidates(
    [{
        "surface": f"Concurrent {index}",
        "sourceKind": "note",
        "sourceId": f"note:concurrent:{index}",
        "documentRef": f"vault:concurrent:{index}",
    }],
    mutation_id=f"subprocess-{index}",
    source="test-subprocess",
)
"""
        paths = {
            "graph_path": str(service.graph_path),
            "journal_path": str(service.journal_path),
            "aliases_path": str(service.aliases_path),
            "confirmations_path": str(service.confirmations_path),
            "kg_dir": str(service.kg_dir),
            "concept_root": str(service.concept_root),
        }
        ready_dir = self.tmp_path / "workers-ready"
        start_path = self.tmp_path / "workers-start"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(ROOT),
                    json.dumps(paths),
                    str(index),
                    str(ready_dir),
                    str(start_path),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(8)
        ]
        try:
            deadline = time.monotonic() + 10
            while (
                len(list(ready_dir.glob("*"))) < len(processes)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(len(list(ready_dir.glob("*"))), len(processes))
            start_path.write_text("start", "utf-8")
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                if process.returncode:
                    failures.append((process.returncode, stdout, stderr))
            self.assertEqual(failures, [])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        graph = service.load_graph()
        self.assertEqual(
            sorted(graph["nodes"]),
            [f"concurrent {index}" for index in range(8)],
        )
        commits = [
            row for row in service._journal_rows()
            if row.get("phase") == "commit"
        ]
        self.assertEqual(len(commits), 8)
        self.assertEqual(
            {row.get("mutationId") for row in commits},
            {f"subprocess-{index}" for index in range(8)},
        )

    def test_file_lock_times_out_and_kernel_releases_after_process_exit(self):
        lock_path = self.tmp_path / "state" / "held.lock"
        ready_path = self.tmp_path / "holder-ready"
        holder_code = r"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts" / "kg"))
from concept_node_service import _exclusive_file_lock

with _exclusive_file_lock(Path(sys.argv[2]), timeout_seconds=2):
    Path(sys.argv[3]).write_text("ready", encoding="utf-8")
    __import__("time").sleep(30)
"""
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                holder_code,
                str(ROOT),
                str(lock_path),
                str(ready_path),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready_path.exists() and time.monotonic() < deadline:
                if holder.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(
                ready_path.exists(),
                holder.stderr.read() if holder.poll() is not None else "",
            )
            with self.assertRaises(ConceptNodeError) as caught:
                with _exclusive_file_lock(
                    lock_path,
                    timeout_seconds=0.1,
                    poll_seconds=0.01,
                ):
                    self.fail("contended lock must not be entered")
            self.assertEqual(caught.exception.code, "BW_KG_NODE_LOCK_TIMEOUT")
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=5)

        # 进程被异常终止后，内核会释放其句柄锁；下一次写入应立即可用。
        with _exclusive_file_lock(lock_path, timeout_seconds=2):
            pass

    def test_file_lock_is_released_when_mutation_raises(self):
        lock_path = self.tmp_path / "state" / "exception.lock"
        with self.assertRaisesRegex(RuntimeError, "mutation failed"):
            with _exclusive_file_lock(lock_path, timeout_seconds=1):
                raise RuntimeError("mutation failed")
        with _exclusive_file_lock(lock_path, timeout_seconds=1):
            pass

    def test_windows_lock_backend_uses_fixed_byte_and_fails_closed(self):
        lock_path = self.tmp_path / "state" / "windows.lock"
        lock_path.parent.mkdir(parents=True)
        calls = []

        def locking(_fd, mode, length):
            calls.append((mode, length))

        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=10,
            LK_UNLCK=11,
            locking=locking,
        )
        with lock_path.open("a+b") as handle:
            with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
                unlock = _acquire_windows_file_lock(
                    handle,
                    lock_path,
                    deadline=time.monotonic() + 1,
                    poll_seconds=0.01,
                )
                unlock()
        self.assertEqual(calls, [(10, 1), (11, 1)])
        self.assertEqual(lock_path.read_bytes(), b"\0")

        def busy(_fd, _mode, _length):
            raise OSError(errno.EACCES, "lock violation")

        fake_msvcrt.locking = busy
        with lock_path.open("a+b") as handle:
            with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
                with self.assertRaises(ConceptNodeError) as caught:
                    _acquire_windows_file_lock(
                        handle,
                        lock_path,
                        deadline=time.monotonic() + 0.02,
                        poll_seconds=0.005,
                    )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_LOCK_TIMEOUT")

    def test_same_batch_alias_is_resolved_to_one_node(self):
        service = _service(self.tmp_path)
        result = service.upsert_candidates(
            [
                {
                    "surface": "Vector space",
                    "aliases": ["VS"],
                    "sourceKind": "note",
                    "sourceId": "note:vector-space",
                    "documentRef": "vault:vector-space",
                },
                {
                    "surface": "VS",
                    "sourceKind": "note",
                    "sourceId": "note:vs",
                    "documentRef": "vault:vs",
                },
            ],
            mutation_id="same-batch-alias",
        )
        self.assertEqual(len(result["created"]), 1)
        graph = service.load_graph()
        self.assertEqual(list(graph["nodes"]), ["vector space"])
        self.assertEqual(graph["nodes"]["vector space"]["signal"], 2)

    def test_corrupt_graph_and_identity_dependencies_fail_closed(self):
        service = _service(self.tmp_path)
        service.graph_path.parent.mkdir(parents=True)
        service.graph_path.write_text("{broken", "utf-8")
        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
                [_page_candidate()],
                mutation_id="corrupt-graph",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_GRAPH_CORRUPT")
        self.assertEqual(service.graph_path.read_text("utf-8"), "{broken")

        service.graph_path.unlink()
        service.aliases_path.write_text("{broken", "utf-8")
        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
                [_page_candidate()],
                mutation_id="corrupt-alias",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_ALIASES_CORRUPT")
        self.assertFalse(service.graph_path.exists())

    def test_concurrent_rollback_is_one_commit_then_replays(self):
        service = _service(self.tmp_path)
        created = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="create-concurrent-rollback",
        )
        results: list[dict] = []
        failures: list[Exception] = []

        def run():
            try:
                results.append(service.rollback(
                    created["txId"],
                    mutation_id="rollback-concurrent",
                ))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(sum(not row.get("replay") for row in results), 1)
        self.assertEqual(sum(row.get("replay") is True for row in results), 5)

    def test_stable_id_collision_fails_closed(self):
        service = _service(self.tmp_path)
        service.graph_path.parent.mkdir(parents=True)
        service.graph_path.write_text(json.dumps({
            "nodes": {
                "alpha": {"id": stable_node_id("beta"), "surface": "alpha"},
                "beta": {"id": stable_node_id("beta"), "surface": "beta"},
            },
            "edges": [],
            "meta": {},
        }), "utf-8")

        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
            [{
                "surface": "alpha",
                "sourceKind": "note",
                "sourceId": "note:alpha",
                "documentRef": "vault:alpha",
            }],
            mutation_id="collision",
        )
        self.assertEqual(
            caught.exception.code,
            "BW_KG_NODE_ID_COLLISION",
        )

    def test_generic_graph_mutation_cannot_add_or_revive_nodes(self):
        service = _service(self.tmp_path)
        created = service.upsert_candidates(
            [_page_candidate()],
            mutation_id="generic-base",
        )
        service.rollback(created["txId"], mutation_id="generic-rollback")

        def revive(graph):
            graph["nodes"]["线性映射"].pop("deleted", None)
            graph["nodes"]["线性映射"].pop("tombstone", None)

        with self.assertRaises(ConceptNodeError) as caught:
            service.mutate_graph(
                mutation_id="generic-revive",
                source="test",
                mutator=revive,
                operation_contract="kg-op/test-revive/1",
                operation_payload={"target": "线性映射"},
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_IDENTITY")
        self.assertIs(service.load_graph()["nodes"]["线性映射"]["deleted"], True)

        def add_node(graph):
            graph["nodes"]["bypass"] = {
                "id": "em:bypass",
                "surface": "bypass",
            }

        with self.assertRaises(ConceptNodeError) as caught:
            service.mutate_graph(
                mutation_id="generic-add",
                source="test",
                mutator=add_node,
                operation_contract="kg-op/test-add/1",
                operation_payload={"target": "bypass"},
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_IDENTITY")
        self.assertNotIn("bypass", service.load_graph()["nodes"])

    def test_page_brief_adapter_and_parser_only_keep_verifiable_concepts(self):
        parsed = _parse(json.dumps({
            "page_type": "knowledge",
            "subtype": "text",
            "brief": "定义导数。",
            "tags": ["导数", "伪标签"],
            "concepts": [
                {"name": "导数", "evidence": "导数描述函数的局部变化率"},
                {"name": "伪概念", "evidence": "这是模型改写"},
            ],
        }, ensure_ascii=False))
        parsed["concepts"] = _verified_concepts(
            parsed["concepts"],
            "导数描述函数的局部变化率，并可由极限定义。",
        )
        candidates = page_brief_candidates(
            file_rel="资源/books/calculus.pdf",
            page=7,
            page_text="导数描述函数的局部变化率，并可由极限定义。",
            brief=parsed,
            source_id="brief:calculus:7:v2",
        )

        self.assertEqual([item["surface"] for item in candidates], ["导数"])
        self.assertEqual(candidates[0]["quote"], "导数描述函数的局部变化率")
        self.assertTrue(
            all(item["surface"] != "伪标签" for item in candidates)
        )
        self.assertEqual(page_brief_candidates(
            file_rel="a.pdf",
            page=1,
            page_text="目录",
            brief={"page_type": "skip", "tags": ["目录"], "concepts": []},
            source_id="brief:a:1:v2",
        ), [])

    def test_page_brief_parser_only_accepts_explicit_skip(self):
        for raw in (
            "{}",
            json.dumps({
                "brief": "",
                "tags": [],
                "concepts": [],
            }),
            json.dumps({
                "page_type": "unknown",
                "brief": "",
                "tags": [],
                "concepts": [],
            }),
        ):
            parsed = _parse(raw)
            self.assertEqual(parsed["page_type"], "")
            self.assertEqual(parsed["brief"], "")
            self.assertEqual(parsed["tags"], [])
            self.assertEqual(parsed["concepts"], [])

        explicit_skip = _parse(json.dumps({
            "page_type": "skip",
            "subtype": "blank",
            "brief": "",
            "tags": [],
            "concepts": [{
                "name": "不应保留",
                "evidence": "skip 页不进入知识图谱",
            }],
        }, ensure_ascii=False))
        self.assertEqual(explicit_skip["page_type"], "skip")
        self.assertEqual(explicit_skip["subtype"], "blank")
        self.assertEqual(explicit_skip["concepts"], [])

        inferred_knowledge = _parse(json.dumps({
            "brief": "定义导数。",
            "tags": ["导数"],
            "concepts": [],
        }, ensure_ascii=False))
        self.assertEqual(inferred_knowledge["page_type"], "knowledge")

    def test_page_brief_transport_metadata_does_not_change_mutation(self):
        service = _service(self.tmp_path)
        semantic = {
            "brief": "定义线性映射。",
            "tags": ["线性映射"],
            "concepts": [{
                "name": "线性映射",
                "evidence": "线性映射保持向量加法。",
            }],
            "page_type": "knowledge",
            "subtype": "text",
        }
        first = promote_page_brief(
            file_rel="资源/books/a.pdf",
            page=12,
            page_text="线性映射保持向量加法。",
            brief=semantic,
            service=service,
        )
        retry = promote_page_brief(
            file_rel="资源/books/a.pdf",
            page=12,
            page_text="线性映射保持向量加法。",
            brief={
                **semantic,
                "model": "haiku",
                "kg_status": "pending",
                "kg_error": "timeout",
                "ts": 123,
            },
            service=service,
        )
        self.assertEqual(retry["mutationId"], first["mutationId"])
        self.assertIs(retry["replay"], True)
        self.assertEqual(service.load_graph()["nodes"]["线性映射"]["signal"], 1)

    def test_concept_name_must_appear_in_exact_evidence(self):
        service = _service(self.tmp_path)
        with self.assertRaises(ConceptNodeError) as caught:
            service.upsert_candidates(
                [_page_candidate(
                    surface="线性映射",
                    quote="A linear map preserves vector addition.",
                    source_text="A linear map preserves vector addition.",
                )],
                mutation_id="translated-name",
            )
        self.assertEqual(caught.exception.code, "BW_KG_NODE_EVIDENCE")
        self.assertEqual(
            _verified_concepts(
                [{"name": "线性映射", "evidence": "A linear map preserves vector addition."}],
                "A linear map preserves vector addition.",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
