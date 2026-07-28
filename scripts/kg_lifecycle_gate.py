#!/usr/bin/env python3
"""Release-local, zero-data KG lifecycle safety gate.

This is production executable code, not a checkout test runner.  It exercises
the invariants that make a nightly concept-graph write safe while redirecting
every write to a temporary directory.  All imported KG modules must resolve
inside the same immutable release as this file; any mixed or broken release
fails closed before the real graph stages start.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = CODE_ROOT / "scripts"
KG_DIR = SCRIPTS_DIR / "kg"
for import_root in (KG_DIR, SCRIPTS_DIR):
    value = str(import_root)
    if value not in sys.path:
        sys.path.insert(0, value)

import attention_profile as AP  # noqa: E402
import build_unified_graph as BUG  # noqa: E402
import concept_node_service as CNS  # noqa: E402
import promote_concepts as PC  # noqa: E402
import propose_concept_notes as PCN  # noqa: E402


class LifecycleGateError(RuntimeError):
    """The selected KG runtime cannot prove its write invariants."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleGateError(message)


def _require_release_module(module, relative: str) -> None:
    expected = (CODE_ROOT / relative).resolve()
    try:
        actual = Path(module.__file__).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise LifecycleGateError(
            f"模块身份无法验证: {relative}"
        ) from exc
    if actual != expected:
        raise LifecycleGateError(
            f"模块逃逸 immutable release: {relative} -> {actual}"
        )


def _check_module_identity() -> None:
    _require_release_module(AP, "scripts/attention_profile.py")
    _require_release_module(BUG, "scripts/kg/build_unified_graph.py")
    _require_release_module(CNS, "scripts/kg/concept_node_service.py")
    _require_release_module(PC, "scripts/kg/promote_concepts.py")
    _require_release_module(PCN, "scripts/kg/propose_concept_notes.py")


def _check_edge_lifecycle(root: Path) -> None:
    saved = (PC.OUT, PC.CONF_FILE)
    PC.OUT = root / "edge-graph.json"
    PC.CONF_FILE = root / "edge-confirmations.json"
    try:
        graph = {
            "nodes": {},
            "edge_claims": {},
            "edge_audits": {},
            "meta": {},
        }
        PC.upsert_claim(
            graph, "A", "B", "prereq", "prereq", "B 依赖 A",
            "note:x.md", "quote", "aliasscan+sentconfirm",
        )
        PC.upsert_claim(
            graph, "C", "D", "related", "demote", "C 与 D 相关",
            "note:y.md", "prose", "aliasscan+sentconfirm",
        )
        PC.upsert_claim(
            graph, "E", "B", "prereq", "prereq", "B 用到 E",
            "book:z.pdf#p3", "book", "forwardsearch+aiclassify",
        )
        first = PC.derive_edges(graph)
        _require(
            {
                edge["status"]
                for edge in first
                if edge["kind"] == "prereq"
            }
            == {"shadow"},
            "未审计 prereq 没有保持 shadow",
        )

        graph["edge_audits"][PC._edge_id("A", "B")] = {
            "verdict": "keep",
            "ts": 1,
        }
        graph["edge_audits"][PC._edge_id("C", "D")] = {
            "verdict": "remove",
            "ts": 1,
        }
        PC.upsert_claim(
            graph, "A", "B", "prereq", "prereq", "B 依赖 A",
            "note:x.md", "quote", "aliasscan+sentconfirm",
        )
        PC.upsert_claim(
            graph, "C", "D", "related", "demote", "C 与 D 相关",
            "note:y.md", "prose", "aliasscan+sentconfirm",
        )
        second = PC.derive_edges(graph)
        projection = {
            (edge["from"], edge["to"]): edge for edge in second
        }
        _require(("E", "B") in projection, "第二晚丢失其它来源边")
        _require(
            projection[("A", "B")]["status"] == "audited",
            "审计 keep 被重扫覆盖",
        )
        _require(("C", "D") not in projection, "审计墓碑发生复活")
        _require(
            PC._edge_id("C", "D") in graph["edge_claims"],
            "墓碑错误删除了原始证据 claim",
        )
        _require(
            len(PC.derive_edges(graph)) == len(second),
            "重复派生导致边数量增长",
        )
        observations = graph["edge_claims"][
            PC._edge_id("A", "B")
        ]["observations"]
        _require(len(observations) == 1, "重复证据没有幂等去重")

        override_graph = {"edge_claims": {}, "edge_audits": {}}
        PC.upsert_claim(
            override_graph, "A", "B", "prereq", "prereq", "q",
            "note:x.md", "quote", "m",
        )
        PC.CONF_FILE.write_text(
            json.dumps({"edges": {"A|B": False}}),
            encoding="utf-8",
        )
        _require(
            PC.derive_edges(override_graph) == [],
            "用户否决没有压过自动边",
        )
        PC.CONF_FILE.write_text(
            json.dumps({"edges": {"A|B|prereq": True}}),
            encoding="utf-8",
        )
        _require(
            PC.derive_edges(override_graph)[0]["status"]
            == "user_confirmed",
            "旧三段 override 没有兼容为用户确认",
        )
        override_graph["edge_audits"][PC._edge_id("A", "B")] = {
            "verdict": "remove",
            "ts": 1,
        }
        _require(
            PC.derive_edges(override_graph)[0]["status"]
            == "user_confirmed",
            "用户确认没有压过审计墓碑",
        )

        PC.CONF_FILE.write_text("{broken", encoding="utf-8")
        try:
            PC._load_conf_edges(strict=True)
        except PC.ConceptNodeError as exc:
            _require(
                exc.code == "BW_KG_NODE_CONFIRMATIONS_CORRUPT",
                "损坏确认文件返回了错误的 fail-closed 代码",
            )
        else:
            raise LifecycleGateError("损坏确认文件没有 fail closed")
        PC.CONF_FILE.write_text(
            json.dumps({"edges": {"A|B": True}}),
            encoding="utf-8",
        )
        _require(
            PC.derive_edges(
                override_graph,
                confirmation_edges={"A|B": False},
            )
            == [],
            "事务没有消费冻结的确认快照",
        )

        generated = (
            "---\ntype: concept-auto\n---\n# X\n\n## 定义\n"
            "**AI 生成(仅参考,非原文)**:AI文本提到子空间。\n\n"
            "## 概念链接(自动)\n- 相关:[[200-向量空间|向量空间]]\n"
            "\n## AI 解释(自动)\n直和相关。\n"
        )
        scanned = " ".join(
            text for text, _source in PC._note_scannable_text(generated)
        )
        _require(
            all(
                token not in scanned
                for token in ("子空间", "向量空间", "直和", "AI 生成")
            ),
            "AI 自动文本反哺了边扫描",
        )
        sentences = PC._split_sentences(
            "A vector space is a set V. "
            "It satisfies axioms like 1.20 and F^n cases. "
            "Next sentence here."
        )
        _require(
            len(sentences) >= 3
            and any("vector space" in item for item in sentences),
            "英文句子切分退化",
        )
    finally:
        PC.OUT, PC.CONF_FILE = saved


