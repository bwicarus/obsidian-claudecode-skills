"""把知识图谱只读地暴露给电脑侧。

为什么要有它：AI 要在电脑上做批量处理（跨书串联、找前置、判断该读什么），
而 KG 现在只在 Pi 上。每问一次就跨网一次，既慢又依赖 Pi 在线；批量处理更是
不可能一条条问。所以电脑上要有一份本地副本。

**只读、单向。** 图是"这本书讲了什么、什么在什么之前"，跟谁在学无关，所以
不存在两边都改的问题 —— 没有冲突，就不需要双向同步那一整套。掌握度是另一回事
（它跟人走、两边都会写），不在这个端点里，将来单独处理。

设计上的两个要点：

  · **带修订号**。副本要能回答"我这份是不是最新的"。没有它，电脑侧要么每次
    全量重拉（一本书的图不小），要么就得赌。修订号用内容哈希而不是 mtime ——
    重跑一次建图但内容没变，不应该让所有副本重下一遍。

  · **列表与内容分开**。先问"有哪些书、各自什么修订号"，再只拉变了的那几本。
    一次性全量返回会让"检查更新"和"下载"变成同一件事，而前者应该很便宜。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from flask import abort, jsonify, request

CONTRACT = "reader-kg-export/1"

# 图里跟"谁在学"有关的字段。它们随人变化、两边都会写，因此不走这个只读通道 ——
# 混进来的话，电脑侧会拿到一份看似权威、实则可能已经过时的掌握度，
# 而它没有任何办法知道自己拿到的是旧的。
_LEARNER_NODE_FIELDS = frozenset({
    "mastery",
    "mastery_level",
    "mastery_inferred",
    "state",
    "containing_notes",
    "note_ref",
    "note_ref_ai_verified",
    "card_refs",
    "has_cards",
})

# 顶层同理：这些是证据与呈现策略，不是图本身。
_LEARNER_TOP_FIELDS = frozenset({
    "_note_to_covered_l2",
    "_rejected_links",
    "_archive_suggestions",
})


def _kg_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT")
    root = Path(override) if override else Path(__file__).resolve().parent.parent
    return root / "knowledge_graph"


def graph_only(kg: dict) -> dict:
    """剥掉跟学习者有关的部分，只留图。

    不是为了省流量 —— 是为了让这份副本的语义单一：**它描述的是书，不是你。**
    掌握度混进来，电脑侧就无法判断手上这份是不是最新的学习状态。
    """
    out = {
        key: value
        for key, value in kg.items()
        if key not in _LEARNER_TOP_FIELDS and key != "nodes"
    }
    nodes = []
    for node in kg.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nodes.append({
            key: value
            for key, value in node.items()
            if key not in _LEARNER_NODE_FIELDS
        })
    out["nodes"] = nodes
    return out


def revision_of(payload: dict) -> str:
    """内容哈希。重跑建图但内容没变时，副本不必重下。"""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _safe_book(name: str) -> str:
    """书名来自 URL，必须挡住路径穿越。只允许一层文件名。"""
    value = str(name or "")
    if not value or len(value) > 200:
        abort(400)
    if "/" in value or "\\" in value or value.startswith("."):
        abort(400)
    return value


def _load_graph(path: Path) -> dict:
    try:
        kg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        abort(500)
    if not isinstance(kg, dict):
        abort(500)
    return graph_only(kg)


def register_kg_export(app, require_owner) -> None:
    """挂两个只读端点。

    `require_owner()` 由 app.py 注入：它返回当前已验证用户或 None。
    鉴权不在这里自己实现 —— 另写一套认证是最容易出岔子的地方。
    """

    @app.route("/api/kg/index", methods=["GET"])
    def api_kg_index():
        if not require_owner():
            abort(401)
        directory = _kg_dir()
        books = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    graph = _load_graph(path)
                except Exception:
                    # 一本书坏了不该让整个索引失败 —— 那会让电脑侧以为
                    # 一本书都没有。跳过它，但把它列出来。
                    books.append({
                        "book": path.stem,
                        "error": "unreadable",
                    })
                    continue
                books.append({
                    "book": path.stem,
                    "revision": revision_of(graph),
                    "nodes": len(graph.get("nodes") or []),
                    "edges": len(graph.get("edges") or []),
                })
        return jsonify({
            "contract": CONTRACT,
            "ok": True,
            "books": books,
        })

    @app.route("/api/kg/graph/<book>", methods=["GET"])
    def api_kg_graph(book):
        if not require_owner():
            abort(401)
        path = _kg_dir() / f"{_safe_book(book)}.json"
        if not path.is_file():
            abort(404)
        graph = _load_graph(path)
        revision = revision_of(graph)
        # 客户端报上它手里的修订号，一致就不必重传。
        if request.args.get("since") == revision:
            return jsonify({
                "contract": CONTRACT,
                "ok": True,
                "book": book,
                "revision": revision,
                "unchanged": True,
            })
        return jsonify({
            "contract": CONTRACT,
            "ok": True,
            "book": book,
            "revision": revision,
            "unchanged": False,
            "graph": graph,
        })