def _check_identity_resolution(root: Path) -> None:
    vault = root / "vault"
    concept_dir = vault / "资源" / "概念" / "书"
    concept_dir.mkdir(parents=True)
    (concept_dir / "200-食中毒.md").write_text(
        "---\ntype: concept-auto\naliases: []\n---\n# 食中毒\n",
        encoding="utf-8",
    )
    saved = (AP.VAULT_ROOT, PCN.EMERGENT)
    AP.VAULT_ROOT = vault
    PCN.EMERGENT = root / "missing-emergent.json"
    try:
        identity, how = PCN._identity_resolve("食中毒", use_ai=False)
        _require(
            bool(identity) and how == "note_filename",
            "已有概念笔记没有按文件名解析为同一身份",
        )
    finally:
        AP.VAULT_ROOT, PCN.EMERGENT = saved


def _check_unified_projection(root: Path) -> None:
    kg_dir = root / "knowledge_graph"
    kg_dir.mkdir()
    emergent = root / "emergent.json"
    output = root / "unified.json"
    confirmations = root / "confirmations.json"
    saved = (BUG.KG_DIR, BUG.EMERGENT, BUG.OUT, BUG.CONF)
    BUG.KG_DIR, BUG.EMERGENT = kg_dir, emergent
    BUG.OUT, BUG.CONF = output, confirmations
    try:
        (kg_dir / "book.json").write_text(
            json.dumps(
                {
                    "book": "Book",
                    "nodes": [
                        {
                            "id": "auth-1",
                            "level": 2,
                            "name": "Authored",
                            "pages": [1],
                        }
                    ],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )
        active_id = CNS.stable_node_id("active")
        emergent.write_text(
            json.dumps(
                {
                    "nodes": {
                        "active": {
                            "id": active_id,
                            "surface": "Active",
                            "subject": "Subject",
                            "origin": "emergent",
                            "provenance": [],
                        },
                        "rolled-back": {
                            "id": CNS.stable_node_id("rolled-back"),
                            "surface": "Rolled back",
                            "deleted": True,
                            "tombstone": {"rollbackOf": "tx-1"},
                        },
                        "authored": {
                            "id": CNS.stable_node_id("authored"),
                            "surface": "Authored",
                            "in_authored_kg": True,
                            "authored_ref": "Book#auth-1",
                            "provenance": [
                                {"type": "page-brief", "page": 7}
                            ],
                        },
                    },
                    "edges": [
                        {
                            "from": "active",
                            "to": "authored",
                            "kind": "related",
                            "status": "audited",
                            "quote": "evidence",
                        }
                    ],
                    "meta": {},
                }
            ),
            encoding="utf-8",
        )
        confirmations.write_text(
            json.dumps({"nodes": {}, "edges": {}}),
            encoding="utf-8",
        )
        result = BUG.build(write=False)
        ids = {node["id"] for node in result["nodes"]}
        _require(active_id in ids, "active emergent 节点丢失")
        _require(
            CNS.stable_node_id("rolled-back") not in ids,
            "rollback 墓碑在统一图复活",
        )
        _require(
            CNS.stable_node_id("authored") not in ids,
            "authored anchor 被重复建点",
        )
        authored = next(
            node
            for node in result["nodes"]
            if node["id"] == "Book::auth-1"
        )
        _require(authored["pages"] == [1, 7], "anchor 页证据没有合并")
        _require(
            authored["emergent_key"] == "authored",
            "anchor emergent identity 丢失",
        )
        _require(
            any(
                edge["from"] == active_id
                and edge["to"] == "Book::auth-1"
                for edge in result["edges"]
            ),
            "跨来源边没有重映射到 authored anchor",
        )
    finally:
        BUG.KG_DIR, BUG.EMERGENT, BUG.OUT, BUG.CONF = saved


def _check_nightly_merge(root: Path) -> None:
    graph_path = root / "nightly-emergent.json"
    aliases_path = root / "aliases.json"
    confirmations_path = root / "nightly-confirmations.json"
    kg_dir = root / "nightly-kg"
    vault = root / "nightly-vault"
    graph = {
        "nodes": {
            "page concept": {
                "id": CNS.stable_node_id("page concept"),
                "surface": "Page Concept",
                "origin": "emergent",
                "signal": 0,
                "provenance": [],
            },
            "rolled back": {
                "id": CNS.stable_node_id("rolled back"),
                "surface": "Rolled Back",
                "origin": "emergent",
                "deleted": True,
                "tombstone": {"rollbackOf": "tx-old"},
            },
        },
        "edges": [],
        "edge_claims": {},
        "edge_audits": {},
        "meta": {},
    }
    saved_globals = (
        PC.OUT,
        PC.ALIASES_FILE,
        PC.CONF_FILE,
        PC.KG_DIR,
        PC.VAULT,
    )
    saved_functions = (PC.collect_seeds, PC._authored_kg_terms)
    PC.OUT = graph_path
    PC.ALIASES_FILE = aliases_path
    PC.CONF_FILE = confirmations_path
    PC.KG_DIR = kg_dir
    PC.VAULT = vault
    kg_dir.mkdir()
    vault.mkdir()
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    aliases_path.write_text("{}", encoding="utf-8")
    confirmations_path.write_text(
        '{"nodes":{},"edges":{}}',
        encoding="utf-8",
    )
    PC.collect_seeds = lambda: {
        "nightly concept": {
            "surface": "Nightly Concept",
            "sources": {"note"},
            "signal": 1,
            "provenance": [
                {"type": "note", "ref": "000-night.md"}
            ],
        }
    }
    PC._authored_kg_terms = lambda: {}
    try:
        result = PC.build(write=True)
        graph_after_first = graph_path.read_bytes()
        journal_path = graph_path.parent / "kg-node-mutations.jsonl"
        journal_after_first = journal_path.read_bytes()
        repeated = PC.build(write=True)
        _require(
            graph_path.read_bytes() == graph_after_first,
            "相同 nightly build 改写了 graph",
        )
        _require(
            journal_path.read_bytes() == journal_after_first,
            "相同 nightly build 增长了 mutation journal",
        )
        _require(
            repeated["nodes"] == result["nodes"],
            "相同 nightly build 投影不一致",
        )
        _require("page concept" in result["nodes"], "PageBrief 节点被覆盖")
        _require(
            result["nodes"]["rolled back"].get("deleted") is True,
            "rollback 墓碑被覆盖",
        )
        _require(
            result["nodes"]["page concept"]["id"]
            == CNS.stable_node_id("page concept"),
            "PageBrief 节点稳定 ID 漂移",
        )
        _require(
            "nightly concept" in result["nodes"],
            "nightly 新节点未写入",
        )
    finally:
        (
            PC.OUT,
            PC.ALIASES_FILE,
            PC.CONF_FILE,
            PC.KG_DIR,
            PC.VAULT,
        ) = saved_globals
        PC.collect_seeds, PC._authored_kg_terms = saved_functions


def run_gate() -> tuple[str, ...]:
    _check_module_identity()
    with tempfile.TemporaryDirectory(prefix="bw-kg-lifecycle-gate.") as raw:
        root = Path(raw)
        checks = (
            ("edge-lifecycle", _check_edge_lifecycle),
            ("identity-resolution", _check_identity_resolution),
            ("unified-projection", _check_unified_projection),
            ("nightly-merge", _check_nightly_merge),
        )
        completed = []
        for name, check in checks:
            check_root = root / name
            check_root.mkdir()
            check(check_root)
            completed.append(name)
    return tuple(completed)


def main() -> int:
    try:
        completed = run_gate()
    except Exception as exc:
        print(
            f"KG lifecycle release gate: FAIL: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        "KG lifecycle release gate: PASS: " + ",".join(completed),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
